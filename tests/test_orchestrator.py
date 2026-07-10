"""Unit tests for the deterministic graph nodes (orchestrator.py).

Lean core suite: one happy path per flow + one guard per documented lesson
(L8 car-ZTL, L17 POI ranking, L18 missing-slot ask, L19 service-error vs ZTL).
"""
import json

from snap4city_mobility_mcp import mcp_tools
from snap4city_mobility_mcp.llm import Llama4Error
from snap4city_mobility_mcp.orchestrator import (
    _EXTRACT_SLOTS_SCHEMA,
    _build_graph,
    _extract_data,
    _extract_parking,
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
    """Server `routing` payload shape (journey + km4city success envelope)."""
    if routes is None:
        routes = [{"wkt": "LINESTRING(1 2,3 4)", "distance": distance, "eta": "15:59:09", "time": "00:23:18"}]
    return {"journey": {"routes": routes, "source_node": "s", "destination_node": "d"},
            "response": {"error_code": "0"}}


class _RaisingLLM:
    """LLM double whose achat always raises — exercises respond's template fallback."""

    async def achat(self, *args, **kwargs):
        raise Llama4Error("boom")


# --- understand --------------------------------------------------------------

async def test_understand_parses_slots(make_llm):
    llm = make_llm([_slots_response(
        '{"request_type":"journey","origin_text":"Duomo",'
        '"destination_text":"Santa Croce","destination_category":"","mode":"foot_shortest"}'
    )])
    out = await understand(
        {"messages": [{"role": "user", "content": "from Duomo to Santa Croce on foot"}]}, llm=llm
    )
    assert out["intent"] == "route"  # journey folded into the internal route intent
    assert out["slots"]["origin_text"] == "Duomo"
    assert out["slots"]["mode"] == "foot_shortest"


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
    }
    assert "" in params["properties"]["mode"]["enum"]  # required mode needs an 'absent' value


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
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "foot_shortest"}
    out = await execute({"slots": slots}, client=client)
    assert out["unsupported"] is False
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "address_search_location", "routing"]
    # routing got [lng,lat] mapped to the right lat/lng fields
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["startlatitude"] == 43.77 and route_args["startlongitude"] == 11.24
    assert route_args["routetype"] == "foot_shortest"


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
    slots = {"intent": "route", "origin_text": "", "destination_text": "Santa Croce", "mode": "foot_shortest"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    assert out["unsupported"] is False
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["coordinates_to_address", "address_search_location", "routing"]
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["startlatitude"] == 43.7731 and route_args["startlongitude"] == 11.2558
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
    slots = {"intent": "route", "origin_text": "", "destination_text": "Santa Croce", "mode": "foot_shortest"}
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
             "destination_category": "Pharmacy", "mode": "foot_shortest"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "service_search_near_gps_position", "routing"]
    near_args = json.loads(out["tool_results"][1]["args"])
    assert near_args["categories"] == "Pharmacy"
    assert near_args["latitude"] == 43.7731 and near_args["longitude"] == 11.2558
    assert near_args["maxdistance"] == 0.5  # first rung of the widening ladder
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["endlatitude"] == 43.7735 and route_args["endlongitude"] == 11.2546
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
             "destination_text": "via Pisana 166", "mode": "foot_shortest"}
    out = await execute({"slots": slots}, client=client)
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["endlatitude"] == 43.7747 and route_args["endlongitude"] == 11.2216


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
             "destination_text": "via Barna 7, Firenze", "mode": "foot_shortest"}
    out = await execute({"slots": slots, "user_gps": {"lat": 45.5308, "lng": 10.1828}}, client=client)
    assert out["unsupported"] is False
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "address_search_location", "routing"]
    assert all("error" not in e["result"] for e in out["tool_results"][:2])
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["startlatitude"] == 43.77 and route_args["endlatitude"] == 43.76


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
             "destination_category": "Pharmacy", "mode": "foot_shortest"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    assert len([n for n, _ in client.calls if n == "service_search_near_gps_position"]) == 3
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "service_search_near_gps_position",
                     "address_search_location", "routing"]
    assert out["endpoints"]["destination"] == {"lat": 43.76, "lng": 11.26}


