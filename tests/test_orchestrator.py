"""Unit tests for the deterministic graph nodes (orchestrator.py).

Lean core suite: one happy path per flow + one guard per documented lesson
(L17 POI ranking, L18 missing-slot ask, L39 PT walking degrade, L43 dest anchoring).
"""
import json
from datetime import datetime

from snap4city_mobility_mcp import mcp_tools
from snap4city_mobility_mcp.llm import Llama4Error
from snap4city_mobility_mcp.orchestrator import (
    _EXTRACT_SLOTS_SCHEMA,
    ROME,
    _build_graph,
    _extract_data,
    _extract_parking,
    _format_detail,
    _parse_departure,
    _pick_coord,
    _request_to_intent,
    _results_view,
    _routing_hint,
    execute,
    respond,
    understand,
)


def _parking_search(*spots) -> dict:
    """service_search_near_gps_position envelope from (name, lng, lat) tuples, in the LIVE
    shape probe_parking.py captured: {"result": [[uris], {"Services": {"features": [...]}}]}.
    The search carries NO free-spaces (that comes per-spot from service_info_dev)."""
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "name": name,
                "serviceUri": f"http://www.disit.org/km4city/resource/{name}",
                "serviceType": "TransferServiceAndRenting_Car_park",
                "tipo": "Car_park",
                "distance": "0.1",
            },
        }
        for (name, lng, lat) in spots
    ]
    uris = [f["properties"]["serviceUri"] for f in features]
    return {"result": [uris, {"Services": {"features": features}}]}


def _parking_realtime(free, capacity=200) -> dict:
    """service_info_dev realtime response: latest binding carries free/total as strings."""
    return {"realtime": {"head": {"vars": ["freeParkingLots", "capacity"]}, "results": {"bindings": [
        {"freeParkingLots": {"value": str(free)}, "capacity": {"value": str(capacity)}},
    ]}}}


def _slots_response(arguments: str) -> dict:
    """A canned OpenAI response whose assistant turn is a forced extract_slots call."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "extract_slots", "arguments": arguments},
                        }
                    ],
                }
            }
        ]
    }


def _text_response(text: str) -> dict:
    """A canned OpenAI response whose assistant turn is a plain text answer."""
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _feature_collection(lng: float, lat: float) -> dict:
    """Minimal GeoJSON FeatureCollection (server `address_search_location` shape)."""
    return {
        "type": "FeatureCollection",
        "features": [{"geometry": {"coordinates": [lng, lat]}, "properties": {"city": "FIRENZE"}}],
    }


def _fc_with_addresses(*entries) -> dict:
    """FeatureCollection from (lng, lat, address) triples (address may be None)."""
    return {"type": "FeatureCollection", "features": [
        {"geometry": {"coordinates": [lng, lat]},
         "properties": {"address": addr, "city": "FIRENZE"}}
        for lng, lat, addr in entries
    ]}


def _journey(distance=1.83, routes=None) -> dict:
    """The local `route` tool's payload shape: {"journey": {"routes": [...]}}."""
    if routes is None:
        routes = [{"wkt": "LINESTRING(1 2,3 4)", "distance": distance, "time": "00:23:18"}]
    return {"journey": {"routes": routes}}


class _RaisingLLM:
    """LLM double whose achat always raises — exercises respond's template fallback."""

    async def achat(self, *args, **kwargs):
        raise Llama4Error("boom")


# --- understand --------------------------------------------------------------

async def test_understand_parses_slots(make_llm):
    llm = make_llm([_slots_response(
        '{"request_type":"journey","origin_text":"Duomo",'
        '"destination_text":"Santa Croce","destination_category":"","mode":"foot"}'
    )])
    out = await understand(
        {"messages": [{"role": "user", "content": "from Duomo to Santa Croce on foot"}]}, llm=llm
    )
    assert out["intent"] == "route"  # journey folded into the internal route intent
    assert out["slots"]["origin_text"] == "Duomo"
    assert out["slots"]["mode"] == "foot"


def test_request_to_intent():
    assert _request_to_intent({"request_type": "journey"}) == "route"
    assert _request_to_intent({"request_type": "other"}) == "other"
    assert _request_to_intent({}) == "other"


def test_extract_slots_schema_requires_all_fields():
    """Llama4 only fills required params — a real run dropped destination_text when
    it was optional. All slots must stay required ('' marks an absent one)."""
    params = _EXTRACT_SLOTS_SCHEMA["function"]["parameters"]
    assert set(params["required"]) == {
        "request_type", "origin_text", "destination_text", "destination_category", "mode",
        "departure_time",
    }
    assert "" in params["properties"]["mode"]["enum"]  # required mode needs an 'absent' value


# --- _parse_departure ---------------------------------------------------------

def test_parse_departure_reads_a_time_today():
    now = datetime(2026, 7, 13, 9, 0, tzinfo=ROME)
    assert _parse_departure("18:00", now) == datetime(2026, 7, 13, 18, 0, tzinfo=ROME)


def test_parse_departure_rolls_a_past_time_to_tomorrow():
    """"alle 8" asked at 22:00 means the next 8 o'clock: departing in the past would query a
    dead GTFS window."""
    now = datetime(2026, 7, 13, 22, 0, tzinfo=ROME)
    assert _parse_departure("08:00", now) == datetime(2026, 7, 14, 8, 0, tzinfo=ROME)


def test_parse_departure_reads_a_dated_time():
    now = datetime(2026, 7, 13, 9, 0, tzinfo=ROME)
    assert _parse_departure("2026-07-14T09:30", now) == datetime(2026, 7, 14, 9, 30, tzinfo=ROME)


def test_parse_departure_none_when_absent_or_unusable():
    """No time given (or a garbled one) → depart now, never guess a time."""
    now = datetime(2026, 7, 13, 9, 0, tzinfo=ROME)
    assert _parse_departure("", now) is None
    assert _parse_departure("stasera", now) is None
    assert _parse_departure("25:99", now) is None


# --- _pick_coord (L17 + GPS-nearest) ------------------------------------------

def test_pick_coord_rejects_labels_with_extra_tokens():
    # "PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S." (a company, L17) contains the
    # search tokens but adds its own → no match; the real square matches even
    # across case and the Italian function word "del".
    fc = _fc_with_addresses(
        (11.2421, 43.7736, "PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S."),
        (11.2560, 43.7731, "PIAZZA DEL DUOMO"),
    )
    assert _pick_coord(fc, "Piazza Duomo") == [11.2560, 43.7731]


def test_pick_coord_gps_picks_nearest_candidate():
    """Two equally label-matching squares in different towns: with the user's GPS the
    nearest one wins; without it the server's first (best-score) hit wins as before."""
    fc = _fc_with_addresses(
        (10.2270, 43.9580, "PIAZZA DEL DUOMO"),   # Pietrasanta
        (11.2560, 43.7731, "PIAZZA DEL DUOMO"),   # Florence
    )
    florence_gps = {"lat": 43.7731, "lng": 11.2558}
    assert _pick_coord(fc, "Piazza Duomo", gps=florence_gps) == [11.2560, 43.7731]
    assert _pick_coord(fc, "Piazza Duomo") == [10.2270, 43.9580]


# --- execute -----------------------------------------------------------------

async def test_execute_route_success(make_client, make_result):
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # routing
    ])
    # City-named searches resolve on the address pass alone (named-city subset), so one
    # geocode call per endpoint; a bare place name would trigger the POI fallback pass too.
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "foot"}
    out = await execute({"slots": slots}, client=client)
    assert out["unsupported"] is False
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "address_search_location", "routing"]
    # the local route tool got [lng,lat] mapped to the right lat/lng fields + the vehicle
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["start_latitude"] == 43.77 and route_args["start_longitude"] == 11.24
    assert route_args["routetype"] == "foot" and route_args["vehicle"] == "foot"
    assert client.calls[-1][0] == "route"


async def test_execute_unsupported_intent(make_client):
    client = make_client([])  # must never reach the client
    out = await execute({"slots": {"intent": "other"}}, client=client)
    assert out["unsupported"] is True
    assert out["tool_results"] == []
    assert client.calls == []


async def test_execute_no_gps_missing_origin_unsupported(make_client):
    """No origin text and no GPS → nothing can cover the origin: unsupported, no calls
    (respond then asks for the starting point)."""
    client = make_client([])
    slots = {"intent": "route", "origin_text": "", "destination_text": "Santa Croce", "mode": ""}
    out = await execute({"slots": slots}, client=client)
    assert out["unsupported"] is True
    assert client.calls == []


async def test_execute_gps_default_origin(make_client, make_result):
    """No origin text + GPS → the origin IS the GPS point (no origin geocode); it is
    reverse-geocoded once (success → audited) so respond can say 'dalla tua posizione'."""
    rev = {"result": [{"number": "3", "address": "VIA ZARA", "municipality": "FIRENZE"}]}
    client = make_client([
        make_result(structured=rev),                                  # coordinates_to_address
        make_result(structured=_feature_collection(11.26, 43.76)),   # geocode dest (address pass)
        make_result(structured={"type": "FeatureCollection", "features": []}),  # dest POI pass (no city named)
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "", "destination_text": "Santa Croce", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    assert out["unsupported"] is False
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["coordinates_to_address", "address_search_location", "routing"]
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["start_latitude"] == 43.7731 and route_args["start_longitude"] == 11.2558
    assert out["endpoints"]["origin"] == {"lat": 43.7731, "lng": 11.2558}


async def test_execute_gps_default_origin_reverse_failure_not_audited(make_client, make_result):
    """A failed reverse geocode must NOT enter the audit (it would trip respond's error
    rule for a trip that is fine); the route still runs from the GPS point."""
    client = make_client([
        make_result(structured={"error": "boom"}),                    # coordinates_to_address fails
        make_result(structured=_feature_collection(11.26, 43.76)),   # geocode dest (address pass)
        make_result(structured={"type": "FeatureCollection", "features": []}),  # dest POI pass
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "", "destination_text": "Santa Croce", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "routing"]
    assert out["endpoints"]["origin"] == {"lat": 43.7731, "lng": 11.2558}


async def test_execute_category_destination_uses_near_tool(make_client, make_result):
    """A generic-category destination + GPS → service_search_near_gps_position around the
    GPS point; the nearest service (features[0], server distance-sorted) becomes the
    destination. No destination geocode call is made."""
    pharmacy = _parking_search(("Farmacia Moro", 11.2546, 43.7735))  # same envelope shape
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),   # geocode origin
        make_result(structured=pharmacy),                             # near search, first rung
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "farmacia",
             "destination_category": "Pharmacy", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "service_search_near_gps_position", "routing"]
    near_args = json.loads(out["tool_results"][1]["args"])
    assert near_args["categories"] == "Pharmacy"
    assert near_args["latitude"] == 43.7731 and near_args["longitude"] == 11.2558
    assert near_args["maxdistance"] == 0.5  # first rung of the widening ladder
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["end_latitude"] == 43.7735 and route_args["end_longitude"] == 11.2546
    assert out["endpoints"]["destination"] == {"lat": 43.7735, "lng": 11.2546}


async def test_execute_dest_geocode_anchors_to_origin(make_client, make_result):
    """Same-name streets in different towns, no named city, no GPS: the destination
    candidate nearest the resolved ORIGIN wins, not the server's first hit (live-tested:
    "via Pisana 166" from a Florence origin got routed to Lucca's VIA PISANA, 65 km)."""
    dest_fc = _fc_with_addresses(
        (10.4901, 43.8424, "VIA PISANA"),  # Lucca — the server's first hit
        (11.2216, 43.7747, "VIA PISANA"),  # Florence — nearest to the origin
    )
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # origin (city-named, 1 call)
        make_result(structured=dest_fc),                             # dest address pass
        make_result(structured={"type": "FeatureCollection", "features": []}),  # dest POI pass
        make_result(structured=_journey()),                          # routing
    ])
    slots = {"intent": "route", "origin_text": "via Mortuli 40, Firenze",
             "destination_text": "via Pisana 166", "mode": "foot"}
    out = await execute({"slots": slots}, client=client)
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["end_latitude"] == 43.7747 and route_args["end_longitude"] == 11.2216