async def test_execute_foot_quiet_falls_back_to_foot_shortest(make_client, make_result):
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey(routes=[])),                 # foot_quiet → empty routes error
        make_result(structured=_journey()),                          # foot_shortest → success
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "foot_quiet"}
    out = await execute({"slots": slots}, client=client)
    routings = [e for e in out["tool_results"] if e["name"] == "routing"]
    assert len(routings) == 2
    assert json.loads(routings[0]["args"])["routetype"] == "foot_quiet"
    assert json.loads(routings[1]["args"])["routetype"] == "foot_shortest"
    # last routing succeeded → widget data has the journey
    assert _extract_data(out["tool_results"])["distance_km"] == 1.83


async def test_execute_car_bare_error_burns_ladder_only(make_client, make_result, monkeypatch):
    """L8: the stable car-ZTL wrapper bug returns bare {"error": ""} — the stale
    ladder runs its 3 attempts but NO foot-style profile fallback follows."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mcp_tools.asyncio, "sleep", _noop)
    stale = {"error": ""}
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=stale),                               # car #1
        make_result(structured=stale),                               # car #2
        make_result(structured=stale),                               # car #3
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),  # parking search (fetched concurrently, then discarded)
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert len([n for n, _ in client.calls if n == "routing"]) == 3  # ladder only, no 4th probe
    assert "route_error" in _extract_data(out["tool_results"])
    # car route failed → nowhere to drive, so parking is dropped (fetched concurrently but not
    # surfaced) and never enriched (no service_info_dev call).
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
        _routing_entry("foot_shortest", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("car", route={"wkt": "LINESTRING(0 0,2 2)", "distance": 3.5, "time": "00:08:00"}),
    ]
    data = _extract_data(results)
    assert [r["mode"] for r in data["routes"]] == ["car", "foot_shortest"]
    assert data["mode"] == "car" and data["wkt"] == "LINESTRING(0 0,2 2)"


def test_extract_data_drops_empty_car_keeps_foot():
    """car empty (ZTL / too close) → a single foot route, no route_error."""
    results = [
        _routing_entry("foot_shortest", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("car", error="no route found (empty routes list)"),
    ]
    data = _extract_data(results)
    assert len(data["routes"]) == 1 and data["routes"][0]["mode"] == "foot_shortest"
    assert "route_error" not in data


def test_extract_data_all_fail_reports_earliest_error():
    results = [
        _routing_entry("foot_shortest", error="empty response from routing service"),
        _routing_entry("car", error="no route found (empty routes list)"),
    ]
    data = _extract_data(results)
    assert data.get("route_error") == "empty response from routing service"  # the user's first mode
    assert "routes" not in data


async def test_execute_unspecified_mode_routes_foot_car_only(make_client, make_result):
    """Empty mode → execute routes walking and driving only. Public transport is NOT run by
    default (the What-If bus router is ~25 s); it runs only on an explicit public_transport mode."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # foot_shortest
        make_result(structured=_journey()),                          # car
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),  # parking search (car in modes)
        make_result(structured=_parking_realtime(5, 50)),            # P1 realtime enrichment
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": ""}
    out = await execute({"slots": slots}, client=client)
    routetypes = [json.loads(e["args"])["routetype"] for e in out["tool_results"] if e["name"] == "routing"]
    assert routetypes == ["foot_shortest", "car"]  # no public_transport by default
    # car is among the modes → the parking entry is appended after all routing entries.
    assert out["tool_results"][-1]["name"] == "service_search_near_gps_position"
    assert out["parking"][0]["free_spaces"] == 5


async def test_execute_public_transport_routes_via_bus_route(make_client, make_result):
    """public_transport routes through the local `bus_route` tool (What-If router), NOT the
    transit-blind MCP routing; a returned bus journey becomes a drawable PT route."""
    bus_journey = {"journey": {"routes": [{
        "wkt": "LINESTRING(0 0,3 3)", "distance": 4.38,
        "arc": [{"transport": "bus", "transport_provider": "at - Firenze urbano",
                 "desc": "linea 57 da PORTE NUOVE BELFIORE", "line": "57"}],
    }]}}
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=bus_journey),                         # bus_route (local tool)
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze", "mode": "public_transport"}
    out = await execute({"slots": slots}, client=client)
    # PT went to bus_route, not MCP routing; the audit entry is still tagged public_transport.
    assert client.calls[-1][0] == "bus_route"
    routings = [e for e in out["tool_results"] if e["name"] == "routing"]
    assert len(routings) == 1 and json.loads(routings[0]["args"])["routetype"] == "public_transport"
    # the bus journey (real ride leg) survives _extract_data as a drawable PT route.
    data = _extract_data(out["tool_results"])
    assert data["routes"][0]["mode"] == "public_transport"
    assert data["wkt"] == "LINESTRING(0 0,3 3)" and data["distance_km"] == 4.38