async def test_execute_far_gps_named_city_still_routes(make_client, make_result):
    """A user physically far from the data region (live-tested from Brescia, ~211 km) who
    names an in-region city ("..., Firenze") gets a normal route: distance from the user's
    GPS never vetoes a geocode hit (the old 150 km coverage sentinel mis-killed exactly
    this legitimate remote-trip query and was removed)."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # origin: Firenze hit
        make_result(structured=_feature_collection(11.26, 43.76)),  # dest: Firenze hit
        make_result(structured=_journey()),                          # routing
    ])
    slots = {"intent": "route", "origin_text": "via Pisana 157, Firenze",
             "destination_text": "via Barna 7, Firenze", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 45.5308, "lng": 10.1828}}, client=client)
    assert out["unsupported"] is False
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "address_search_location", "routing"]
    assert all("error" not in e["result"] for e in out["tool_results"][:2])
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["start_latitude"] == 43.77 and route_args["end_latitude"] == 43.76


async def test_execute_category_ladder_empty_falls_back_to_geocode(make_client, make_result):
    """All near-search rungs empty (bad category / nothing within 10 km) → only the last
    empty attempt is audited and the destination degrades to the plain text geocode."""
    empty = {"result": [[], {"Services": {"features": []}}]}
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),   # geocode origin
        make_result(structured=empty),                                # near rung 0.5
        make_result(structured=empty),                                # near rung 2
        make_result(structured=empty),                                # near rung 10
        make_result(structured=_feature_collection(11.26, 43.76)),   # dest geocode (address pass)
        make_result(structured={"type": "FeatureCollection", "features": []}),  # dest POI pass
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "farmacia",
             "destination_category": "Pharmacy", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    assert len([n for n, _ in client.calls if n == "service_search_near_gps_position"]) == 3
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "service_search_near_gps_position",
                     "address_search_location", "routing"]
    assert out["endpoints"]["destination"] == {"lat": 43.76, "lng": 11.26}


async def test_execute_car_error_drops_parking(make_client, make_result):
    """A failed car route means nowhere to drive: the parking search (fetched
    concurrently to add no wall-clock) is discarded, never surfaced or enriched."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured={"error": "whatif-router returned no car path"}),  # route (car)
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),  # parking search (discarded)
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert "route_error" in _extract_data(out["tool_results"])
    assert out["parking"] == []
    assert not any(n == "service_info_dev" for n, _ in client.calls)


# --- _extract_data -----------------------------------------------------------

def test_extract_data_preserves_full_wkt():
    long_wkt = "LINESTRING(" + "9 4," * 50 + "1 1)"
    results = [{"name": "routing", "result": {"journey": {"routes": [{"wkt": long_wkt, "distance": 1.0}]}}}]
    data = _extract_data(results)
    assert data["wkt"] == long_wkt  # not truncated — the map widget needs the full geometry
    assert len(data["wkt"]) > 80


def _routing_entry(routetype, *, route=None, error=None):
    result = {"error": error} if error is not None else {"journey": {"routes": [route]}}
    return {"name": "routing", "args": json.dumps({"routetype": routetype}), "result": result}


def test_extract_data_orders_routes_fastest_first():
    """No mode given → both routes returned; routes is fastest-first and the top-level
    mirrors the faster one (car here: 8 min beats 20 min on foot)."""
    results = [
        _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("car", route={"wkt": "LINESTRING(0 0,2 2)", "distance": 3.5, "time": "00:08:00"}),
    ]
    data = _extract_data(results)
    assert [r["mode"] for r in data["routes"]] == ["car", "foot"]
    assert data["mode"] == "car" and data["wkt"] == "LINESTRING(0 0,2 2)"


def test_extract_data_drops_empty_car_keeps_foot():
    """car failed (no path / router error) → a single foot route, no route_error."""
    results = [
        _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("car", error="whatif-router returned no car path"),
    ]
    data = _extract_data(results)
    assert len(data["routes"]) == 1 and data["routes"][0]["mode"] == "foot"
    assert "route_error" not in data


def test_extract_data_all_fail_reports_earliest_error():
    results = [
        _routing_entry("foot", error="whatif-router foot route failed: TimeoutError: x"),
        _routing_entry("car", error="whatif-router returned no car path"),
    ]
    data = _extract_data(results)
    assert data.get("route_error") == "whatif-router foot route failed: TimeoutError: x"  # the user's first mode
    assert "routes" not in data


async def test_execute_unspecified_mode_routes_all_three(make_client, make_result):
    """Empty mode → execute routes ALL THREE modes concurrently, so the reply can compare
    walking, driving and public transport and the map draws a line each. The modes keep their
    request order in the audit, and the parking entry stays last (car is among them)."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # foot
        make_result(structured=_journey()),                          # car
        make_result(structured=_journey()),                          # public_transport
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),  # parking search (car in modes)
        make_result(structured=_parking_realtime(5, 50)),            # P1 realtime enrichment
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": ""}
    out = await execute({"slots": slots}, client=client)
    routetypes = [json.loads(e["args"])["routetype"] for e in out["tool_results"] if e["name"] == "routing"]
    assert routetypes == ["foot", "car", "public_transport"]
    # car is among the modes → the parking entry is appended after all routing entries.
    assert out["tool_results"][-1]["name"] == "service_search_near_gps_position"
    assert out["parking"][0]["free_spaces"] == 5


async def test_execute_explicit_mode_pays_no_bus_latency(make_client, make_result):
    """An explicit mode routes THAT ONE only: asking "a piedi" must not wait for the bus router
    (which rebuilds its PT graph per request, ~30-45s), nor draw a car/bus line."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # foot only
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "foot"}
    out = await execute({"slots": slots}, client=client)
    routetypes = [json.loads(e["args"])["routetype"] for e in out["tool_results"] if e["name"] == "routing"]
    assert routetypes == ["foot"]


async def test_execute_stages_are_reported_in_order(make_client, make_result):
    """execute reports what it is doing (the bridge relays it to the chat box). The stage is
    emitted ONCE, before the gather — reporting from inside the concurrent _route coroutines would
    fire once per mode in a nondeterministic order. With a bus leg in the batch it gets the
    slow-stage label, because that is the one the user actually waits through."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),
        make_result(structured=_feature_collection(11.26, 43.76)),
        make_result(structured=_journey()),                          # foot
        make_result(structured=_journey()),                          # car
        make_result(structured=_journey()),                          # public_transport
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),
        make_result(structured=_parking_realtime(5, 50)),
    ])
    seen = []
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": ""}
    await execute({"slots": slots}, client=client, on_stage=seen.append)
    assert seen == ["geocode", "routing_bus"]


async def test_execute_stage_callback_failure_never_sinks_the_turn(make_client, make_result):
    """Progress is cosmetic: a callback that raises must not lose a route that was computed."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),
        make_result(structured=_feature_collection(11.26, 43.76)),
        make_result(structured=_journey()),
    ])

    def boom(_stage):
        raise RuntimeError("the poller went away")

    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "foot"}
    out = await execute({"slots": slots}, client=client, on_stage=boom)
    assert _extract_data(out["tool_results"])["routes"]


async def test_execute_departure_time_drives_the_gtfs_window(make_client, make_result):
    """A departure the user asked for ("alle 18") becomes the bus route's timetable window."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),
        make_result(structured=_feature_collection(11.26, 43.76)),
        make_result(structured=_journey()),
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze",
             "mode": "public_transport", "departure_time": "18:00"}
    out = await execute({"slots": slots}, client=client)
    _, sent = client.calls[-1]
    assert sent["startdatetime"].endswith("T18:00")
    assert out["departure"] == "18:00"  # respond states it; HH:MM only, never a date (L43)


async def test_execute_without_departure_time_leaves_now(make_client, make_result):
    """No departure asked for → the GTFS window is still pinned (now, in Rome), and the reply
    is told nothing to announce."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),
        make_result(structured=_feature_collection(11.26, 43.76)),
        make_result(structured=_journey()),
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze",
             "mode": "public_transport"}
    out = await execute({"slots": slots}, client=client)
    _, sent = client.calls[-1]
    assert sent["startdatetime"]  # never naive-UTC: a bare now() would query the timetable 2h off
    assert out["departure"] == ""


async def test_execute_public_transport_routes_vehicle_bus(make_client, make_result):
    """public_transport goes to the local `route` tool as vehicle=bus with a GTFS
    startdatetime; a returned bus journey becomes a drawable PT route."""
    bus_journey = {"journey": {"routes": [{
        "wkt": "LINESTRING(0 0,3 3)", "distance": 4.38,
        "arc": [{"transport": "bus", "transport_provider": "at - Firenze urbano",
                 "desc": "linea 57 da PORTE NUOVE BELFIORE", "line": "57"}],
    }]}}
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=bus_journey),                         # route (local tool, vehicle=bus)
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze", "mode": "public_transport"}
    out = await execute({"slots": slots}, client=client)
    # PT went to the local route tool as vehicle=bus (+ timetable window); the audit
    # entry is still tagged public_transport.
    name, sent = client.calls[-1]
    assert name == "route"
    assert sent["vehicle"] == "bus" and sent["startdatetime"]
    routings = [e for e in out["tool_results"] if e["name"] == "routing"]
    assert len(routings) == 1 and json.loads(routings[0]["args"])["routetype"] == "public_transport"
    # the bus journey (real ride leg) survives _extract_data as a drawable PT route.
    data = _extract_data(out["tool_results"])
    assert data["routes"][0]["mode"] == "public_transport"
    assert data["wkt"] == "LINESTRING(0 0,3 3)" and data["distance_km"] == 4.38