def test_extract_data_collects_three_modes():
    """foot+car+real-PT all succeed → routes has all three, keyed to distinct vehicles."""
    results = [
        _routing_entry("foot_shortest", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("car", route={"wkt": "LINESTRING(0 0,2 2)", "distance": 3.5, "time": "00:08:00"}),
        _routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,3 3)", "distance": 3.0, "time": "00:15:00",
            "arc": [{"transport": "foot"}, {"transport": "bus"}],  # a real ride leg
        }),
    ]
    data = _extract_data(results)
    assert {r["mode"] for r in data["routes"]} == {"foot_shortest", "car", "public_transport"}
    assert len(data["routes"]) == 3


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
    assert mcp_tools.parse_service_features({"error": "boom"}) == []
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
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze", "mode": "foot_shortest"}
    out = await execute({"slots": slots}, client=client)
    assert not any(n == "service_search_near_gps_position" for n, _ in client.calls)
    assert out["parking"] == []


def test_extract_data_drops_foot_only_pt_when_real_foot_present():
    """PT degraded to a walking-only journey (no transit leg): with a genuine foot route in
    the same run it is a duplicate → dropped, no bus route, no error."""
    results = [
        _routing_entry("foot_shortest", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00",
            "arc": [{"transport": "foot"}],  # walking only — not real PT
        }),
    ]
    data = _extract_data(results)
    assert [r["mode"] for r in data["routes"]] == ["foot_shortest"]
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
    assert [r["mode"] for r in data["routes"]] == ["foot_shortest"]
    assert data["mode"] == "foot_shortest"
    assert data["distance_km"] == 1.328 and data["duration"] == "0:16:00"
    assert "route_error" not in data


def test_routing_hint_zero_distance_is_service_side():
    """A shape-D zero-distance car route is a server data bug, not a ZTL: the hint must
    steer respond to 'service problem, try foot/later', never to the ZTL phrasing."""
    err = {"error": "routing failed: zero-distance route (server-side data bug)"}
    assert _routing_hint("car", err) == "service_empty_try_foot_or_later"


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


def test_results_view_hint_separates_service_error_from_ztl():
    """L19: a server-side empty (car/PT broken) must NOT be narrated as a ZTL/pedestrian
    restriction — that misled the user and the referente (a drivable, non-ZTL destination got
    blamed on a ZTL). The judgement now lives in _results_view as a deterministic `hint`
    (not a respond-prompt error-string match): ZTL phrasing is reserved for the genuine
    empty-routes error on a car/PT mode; the service-side empty gets a neutral hint."""
    car_ztl = _results_view(
        [{"name": "routing", "args": json.dumps({"routetype": "car"}),
          "result": {"error": "no route found (empty routes list)"}}],
        unsupported=False,
    )
    assert car_ztl["results"][0]["hint"] == "car_pt_blocked_try_foot"

    service = _results_view(
        [{"name": "routing", "args": json.dumps({"routetype": "car"}),
          "result": {"error": "routing failed: empty response from routing service (mode=car)"}}],
        unsupported=False,
    )
    assert service["results"][0]["hint"] == "service_empty_try_foot_or_later"

    # An unclassified routing error carries no hint — respond's generic error rule handles it.
    other = _results_view(
        [{"name": "routing", "args": json.dumps({"routetype": "car"}),
          "result": {"error": "routing call failed: TimeoutError: x"}}],
        unsupported=False,
    )
    assert "hint" not in other["results"][0]


# --- graph wiring ------------------------------------------------------------

def test_graph_compiles(make_client, make_llm):
    """StateGraph.compile() validates node/edge wiring — catches typos statically."""
    graph = _build_graph(make_client([]), make_llm([]))
    assert graph is not None