def test_extract_data_collects_three_modes():
    """foot+car+real-PT all succeed → routes has all three, keyed to distinct vehicles."""
    results = [
        _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("car", route={"wkt": "LINESTRING(0 0,2 2)", "distance": 3.5, "time": "00:08:00"}),
        _routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,3 3)", "distance": 3.0, "time": "00:15:00",
            "arc": [{"transport": "foot"}, {"transport": "bus"}],  # a real ride leg
        }),
    ]
    data = _extract_data(results)
    assert {r["mode"] for r in data["routes"]} == {"foot", "car", "public_transport"}
    assert len(data["routes"]) == 3


def test_extract_data_forwards_pt_legs():
    """The route tool's per-leg geometry rides along into the widget route (the dashboard
    draws the walk/ride split + stop pins from it, no second router call, L44); routes
    without it stay bare."""
    legs = [
        {"type": "foot", "wkt": "LINESTRING(0 0,1 1)"},
        {"type": "bus", "wkt": "LINESTRING(1 1,2 2)"},
        {"type": "foot", "wkt": "LINESTRING(2 2,3 3)"},
    ]
    results = [
        _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,3 3)", "distance": 3.0, "time": "00:15:00",
            "arc": [{"transport": "foot"}, {"transport": "bus"}],  # a real ride leg
            "legs": legs,
        }),
    ]
    data = _extract_data(results)
    by_mode = {r["mode"]: r for r in data["routes"]}
    assert by_mode["public_transport"]["legs"] == legs
    assert "legs" not in by_mode["foot"]


# --- parking -----------------------------------------------------------------

def _parking_entry(*spots) -> dict:
    return {"name": "service_search_near_gps_position", "result": _parking_search(*spots)}


def test_extract_parking_distance_sort_and_haversine():
    """The search has no free-spaces, so _extract_parking sorts by distance (Haversine from
    dest, not the envelope's own distance field) and leaves free_spaces None."""
    dest = {"lat": 43.770, "lng": 11.250}
    entry = _parking_entry(
        ("far", 11.270, 43.790),
        ("near", 11.2505, 43.7705),
    )
    parking = _extract_parking(entry, dest)
    assert [p["name"] for p in parking] == ["near", "far"]
    assert all(p["free_spaces"] is None for p in parking)
    assert all(isinstance(p["distance_km"], float) for p in parking)
    assert parking[0]["distance_km"] < parking[1]["distance_km"]


def test_extract_parking_caps_and_handles_empty():
    dest = {"lat": 43.77, "lng": 11.25}
    many = _parking_entry(*[(f"P{i}", 11.25, 43.77 + i * 0.001) for i in range(12)])
    assert len(_extract_parking(many, dest)) == mcp_tools.PARKING_MAX
    assert _extract_parking(_parking_entry(), dest) is None      # empty features
    assert _extract_parking({"name": "routing", "result": {}}, dest) is None  # unparseable entry


def test_parse_service_features_reads_nested_service_envelope():
    """Defensive: the parser reads the live result[1].Services nesting and a Service wrapper."""
    nested = {"Service": {"features": [
        {"geometry": {"coordinates": [11.25, 43.77]},
         "properties": {"serviceName": "Garage X", "serviceuri": "http://x"}},
    ]}}
    spots = mcp_tools.parse_service_features(nested)
    assert spots == [{"name": "Garage X", "lat": 43.77, "lng": 11.25, "uri": "http://x", "free_spaces": None}]
    # the live envelope shape (result -> [uris, {Services: {features}}])
    live = _parking_search(("P", 11.25, 43.77))
    assert mcp_tools.parse_service_features(live)[0]["name"] == "P"


def test_read_parking_realtime():
    """Latest free/total from a service_info_dev realtime binding; absent → None."""
    rt = mcp_tools.read_parking_realtime(_parking_realtime(31, 202))
    assert rt == {"free_spaces": 31, "total_spaces": 202}
    assert mcp_tools.read_parking_realtime({"realtime": {}}) == {"free_spaces": None, "total_spaces": None}
    assert mcp_tools.read_parking_realtime({"error": "x"}) == {"free_spaces": None, "total_spaces": None}


async def test_enrich_parking_fills_free_and_resorts(make_client, make_result):
    """_enrich_parking calls service_info_dev per spot, fills live free-spaces, and re-sorts
    so a farther parking with known free outranks a nearer one without realtime."""
    from snap4city_mobility_mcp.orchestrator import _enrich_parking
    spots = [
        {"name": "near_nodata", "uri": "http://near", "lat": 43.771, "lng": 11.250, "distance_km": 0.1, "free_spaces": None},
        {"name": "far_30free", "uri": "http://far", "lat": 43.780, "lng": 11.260, "distance_km": 1.2, "free_spaces": None},
    ]
    client = make_client([
        make_result(structured={"realtime": {}}),              # near → no realtime
        make_result(structured=_parking_realtime(30, 200)),    # far → 30 free
    ])
    out = await _enrich_parking(client, spots)
    assert out[0]["name"] == "far_30free" and out[0]["free_spaces"] == 30 and out[0]["total_spaces"] == 200
    assert out[1]["name"] == "near_nodata" and out[1]["free_spaces"] is None


async def test_execute_car_enriches_parking(make_client, make_result):
    """Car route → search car parks, then enrich each with live free-spaces via service_info_dev."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # routing (car)
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),  # parking search
        make_result(structured=_parking_realtime(42, 100)),          # P1 realtime enrichment
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert out["parking"][0]["name"] == "P1"
    assert out["parking"][0]["free_spaces"] == 42 and out["parking"][0]["total_spaces"] == 100
    # the enrichment call hit service_info_dev but is NOT in the audit (internal only)
    assert any(n == "service_info_dev" for n, _ in client.calls)
    assert not any(e["name"] == "service_info_dev" for e in out["tool_results"])


async def test_execute_foot_only_skips_parking(make_client, make_result):
    """The parking search is car-specific: a foot-only request must not call it."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # routing (foot)
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze", "mode": "foot"}
    out = await execute({"slots": slots}, client=client)
    assert not any(n == "service_search_near_gps_position" for n, _ in client.calls)
    assert out["parking"] == []


def test_extract_data_drops_foot_only_pt_when_real_foot_present():
    """PT degraded to a walking-only journey (no transit leg): with a genuine foot route in
    the same run it is a duplicate → dropped, no bus route, no error."""
    results = [
        _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00",
            "arc": [{"transport": "foot"}],  # walking only — not real PT
        }),
    ]
    data = _extract_data(results)
    assert [r["mode"] for r in data["routes"]] == ["foot"]
    assert data["duration"] == "00:20:00"
    assert "route_error" not in data


def test_extract_data_relabels_foot_only_pt_as_walk_when_only_result():
    """Explicit bus request degraded to walking (short trip, walking beats any bus, L39):
    surfaced as a foot route — drawable walking line + real walk duration — instead of
    being dropped; respond still gets the pt_degraded_to_foot hint and says so."""
    results = [
        _routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,1 1)", "distance": 1.328, "time": "0:16:00",
            "arc": [{"transport": "foot", "desc": "a piedi 1328 m", "distance": 1.328}],
        }),
    ]
    data = _extract_data(results)
    assert [r["mode"] for r in data["routes"]] == ["foot"]
    assert data["mode"] == "foot"
    assert data["distance_km"] == 1.328 and data["duration"] == "0:16:00"
    assert "route_error" not in data


def test_routing_hint_flags_foot_only_pt():
    foot_only = {"journey": {"routes": [{"arc": [{"transport": "foot"}]}]}}
    real_pt = {"journey": {"routes": [{"arc": [{"transport": "foot"}, {"transport": "bus"}]}]}}
    assert _routing_hint("public_transport", foot_only) == "pt_degraded_to_foot"
    assert _routing_hint("public_transport", real_pt) is None


# --- respond -----------------------------------------------------------------

async def test_respond_uses_llm_answer(make_llm):
    llm = make_llm([_text_response("The walking distance is about 1.83 km, ETA 15:59:09.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "from Duomo to Santa Croce on foot"}],
        "tool_results": [{"name": "routing", "result": {"journey": _journey()["journey"]}}],
        "unsupported": False,
    }
    out = await respond(state, llm=llm)
    response = out["response"]
    assert response["status"] == "success"
    assert "answer" not in response  # reply lives in messages[-1], not a custom field
    reply = response["messages"][-1]
    assert reply["role"] == "assistant"
    assert "1.83 km" in reply["content"]
    assert response["data"]["distance_km"] == 1.83


async def test_respond_injects_parking_into_data(make_llm):
    """A car route with parking (built in execute, carried on state) → data.parking present."""
    llm = make_llm([_text_response("In auto 3.5 km. Parcheggio P1 (20 liberi) a 80 m.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "in auto da A a B"}],
        "endpoints": {"origin": {"lat": 43.77, "lng": 11.24}, "destination": {"lat": 43.76, "lng": 11.26}},
        "tool_results": [
            {"name": "routing", "args": json.dumps({"routetype": "car"}),
             "result": {"journey": _journey()["journey"]}},
        ],
        "parking": [{"name": "P1", "lat": 43.76, "lng": 11.26, "uri": "http://p1",
                     "distance_km": 0.08, "free_spaces": 20, "total_spaces": 100}],
        "unsupported": False,
    }
    out = await respond(state, llm=llm)
    parking = out["response"]["data"]["parking"]
    assert parking[0]["name"] == "P1" and parking[0]["free_spaces"] == 20


async def test_respond_route_surfaces_mode_for_widget(make_llm):
    """A drawable route (wkt present) carries data.mode so the dashboard widget knows
    the vehicle to render; the value is the routetype the route was computed with."""
    llm = make_llm([_text_response("Percorso in auto, 1.83 km.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da Duomo a Santa Croce in auto"}],
        "tool_results": [
            {"name": "routing", "args": json.dumps({"routetype": "car"}),
             "result": {"journey": _journey()["journey"]}}
        ],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "car"},
    }
    out = await respond(state, llm=llm)
    assert out["response"]["data"]["mode"] == "car"


async def test_respond_no_mode_on_route_error(make_llm):
    """A route that failed (route_error, no wkt) is not drawable → no mode field added."""
    llm = make_llm([_text_response("Non sono riuscito a calcolare il percorso in auto.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B in auto"}],
        "tool_results": [{"name": "routing", "args": json.dumps({"routetype": "car"}),
                          "result": {"error": "empty routes list"}}],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "car"},
    }
    out = await respond(state, llm=llm)
    data = out["response"]["data"]
    assert "route_error" in data and "mode" not in data


async def test_respond_no_mode_outside_route():
    """Non-route intents (unsupported) must never get a mode field (rule 8)."""
    state = {
        "intent": "other",
        "messages": [{"role": "user", "content": "ciao"}],
        "tool_results": [],
        "unsupported": True,
    }
    out = await respond(state, llm=_RaisingLLM())
    assert "mode" not in out["response"]["data"]


async def test_respond_missing_place_asks_instead_of_unsupported():
    """L18: route intent with blank places → targeted ask, not the 'unsupported' pitch."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "voglio andare a piedi"}],
        "tool_results": [],
        "unsupported": True,
        "slots": {"intent": "route", "origin_text": "", "destination_text": ""},
    }
    out = await respond(state, llm=_RaisingLLM())
    reply = out["response"]["messages"][-1]["content"]
    assert "partenza" in reply and "destinazione" in reply
    assert "punto-punto" not in reply


async def test_respond_gps_covers_origin_asks_destination_only():
    """With GPS on state a blank origin is covered (it defaults to the user's position),
    so the ask targets ONLY the destination."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "voglio andare a piedi"}],
        "tool_results": [],
        "unsupported": True,
        "slots": {"intent": "route", "origin_text": "", "destination_text": ""},
        "user_gps": {"lat": 43.7731, "lng": 11.2558},
    }
    out = await respond(state, llm=_RaisingLLM())
    reply = out["response"]["messages"][-1]["content"]
    assert "destinazione" in reply
    assert "partenza" not in reply


def test_results_view_plain_errors_carry_no_hint():
    """The km4city-era ZTL/service-side hint taxonomy retired with the remote routing
    tool (L46): a What-If router failure is a plain error — respond's generic error rule
    phrases it, no hint key. The routetype still surfaces so respond can suggest another
    mode."""
    view = _results_view(
        [{"name": "routing", "args": json.dumps({"routetype": "car"}),
          "result": {"error": "whatif-router returned no car path"}}],
        unsupported=False,
    )
    assert "hint" not in view["results"][0]
    assert view["results"][0]["routetype"] == "car"


def test_results_view_carries_the_requested_departure():
    """The departure rides at the ROOT of the view, not on a routing item: it applies to the
    whole request. Absent when the user asked for no time, so the reply announces nothing."""
    entry = [{"name": "routing", "args": json.dumps({"routetype": "public_transport"}),
              "result": {"journey": {"routes": [{"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0}]}}}]
    assert _results_view(entry, unsupported=False, departure="18:00")["departure_time"] == "18:00"
    assert "departure_time" not in _results_view(entry, unsupported=False)


def test_results_view_parking_item_carries_the_live_free_spaces():
    """The LLM's car-park item speaks for the ENRICHED list (free-spaces from service_info_dev),
    not the raw search: the search envelope never carries occupancy (L33), so re-slimming it
    would show free_spaces: null on every spot and the reply could never say there are free
    spots. Without an enriched list, the raw search still speaks (availability unknown)."""
    entry = [{
        "name": "service_search_near_gps_position",
        "args": json.dumps({"categories": "Car_park"}),
        "result": _parking_search(("P1", 11.26, 43.76)),
    }]
    parking = [{"name": "P1", "free_spaces": 31, "total_spaces": 202, "distance_km": 0.08}]
    item = _results_view(entry, unsupported=False, parking=parking)["results"][0]
    assert item["categories"] == "Car_park"
    assert item["result"] == {"count": 1, "services": [{"name": "P1", "free_spaces": 31}]}
    raw = _results_view(entry, unsupported=False)["results"][0]
    assert raw["result"]["services"][0]["free_spaces"] is None


def test_results_view_routing_item_carries_only_distance_and_time():
    """The LLM's routing view is stripped to distance + duration — no legs, no streets. With
    nothing to enumerate, the reply is concise by construction (the Python detail block owns
    stops/streets); the model cannot narrate what it cannot see."""
    entry = [_routing_entry("public_transport", route={
        "wkt": "LINESTRING(0 0,3 3)", "distance": 4.38, "time": "0:47:03",
        "arc": [{"transport": "foot", "desc": "Via X", "distance": 0.4},
                {"transport": "bus", "transport_provider": "at", "line": "57",
                 "desc": "linea 57", "stops": [{"name": "S1", "time": "2026-01-01T08:00:00+01:00"}]}],
    })]
    item = _results_view(entry, unsupported=False)["results"][0]
    assert item["result"] == {"journey": {"distance_km": 4.38, "time": "0:47:03"}}
    assert item["routetype"] == "public_transport"


# --- _format_detail (deterministic single-mode step block, Python not LLM) ----

def _bus_arc_with_stops() -> list:
    """A realistic multimodal bus arc (walk -> ride -> walk) whose ride carries the FULL stop
    list with ISO times, in the shape mcp_server._bus_arcs emits for group_arc_legs."""
    stops = [
        {"name": "PORTE NUOVE", "time": "2026-07-13T17:38:00+02:00"},
        {"name": "PONTE MOSSE", "time": "2026-07-13T17:45:00+02:00"},
        {"name": "STAZIONE", "time": "2026-07-13T17:52:00+02:00"},
    ]
    return [
        {"transport": "foot", "transport_provider": None, "desc": "a piedi 453 m", "distance": 0.453},
        {"transport": "bus", "transport_provider": "at - Firenze urbano",
         "desc": "linea 56 da PORTE NUOVE", "line": "56", "headsign": "NICCOLO' DA TOLENTINO",
         "stops": stops, "start_datetime": stops[0]["time"]},
        {"transport": "bus", "transport_provider": "at - Firenze urbano",
         "desc": "a STAZIONE", "end_datetime": stops[-1]["time"]},
        {"transport": "foot", "transport_provider": None, "desc": "a piedi 200 m", "distance": 0.2},
    ]


def test_format_detail_bus_lists_every_fermata_with_local_time():
    """The bus block names the line/operator/headsign and lists EVERY stop (fermate) with its
    HH:MM (timeline) — not just board/alight — built exactly from the audit, never the LLM.
    No dates or seconds leak (L43)."""
    results = [_routing_entry("public_transport", route={
        "wkt": "LINESTRING(0 0,3 3)", "distance": 42.6, "time": "0:47:03", "arc": _bus_arc_with_stops(),
    })]
    route = {"mode": "public_transport", "distance_km": 42.6, "duration": "0:47:03"}
    block = _format_detail(results, route, departure="17:32")
    assert "Linea 56" in block and "at - Firenze urbano" in block
    assert "NICCOLO' DA TOLENTINO" in block
    for name in ("PORTE NUOVE", "PONTE MOSSE", "STAZIONE"):  # every fermata, not just endpoints
        assert name in block
    for hhmm in ("17:38", "17:45", "17:52"):  # the timeline
        assert hhmm in block
    assert "Partenza: 17:32" in block
    assert "Totale: 42.6 km · 0:47:03" in block
    # L43: no dates, no seconds, no ISO offset in the user-facing block.
    assert "2026-07-13" not in block and ":00+02:00" not in block and "T17:38" not in block


def test_format_detail_foot_lists_streets_merges_consecutive():
    """A foot/car block is a turn-by-turn street list: consecutive same-street arcs merge
    (distances summed), unnamed 'nd' arcs drop, no bus/stop concepts appear."""
    arc = [
        {"transport": "foot", "transport_provider": None, "desc": "Via Ricasoli", "distance": 0.3},
        {"transport": "foot", "transport_provider": None, "desc": "Via Ricasoli", "distance": 0.2},
        {"transport": "foot", "transport_provider": None, "desc": "nd", "distance": 0.1},
        {"transport": "foot", "transport_provider": None, "desc": "Borgo degli Albizi", "distance": 0.4},
    ]
    results = [_routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 0.9, "time": "0:12:00", "arc": arc})]
    route = {"mode": "foot", "distance_km": 0.9, "duration": "0:12:00"}
    block = _format_detail(results, route)
    assert "Via Ricasoli (0.5 km)" in block  # 0.3 + 0.2 merged
    assert "Borgo degli Albizi (0.4 km)" in block
    assert "nd" not in block and "Linea" not in block
    assert "Totale: 0.9 km · 0:12:00" in block


def test_format_detail_missing_arc_yields_totals_only():
    """Defensive: a route whose audit entry has no arc still yields the totals line, no raise."""
    results = [_routing_entry("car", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 1.83, "time": "00:23:18"})]
    route = {"mode": "car", "distance_km": 1.83, "duration": "00:23:18"}
    assert _format_detail(results, route) == "Totale: 1.83 km · 00:23:18"


async def test_respond_appends_detail_block_for_single_mode(make_llm):
    """A single-mode request with a drawable route → the concise LLM reply gets the
    deterministic step block appended (fermate + orari for bus)."""
    llm = make_llm([_text_response("In autobus, circa 42.6 km.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B in autobus"}],
        "tool_results": [_routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,3 3)", "distance": 42.6, "time": "0:47:03", "arc": _bus_arc_with_stops(),
        })],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "public_transport"},
    }
    out = await respond(state, llm=llm)
    reply = out["response"]["messages"][-1]["content"]
    assert "In autobus, circa 42.6 km." in reply  # the LLM lead survives
    assert "Linea 56" in reply and "PONTE MOSSE" in reply and "17:45" in reply  # block appended


async def test_respond_no_detail_block_for_multi_mode(make_llm):
    """The default multi-mode (no mode specified) reply is the LLM's concise comparison
    only — no step block appended."""
    llm = make_llm([_text_response("A piedi 2 km, in auto 3.5 km. Più veloce: in auto.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B"}],
        "tool_results": [
            _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
            _routing_entry("car", route={"wkt": "LINESTRING(0 0,2 2)", "distance": 3.5, "time": "00:08:00"}),
        ],
        "unsupported": False,
        "slots": {"intent": "route", "mode": ""},
    }
    out = await respond(state, llm=llm)
    reply = out["response"]["messages"][-1]["content"]
    assert reply == "A piedi 2 km, in auto 3.5 km. Più veloce: in auto."
    assert "Totale:" not in reply and "Partenza:" not in reply


# --- graph wiring ------------------------------------------------------------

def test_graph_compiles(make_client, make_llm):
    """StateGraph.compile() validates node/edge wiring — catches typos statically."""
    graph = _build_graph(make_client([]), make_llm([]))
    assert graph is not None
