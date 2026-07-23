"""Unit tests for the deterministic graph nodes (orchestrator.py).

Lean core suite: one happy path per flow + one guard per documented lesson
(L17 POI ranking, L18 missing-slot ask, L39 PT walking degrade, L43 dest anchoring).
"""
import json
from datetime import datetime

from snap4city_mobility_mcp import mcp_tools
from snap4city_mobility_mcp.geo import fmt_linestring
from snap4city_mobility_mcp.orchestrator import (
    _EXTRACT_SLOTS_SCHEMA,
    ROME,
    SERVICES_MAX_ANCHORS,
    SERVICES_RADIUS_KM,
    _build_graph,
    _extract_data,
    _extract_parking,
    _format_detail,
    _parse_departure,
    _pick_coord,
    _request_to_intent,
    _routing_hint,
    _sample_polyline,
    _service_anchors,
    execute,
    respond,
    understand,
)


def _parking_search(*spots) -> dict:
    """service_search_near_gps_position envelope from (name, lng, lat) tuples, in the LIVE
    shape probe_parking.py captured: {"result": [[uris], {"Services": {"features": [...]}}]}.
    The search carries NO free-spaces (parking is pins-only — none is ever fetched)."""
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


def _feature_collection(lng: float, lat: float) -> dict:
    """Minimal GeoJSON FeatureCollection (server `address_search_location` shape)."""
    return {
        "type": "FeatureCollection",
        "features": [{"geometry": {"coordinates": [lng, lat]}, "properties": {"city": "FIRENZE"}}],
    }


def _fc_with_addresses(*entries, city="FIRENZE") -> dict:
    """FeatureCollection from (lng, lat, address[, civic]) tuples (address may be None);
    a 4th element marks a /location/ house-number hit (serviceType StreetNumber)."""
    feats = []
    for e in entries:
        props = {"address": e[2], "city": city}
        if len(e) > 3:
            props.update(civic=e[3], serviceType="StreetNumber")
        feats.append({"geometry": {"coordinates": [e[0], e[1]]}, "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def _journey(distance=1.83, routes=None) -> dict:
    """The local `route` tool's payload shape: {"journey": {"routes": [...]}}."""
    if routes is None:
        routes = [{"wkt": "LINESTRING(1 2,3 4)", "distance": distance, "time": "00:23:18"}]
    return {"journey": {"routes": routes}}


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


async def test_understand_trims_history_to_recent_messages(make_llm):
    """The slot-extraction prompt is bounded: only the last 8 user/assistant messages ride
    along (L54 — the gateway is minute-slow on bad days; unbounded history amplifies it)."""
    llm = make_llm([_slots_response('{"request_type":"other"}')])
    msgs = [{"role": "user", "content": "m" + str(i)} for i in range(12)]
    await understand({"messages": msgs}, llm=llm)
    sent = llm.calls[0]["messages"]
    assert sent[0]["role"] == "system" and len(sent) == 1 + 8
    assert sent[1]["content"] == "m4" and sent[-1]["content"] == "m11"


def test_request_to_intent():
    assert _request_to_intent({"request_type": "journey"}) == "route"
    assert _request_to_intent({"request_type": "nearby"}) == "nearby"
    assert _request_to_intent({"request_type": "other"}) == "other"
    assert _request_to_intent({}) == "other"


def test_extract_slots_schema_requires_all_fields():
    """Llama4 only fills required params — a real run dropped destination_text when
    it was optional. All slots must stay required ('' marks an absent one)."""
    params = _EXTRACT_SLOTS_SCHEMA["function"]["parameters"]
    assert set(params["required"]) == {
        "request_type", "origin_text", "destination_text", "destination_category",
        "services_category", "mode", "departure_time",
    }
    assert "" in params["properties"]["mode"]["enum"]  # required mode needs an 'absent' value
    assert set(params["properties"]["request_type"]["enum"]) == {"journey", "nearby", "other"}


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


def test_pick_coord_civic_exact_beats_anchor_nearest():
    """Regression for 'via Laura 11' (L52): the anchor sat right on a POI named 'LAURA'
    and anchor-nearest buried the civic-exact StreetNumber hit the server ranked first.
    With a house number in the search, the civic-exact feature must win — with and
    without an anchor."""
    fc = _fc_with_addresses(
        (11.2640, 43.7790, "VIA LAURA"),            # street-level entry
        (11.2616, 43.7821, "LAURA"),                # name-only POI, nearest to the anchor
        (11.2647, 43.7784, "VIA LAURA", "11"),      # civic-exact StreetNumber
    )
    anchor = {"lat": 43.7822, "lng": 11.2615}       # = the resolved origin, on the POI
    assert _pick_coord(fc, "via Laura 11, Firenze", gps=anchor) == [11.2647, 43.7784]
    assert _pick_coord(fc, "via Laura 11, Firenze") == [11.2647, 43.7784]


def test_pick_coord_number_prefers_street_over_poi_without_civic():
    """No civic feature in the pool but the search has a number: street-shaped labels
    (road-type word) beat name-only POIs — the 'LAURA' bug without StreetNumber data."""
    fc = _fc_with_addresses(
        (11.2640, 43.7790, "VIA LAURA"),
        (11.2616, 43.7821, "LAURA"),
    )
    anchor = {"lat": 43.7822, "lng": 11.2615}
    assert _pick_coord(fc, "via Laura 11, Firenze", gps=anchor) == [11.2640, 43.7790]


def test_pick_coord_civic_mismatch_keeps_anchor_fallback():
    """A civic in the pool that does NOT match the searched number is no shortcut:
    the street layer applies and anchor-nearest picks among street features as today."""
    fc = _fc_with_addresses(
        (11.2640, 43.7790, "VIA LAURA", "7"),
        (11.2648, 43.7783, "VIA LAURA"),
    )
    anchor = {"lat": 43.7784, "lng": 11.2648}
    assert _pick_coord(fc, "via Laura 11, Firenze", gps=anchor) == [11.2648, 43.7783]


def test_pick_coord_numberless_query_keeps_anchor_nearest():
    """No house number in the search → the civic ladder must not fire at all: the
    anchor-nearest candidate (here a name-only POI) still wins, exactly as before."""
    fc = _fc_with_addresses(
        (11.2640, 43.7790, "VIA LAURA"),
        (11.2616, 43.7821, "LAURA"),
    )
    anchor = {"lat": 43.7822, "lng": 11.2615}
    assert _pick_coord(fc, "via Laura, Firenze", gps=anchor) == [11.2616, 43.7821]


def test_pick_coord_civic_is_last_number_token():
    """Numbered street names keep working: in 'via 20 settembre 5' the civic is the
    LAST standalone number (5), not the street's own 20."""
    fc = _fc_with_addresses(
        (11.2500, 43.7800, "VIA 20 SETTEMBRE", "20"),
        (11.2510, 43.7810, "VIA 20 SETTEMBRE", "5"),
    )
    assert _pick_coord(fc, "via 20 settembre 5, Firenze") == [11.2510, 43.7810]


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


async def test_execute_surfaces_resolved_endpoint_labels(make_client, make_result):
    """execute exposes the resolved endpoint labels (title-cased address + civic + city) so
    the reply can echo 'Da ORIGIN a DESTINATION'."""
    client = make_client([
        make_result(structured=_fc_with_addresses((11.275, 43.774, "VIA CIRO MENOTTI", "19"))),  # origin
        make_result(structured=_fc_with_addresses((11.264, 43.786, "VIA FAENTINA", "16"))),       # dest
        make_result(structured=_journey()),                                                        # routing
    ])
    slots = {"intent": "route", "origin_text": "via ciro menotti 19, Firenze",
             "destination_text": "via faentina 16, Firenze", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert out["labels"]["origin"] == "Via Ciro Menotti 19, Firenze"
    assert out["labels"]["destination"] == "Via Faentina 16, Firenze"


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
    reverse-geocoded once (success → audited) so respond can say 'dalla tua posizione'.
    Its municipality then augments the no-city destination (anchor-city re-query, L49):
    the plain result only has another town's hit, the augmented one wins and is the
    deciding audited call."""
    rev = {"result": [{"number": "3", "address": "VIA ZARA", "municipality": "FIRENZE"}]}
    client = make_client([
        make_result(structured=rev),                                  # coordinates_to_address
        make_result(structured=_fc_with_addresses((11.0948, 43.8805, "SANTA CROCE"), city="PRATO")),  # dest plain (address pass)
        make_result(structured={"type": "FeatureCollection", "features": []}),  # dest plain POI pass (no city named)
        make_result(structured=_fc_with_addresses((11.26, 43.76, "PIAZZA SANTA CROCE"))),  # augmented ", FIRENZE" pass
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "", "destination_text": "Santa Croce", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    assert out["unsupported"] is False
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["coordinates_to_address", "address_search_location", "routing"]
    geocode_args = json.loads(out["tool_results"][1]["args"])
    assert geocode_args["search"] == "Santa Croce, FIRENZE"  # the deciding call is the augmented one
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["start_latitude"] == 43.7731 and route_args["start_longitude"] == 11.2558
    assert route_args["end_latitude"] == 43.76 and route_args["end_longitude"] == 11.26  # NOT Prato
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
    "via Pisana 166" from a Florence origin got routed to Lucca's VIA PISANA, 65 km).
    Here the anchor-city re-query (fed by the origin feature's city) comes back empty,
    so the anchor-nearest pick over the plain result is the safety net."""
    dest_fc = _fc_with_addresses(
        (10.4901, 43.8424, "VIA PISANA"),  # Lucca — the server's first hit
        (11.2216, 43.7747, "VIA PISANA"),  # Florence — nearest to the origin
    )
    empty = {"type": "FeatureCollection", "features": []}
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # origin (city-named, 1 call)
        make_result(structured=dest_fc),                             # dest plain address pass
        make_result(structured=empty),                               # dest plain POI pass
        make_result(structured=empty),                               # augmented address pass (miss)
        make_result(structured=empty),                               # augmented POI pass (miss)
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


async def test_execute_augmented_geocode_rejects_road_word_noise(make_client, make_result):
    """The anchor-city re-query must not hijack a famous place in ANOTHER town: an
    augmented candidate sharing only road-type words with the user's text ("PIAZZA ..."
    for "piazza dei Miracoli") is noise (_signal_subset), so the plain result stands
    and the deciding audited call is the plain one (live-probed 2026-07-16)."""
    rev = {"result": [{"address": "VIA ZARA", "municipality": "FIRENZE"}]}
    client = make_client([
        make_result(structured=rev),                                  # coordinates_to_address
        make_result(structured=_fc_with_addresses((10.3966, 43.7231, "PIAZZA DEI MIRACOLI"), city="PISA")),  # dest plain
        make_result(structured={"type": "FeatureCollection", "features": []}),  # dest plain POI pass
        make_result(structured=_fc_with_addresses((11.25, 43.77, "PIAZZA DELLA REPUBBLICA"))),  # augmented: road-word-only overlap
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "", "destination_text": "piazza dei Miracoli", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    geocode_args = json.loads(out["tool_results"][1]["args"])
    assert geocode_args["search"] == "piazza dei Miracoli"  # plain call decided, not the augmented one
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["end_latitude"] == 43.7231 and route_args["end_longitude"] == 10.3966  # Pisa kept


async def test_execute_named_city_skips_augmentation(make_client, make_result):
    """A city the user named still dominates: the named-city subset short-circuits the
    anchor-city re-query — no augmented call goes out at all."""
    rev = {"result": [{"address": "VIA ZARA", "municipality": "FIRENZE"}]}
    client = make_client([
        make_result(structured=rev),                                  # coordinates_to_address
        make_result(structured=_fc_with_addresses((10.4018, 43.7160, "VIA ROMA"), city="PISA")),  # dest: named-city hit
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "", "destination_text": "via Roma, Pisa", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    searches = [args["search"] for name, args in client.calls if name == "address_search_location"]
    assert searches == ["via Roma, Pisa"]  # one pass, never "..., FIRENZE"
    route_args = json.loads(out["tool_results"][-1]["args"])
    assert route_args["end_latitude"] == 43.7160 and route_args["end_longitude"] == 10.4018


async def test_execute_origin_no_city_augments_via_lazy_reverse_geocode(make_client, make_result):
    """An origin TYPED without a city rides the same anchor-city re-query: the GPS is
    reverse-geocoded lazily for its municipality only (NOT audited — an entry would make
    respond claim the trip starts from the user's position), and the resolved origin's
    city then anchors the destination's own augmentation."""
    rev = {"result": [{"address": "VIA ZARA", "municipality": "FIRENZE"}]}
    empty = {"type": "FeatureCollection", "features": []}
    client = make_client([
        make_result(structured=_fc_with_addresses((11.0948, 43.8805, "VIA ROMA"), city="PRATO")),  # origin plain
        make_result(structured=empty),                                # origin plain POI pass
        make_result(structured=rev),                                  # lazy coordinates_to_address
        make_result(structured=_fc_with_addresses((11.25, 43.77, "VIA ROMA"))),  # origin augmented ", FIRENZE"
        make_result(structured=_fc_with_addresses((11.0946, 43.8803, "SANTA CROCE"), city="PRATO")),  # dest plain
        make_result(structured=empty),                                # dest plain POI pass
        make_result(structured=_fc_with_addresses((11.26, 43.76, "PIAZZA SANTA CROCE"))),  # dest augmented ", FIRENZE"
        make_result(structured=_journey()),                           # routing
    ])
    slots = {"intent": "route", "origin_text": "via Roma", "destination_text": "Santa Croce", "mode": "foot"}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "address_search_location", "routing"]  # rev NOT audited
    assert len([n for n, _ in client.calls if n == "coordinates_to_address"]) == 1  # called once, lazily
    origin_args = json.loads(out["tool_results"][0]["args"])
    dest_args = json.loads(out["tool_results"][1]["args"])
    assert origin_args["search"] == "via Roma, FIRENZE"
    assert dest_args["search"] == "Santa Croce, FIRENZE"
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
        make_result(structured=_fc_with_addresses((11.26, 43.76, "FARMACIA COMUNALE"))),  # augmented ", FIRENZE" pass
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


async def test_execute_parking_ladder_widens_on_empty(make_client, make_result):
    """An empty first parking rung re-searches wider (0.5 then 1 km — suburban destinations
    often have no car park nearby); the pins still come out. Parking is pins-only, so no
    search entry is audited. A first-rung hit keeps costing a single call (other tests)."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # route (car)
        make_result(structured=_parking_search()),                   # parking rung 0.5 km: empty
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),  # rung 1.0 km: hit
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    near = [a for n, a in client.calls if n == "service_search_near_gps_position"]
    assert [a["maxdistance"] for a in near] == [0.5, 1.0]
    assert out["parking"][0]["name"] == "P1" and out["parking"][0]["free_spaces"] is None
    # Parking is pins-only: the search entry is never audited (not in the LLM view).
    assert not [e for e in out["tool_results"] if e["name"] == "service_search_near_gps_position"]


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
    ])
    slots = {"intent": "route", "origin_text": "Duomo, Firenze", "destination_text": "Santa Croce, Firenze", "mode": ""}
    out = await execute({"slots": slots}, client=client)
    routetypes = [json.loads(e["args"])["routetype"] for e in out["tool_results"] if e["name"] == "routing"]
    assert routetypes == ["foot", "car", "public_transport"]
    # car is among the modes → parking pins come out, but pins-only: never audited.
    assert not [e for e in out["tool_results"] if e["name"] == "service_search_near_gps_position"]
    assert out["parking"][0]["name"] == "P1" and out["parking"][0]["free_spaces"] is None


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


async def test_execute_car_finds_parking_pins(make_client, make_result):
    """Car route → search car parks near the destination for map pins. NOT audited (pins-only,
    absent from the LLM view) and NOT enriched (no service_info_dev call): free_spaces stays
    None — the widget resolves each pin's own live availability."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # routing (car)
        make_result(structured=_parking_search(("P1", 11.26, 43.76))),  # parking search
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert out["parking"][0]["name"] == "P1" and out["parking"][0]["free_spaces"] is None
    assert not any(n == "service_info_dev" for n, _ in client.calls)
    assert not [e for e in out["tool_results"] if e["name"] == "service_search_near_gps_position"]


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
    assert out["services"] == {}  # no services_category asked → zero along-route searches


# --- along-route services (referente item 3, L53) ------------------------------

def test_sample_polyline_spacing_endpoints_and_anchor_cap():
    # 0.001 deg of latitude ≈ 111 m; spacing 0.4 km emits roughly every 4th vertex.
    pts = [(11.0, 43.0 + i * 0.001) for i in range(40)]
    out = _sample_polyline(pts, 0.4)
    assert out[0] == pts[0] and out[-1] == pts[-1]
    assert 5 < len(out) < len(pts)
    # _service_anchors then thins to the cap, keeping the endpoints.
    anchors = _service_anchors("foot", {"wkt": fmt_linestring(pts)})
    assert len(anchors) <= SERVICES_MAX_ANCHORS
    assert anchors[0] == pts[0] and anchors[-1] == pts[-1]


def test_service_anchors_bus_takes_stops_and_walk_legs_only():
    # PT rule: a bus leg contributes only its board/alight vertices (never the mid-ride
    # stop), foot legs sample like a walk; shared boundary vertices dedupe.
    a, b = (11.0, 43.0), (11.001, 43.0)
    mid, c = (11.01, 43.0), (11.02, 43.0)
    d = (11.021, 43.0)
    first = {
        "wkt": fmt_linestring([a, b, mid, c, d]),
        "legs": [
            {"type": "foot", "wkt": fmt_linestring([a, b])},
            {"type": "bus", "wkt": fmt_linestring([b, mid, c])},
            {"type": "foot", "wkt": fmt_linestring([c, d])},
        ],
    }
    anchors = _service_anchors("public_transport", first)
    assert anchors == [a, b, c, d]  # mid-ride stop excluded, boundaries deduped


async def test_execute_services_searched_along_route(make_client, make_result):
    """services_category set → one near-search per sampled anchor (NOT audited), results
    deduped by uri across anchors and keyed by mode in state['services']."""
    # Route geometry with 2 anchors (single ~1.9 km hop ≥ spacing → first + last vertex).
    wkt = "LINESTRING (11.24 43.77, 11.26 43.76)"
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey(routes=[{"wkt": wkt, "distance": 1.9, "time": "00:23:00"}])),
        # anchor 1: FarmaciaA; anchor 2: FarmaciaA again (dedup) + FarmaciaB
        make_result(structured=_parking_search(("FarmaciaA", 11.245, 43.768))),
        make_result(structured=_parking_search(("FarmaciaA", 11.245, 43.768), ("FarmaciaB", 11.259, 43.761))),
    ])
    slots = {"intent": "route", "origin_text": "A, Firenze", "destination_text": "B, Firenze",
             "mode": "foot", "services_category": "Pharmacy"}
    out = await execute({"slots": slots}, client=client)
    near = [a for n, a in client.calls if n == "service_search_near_gps_position"]
    assert len(near) == 2
    assert all(c["categories"] == "Pharmacy" and c["maxdistance"] == SERVICES_RADIUS_KM for c in near)
    spots = out["services"]["foot"]
    # Deduped by uri (FarmaciaA appears at both anchors → once) and sorted by distance to
    # the nearest anchor: B sits ~140 m from the last anchor, A ~460 m from the first.
    assert [s["name"] for s in spots] == ["FarmaciaB", "FarmaciaA"]
    assert spots[0]["distance_km"] < spots[1]["distance_km"]
    assert all(set(s) == {"name", "lat", "lng", "uri", "distance_km"} for s in spots)
    # Anchor searches are internal (like parking enrichment): none in the audit.
    assert not any(e["name"] == "service_search_near_gps_position" for e in out["tool_results"])


async def test_respond_attaches_services_per_route_flags_not_names():
    """Each drawable route gets ITS mode's list as routes[i].services (the map pins). The user
    asked to see them, so the deterministic reply ACKNOWLEDGES them on the map — but the service
    names/coords ride ONLY in routes[i].services (the pins are the answer), never in the reply."""
    svc_foot = [{"name": "FarmaciaA", "lat": 43.768, "lng": 11.245, "uri": "http://a", "distance_km": 0.05}]
    svc_car = [{"name": "FarmaciaB", "lat": 43.761, "lng": 11.259, "uri": "http://b", "distance_km": 0.1}]
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B con le farmacie lungo il percorso"}],
        "slots": {"services_category": "Pharmacy"},
        "endpoints": {"origin": {"lat": 43.77, "lng": 11.24}, "destination": {"lat": 43.76, "lng": 11.26}},
        "tool_results": [
            _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
            _routing_entry("car", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.5, "time": "00:08:00"}),
        ],
        "services": {"foot": svc_foot, "car": svc_car},
    }
    out = (await respond(state))["response"]
    by_mode = {r["mode"]: r for r in out["data"]["routes"]}
    assert by_mode["foot"]["services"] == svc_foot
    assert by_mode["car"]["services"] == svc_car
    reply = out["messages"][-1]["content"]
    assert "segnato le farmacie" in reply  # the deterministic reply acknowledges the pins
    assert "FarmaciaA" not in reply and "FarmaciaB" not in reply  # names ride only in the pins


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

async def test_respond_composes_route_answer():
    """respond composes the Italian reply from the route (no LLM): the mode's distance and
    duration land in messages[-1].content; there is no custom top-level `answer` field."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "from Duomo to Santa Croce on foot"}],
        "tool_results": [_routing_entry("foot", route=_journey()["journey"]["routes"][0])],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "foot"},
    }
    out = await respond(state)
    response = out["response"]
    assert response["status"] == "success"
    assert "answer" not in response  # reply lives in messages[-1], not a custom field
    reply = response["messages"][-1]
    assert reply["role"] == "assistant"
    # natural phrasing: km to 1 decimal, duration rounded to the minute (exact figures stay
    # in the detail block); data keeps the precise distance.
    assert "1.8 km" in reply["content"] and "circa 23 minuti" in reply["content"]
    assert response["data"]["distance_km"] == 1.83


async def test_respond_injects_parking_into_data():
    """A car route with parking (built in execute, carried on state) → data.parking present."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "in auto da A a B"}],
        "endpoints": {"origin": {"lat": 43.77, "lng": 11.24}, "destination": {"lat": 43.76, "lng": 11.26}},
        "tool_results": [
            _routing_entry("car", route=_journey()["journey"]["routes"][0]),
        ],
        "parking": [{"name": "P1", "lat": 43.76, "lng": 11.26, "uri": "http://p1",
                     "distance_km": 0.08, "free_spaces": 20, "total_spaces": 100}],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "car"},
    }
    out = await respond(state)
    parking = out["response"]["data"]["parking"]
    assert parking[0]["name"] == "P1" and parking[0]["free_spaces"] == 20


async def test_respond_along_route_services_flag_false_when_none_found():
    """The user asked for a category along the route but execute found none → the reply says
    none were found rather than pretending they are on the map."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B in auto con i ristoranti"}],
        "tool_results": [_routing_entry("car", route=_journey()["journey"]["routes"][0])],
        "services": {},
        "slots": {"intent": "route", "mode": "car", "services_category": "Restaurant"},
        "unsupported": False,
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "Non ho trovato i ristoranti lungo il percorso" in reply


async def test_respond_route_surfaces_mode_for_widget():
    """A drawable route (wkt present) carries data.mode so the dashboard widget knows
    the vehicle to render; the value is the routetype the route was computed with."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da Duomo a Santa Croce in auto"}],
        "tool_results": [
            _routing_entry("car", route=_journey()["journey"]["routes"][0])
        ],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "car"},
    }
    out = await respond(state)
    assert out["response"]["data"]["mode"] == "car"


async def test_respond_no_mode_on_route_error():
    """A route that failed (route_error, no wkt) is not drawable → no mode field added, and the
    reply is the deterministic Italian error sentence (never the raw router string)."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B in auto"}],
        "tool_results": [_routing_entry("car", error="empty routes list")],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "car"},
    }
    out = await respond(state)
    data = out["response"]["data"]
    assert "route_error" in data and "mode" not in data
    reply = out["response"]["messages"][-1]["content"]
    assert "Non sono riuscito a calcolare il percorso in auto" in reply
    assert "empty routes list" not in reply  # the raw router error never reaches the user


async def test_respond_no_mode_outside_route():
    """Non-route intents (unsupported) must never get a mode field (rule 8)."""
    state = {
        "intent": "other",
        "messages": [{"role": "user", "content": "ciao"}],
        "tool_results": [],
        "unsupported": True,
    }
    out = await respond(state)
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
    out = await respond(state)
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
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "destinazione" in reply
    assert "partenza" not in reply


async def test_respond_bare_greeting_welcomes():
    """A message that is only a greeting ('ciao') → a warm welcome + onboarding, NOT the dry
    'unsupported' pitch (understand classes a bare greeting as 'other')."""
    state = {
        "intent": "other",
        "messages": [{"role": "user", "content": "ciao"}],
        "tool_results": [],
        "unsupported": True,
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert reply.startswith("Ciao!")
    assert "mobilità" in reply and "punto-punto" not in reply  # welcome, not the pitch


async def test_respond_greets_on_first_turn_only():
    """A short 'Ciao!' leads the FIRST-turn answer; a follow-up (a prior assistant turn in
    history) answers directly with no greeting."""
    route = _routing_entry("car", route=_journey()["journey"]["routes"][0])
    slots = {"intent": "route", "mode": "car"}
    first = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B in auto"}],
        "tool_results": [route],
        "unsupported": False,
        "slots": slots,
    }
    out = await respond(first)
    assert out["response"]["messages"][-1]["content"].startswith("Ciao!")

    followup = {
        "intent": "route",
        "messages": [
            {"role": "user", "content": "da A a B in auto"},
            {"role": "assistant", "content": "In auto 1.8 km, circa 23 minuti."},
            {"role": "user", "content": "e a piedi?"},
        ],
        "tool_results": [route],
        "unsupported": False,
        "slots": slots,
    }
    out2 = await respond(followup)
    assert not out2["response"]["messages"][-1]["content"].startswith("Ciao!")


async def test_respond_leads_with_resolved_endpoints():
    """The reply opens with 'Da ORIGIN a DESTINATION:' from the resolved endpoint labels (so
    the user can spot a wrong geocode), then lists each mode on its own line."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da via ciro menotti 19 a via faentina 16"}],
        "tool_results": [
            _routing_entry("car", route={"wkt": "L", "distance": 2.609, "time": "0:04:53"}),
            _routing_entry("foot", route={"wkt": "L", "distance": 1.841, "time": "0:22:05"}),
        ],
        "unsupported": False,
        "slots": {"intent": "route", "mode": ""},
        "labels": {"origin": "Via Ciro Menotti 19, Firenze", "destination": "Via Faentina 16, Firenze"},
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert reply.startswith("Ciao! Da Via Ciro Menotti 19, Firenze a Via Faentina 16, Firenze:")
    assert "\nIn auto: 2.6 km, circa 5 minuti" in reply  # one mode per line (newline-separated)
    assert "\nA piedi: 1.8 km, circa 22 minuti" in reply
    assert "L'opzione più veloce è in auto." in reply


async def test_respond_lead_contracts_da_for_gps_origin():
    """A GPS-default origin reads 'Dalla tua posizione ...' (da + la article contraction),
    never the ungrammatical 'Da la tua posizione'."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "portami a via faentina 16"}],
        "tool_results": [_routing_entry("foot", route={"wkt": "L", "distance": 1.0, "time": "0:12:00"})],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "foot", "origin_text": ""},
        "labels": {"origin": "la tua posizione (vicino a Via Zara)", "destination": "Via Faentina 16, Firenze"},
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "Dalla tua posizione (vicino a Via Zara) a Via Faentina 16, Firenze:" in reply
    assert "Da la tua posizione" not in reply


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


async def test_respond_single_mode_detail_on_route_not_reply():
    """A single-mode request → the deterministic step block rides routes[0].detail (the
    front-end renders it as a structured bubble), NOT the concise reply text, so the block
    never leaks into the next turn's history."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B in autobus"}],
        "tool_results": [_routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,3 3)", "distance": 42.6, "time": "0:47:03", "arc": _bus_arc_with_stops(),
        })],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "public_transport"},
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "42.6 km" in reply  # the concise reply carries the totals only
    assert "Linea 56" not in reply and "Totale:" not in reply  # the step block never leaks in
    detail = out["response"]["data"]["routes"][0].get("detail")
    assert detail and "Linea 56" in detail and "PONTE MOSSE" in detail and "17:45" in detail


async def test_respond_no_detail_block_for_multi_mode():
    """The default multi-mode (no mode specified) reply is the concise comparison only —
    no step block (Totale/Partenza lines) appended to the text."""
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
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "più veloce" in reply  # the multi-mode comparison names the fastest
    assert "Totale:" not in reply and "Partenza:" not in reply


async def test_respond_attaches_detail_to_every_route_multi_mode():
    """Multi-mode: every route in data.routes carries its pre-rendered `detail` block for
    the dashboard's local mode picker (no new turn on selection), while the concise reply
    text carries no block. A route whose audit has no arc degrades to the totals line (plus
    the departure line when set)."""
    foot_arc = [
        {"transport": "foot", "transport_provider": None, "desc": "Via Ricasoli", "distance": 2.0},
    ]
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B alle 18"}],
        "tool_results": [
            _routing_entry("foot", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00", "arc": foot_arc}),
            _routing_entry("car", route={"wkt": "LINESTRING(0 0,2 2)", "distance": 3.5, "time": "00:08:00"}),
            _routing_entry("public_transport", route={"wkt": "LINESTRING(0 0,3 3)", "distance": 42.6, "time": "0:47:03", "arc": _bus_arc_with_stops()}),
        ],
        "unsupported": False,
        "slots": {"intent": "route", "mode": ""},
        "departure": "18:00",
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "Linea 56" not in reply and "Totale:" not in reply  # no step block leaks into the text
    routes = out["response"]["data"]["routes"]
    by_mode = {r["mode"]: r for r in routes}
    assert set(by_mode) == {"foot", "car", "public_transport"}
    assert all(r.get("detail") for r in routes)
    pt = by_mode["public_transport"]["detail"]
    assert "Linea 56" in pt and "PONTE MOSSE" in pt and "17:45" in pt
    assert "Via Ricasoli" in by_mode["foot"]["detail"]
    # No arc in the car audit → departure + totals only, never a raise.
    assert by_mode["car"]["detail"] == "Partenza: 18:00\nTotale: 3.5 km · 00:08:00"


async def test_respond_no_detail_on_route_error():
    """All routing failed → data carries route_error and no routes; respond neither
    raises nor invents a detail field anywhere."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "da A a B"}],
        "tool_results": [_routing_entry("car", error="no route")],
        "unsupported": False,
        "slots": {"intent": "route", "mode": "car"},
    }
    out = await respond(state)
    data = out["response"]["data"]
    assert data.get("route_error") and "routes" not in data
    assert "detail" not in json.dumps(data)


# --- graph wiring ------------------------------------------------------------

def test_graph_compiles(make_client, make_llm):
    """StateGraph.compile() validates node/edge wiring — catches typos statically."""
    graph = _build_graph(make_client([]), make_llm([]))
    assert graph is not None


# --- nearby (pins-only, no route) --------------------------------------------

async def test_understand_classifies_nearby(make_llm):
    """"mostrami le farmacie qui intorno" → request_type=nearby folded to the internal
    nearby intent, with the category in destination_category (no destination_text)."""
    llm = make_llm([_slots_response(
        '{"request_type":"nearby","origin_text":"","destination_text":"",'
        '"destination_category":"Pharmacy","mode":""}'
    )])
    out = await understand(
        {"messages": [{"role": "user", "content": "mostrami le farmacie qui intorno"}]}, llm=llm
    )
    assert out["intent"] == "nearby"
    assert out["slots"]["destination_category"] == "Pharmacy"


async def test_execute_nearby_gps_pins_only(make_client, make_result):
    """nearby + GPS + category → ONE service_search_near_gps_position around the GPS point,
    maxresults=50, the WHOLE distance-sorted batch kept as pins, and NO route call. The GPS
    centre carries no label (the reply says "qui vicino")."""
    hits = _parking_search(  # same near-search envelope shape
        ("Farmacia A", 11.2546, 43.7735),
        ("Farmacia B", 11.2550, 43.7740),
    )
    client = make_client([make_result(structured=hits)])
    slots = {"intent": "nearby", "origin_text": "", "destination_text": "",
             "destination_category": "Pharmacy", "mode": ""}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["service_search_near_gps_position"]  # no geocode, no routing
    near_args = json.loads(out["tool_results"][0]["args"])
    assert near_args["categories"] == "Pharmacy"
    assert near_args["maxresults"] == 50  # the "default 50" product requirement
    assert near_args["latitude"] == 43.7731 and near_args["longitude"] == 11.2558
    assert near_args["maxdistance"] == 1.0  # first rung of the nearby ladder
    assert [p["name"] for p in out["nearby"]] == ["Farmacia A", "Farmacia B"]
    assert all(p["uri"] and "distance_km" in p for p in out["nearby"])
    assert out["labels"]["center"] is None


async def test_execute_nearby_named_place_geocodes_center(make_client, make_result):
    """nearby with a named place (origin_text) and no GPS → geocode the centre, then
    near-search around the resolved coordinates; the address rides labels.center for the
    reply ("vicino a ...")."""
    client = make_client([
        make_result(structured=_fc_with_addresses((10.40, 43.72, "PIAZZA GARIBALDI"), city="PISA")),
        make_result(structured=_parking_search(("Super X", 10.401, 43.721))),
    ])
    slots = {"intent": "nearby", "origin_text": "Piazza Garibaldi, Pisa", "destination_text": "",
             "destination_category": "Supermarket", "mode": ""}
    out = await execute({"slots": slots}, client=client)  # no GPS
    names = [e["name"] for e in out["tool_results"]]
    assert names == ["address_search_location", "service_search_near_gps_position"]
    near_args = json.loads(out["tool_results"][1]["args"])
    assert near_args["latitude"] == 43.72 and near_args["longitude"] == 10.40
    assert [p["name"] for p in out["nearby"]] == ["Super X"]
    assert out["labels"]["center"] and "Pisa" in out["labels"]["center"]


async def test_execute_nearby_no_category_is_unsupported(make_client):
    """nearby without a category cannot search (the near tool is category-filtered): execute
    refuses (unsupported) and makes no tool call, so respond can ask 'che tipo?'."""
    client = make_client([])
    slots = {"intent": "nearby", "origin_text": "", "destination_text": "",
             "destination_category": "", "mode": ""}
    out = await execute({"slots": slots, "user_gps": {"lat": 43.7731, "lng": 11.2558}}, client=client)
    assert out["unsupported"] is True
    assert "nearby" not in out
    assert not client.calls


async def test_respond_nearby_counts_pins():
    """nearby ships its hits as TOP-LEVEL data.services and the reply is a short human count."""
    state = {
        "intent": "nearby",
        "messages": [{"role": "user", "content": "mostrami le farmacie qui intorno"}],
        "tool_results": [],
        "nearby": [
            {"name": "Farmacia A", "lat": 43.77, "lng": 11.25, "uri": "http://a", "distance_km": 0.2},
            {"name": "Farmacia B", "lat": 43.78, "lng": 11.26, "uri": "http://b", "distance_km": 0.4},
        ],
        "labels": {"center": None},
        "slots": {"intent": "nearby", "destination_category": "Pharmacy", "origin_text": ""},
        "unsupported": False,
    }
    out = await respond(state)
    response = out["response"]
    assert response["request_type"] == "nearby"
    assert [s["name"] for s in response["data"]["services"]] == ["Farmacia A", "Farmacia B"]
    assert "Ci sono 2 farmacie qui vicino" in response["messages"][-1]["content"]


async def test_respond_nearby_singular_and_named_place():
    """One hit reads "C'è 1 ..." (singular) and a named centre reads "vicino a <place>"."""
    state = {
        "intent": "nearby",
        "messages": [{"role": "user", "content": "supermercati vicino a Pisa"}],
        "tool_results": [],
        "nearby": [{"name": "Super X", "lat": 43.72, "lng": 10.40, "uri": "http://x", "distance_km": 0.1}],
        "labels": {"center": "Piazza Garibaldi, Pisa"},
        "slots": {"intent": "nearby", "destination_category": "Supermarket", "origin_text": "Pisa"},
        "unsupported": False,
    }
    out = await respond(state)
    assert "C'è 1 supermercato vicino a Piazza Garibaldi, Pisa" in out["response"]["messages"][-1]["content"]


async def test_respond_nearby_none_found():
    """The search came back empty → the reply says none were found, not a fake count."""
    state = {
        "intent": "nearby",
        "messages": [{"role": "user", "content": "farmacie qui intorno"}],
        "tool_results": [],
        "nearby": [],
        "labels": {"center": None},
        "slots": {"intent": "nearby", "destination_category": "Pharmacy", "origin_text": ""},
        "unsupported": False,
    }
    out = await respond(state)
    assert out["response"]["data"]["services"] == []
    assert "Non ho trovato farmacie qui vicino" in out["response"]["messages"][-1]["content"]


async def test_respond_nearby_missing_category_asks_type():
    """nearby refused for a missing category → the reply asks which kind, not the generic
    unsupported pitch."""
    state = {
        "intent": "nearby",
        "messages": [{"role": "user", "content": "servizi qui intorno"}],
        "tool_results": [],
        "labels": {"center": None},
        "slots": {"intent": "nearby", "destination_category": "", "origin_text": ""},
        "unsupported": True,
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "Che tipo di servizi cerchi" in reply


async def test_respond_nearby_missing_place_asks_where():
    """nearby with a category but no resolvable centre (no place, no GPS) → the reply asks
    where to search."""
    state = {
        "intent": "nearby",
        "messages": [{"role": "user", "content": "farmacie"}],
        "tool_results": [],
        "labels": {"center": None},
        "slots": {"intent": "nearby", "destination_category": "Pharmacy", "origin_text": ""},
        "unsupported": True,
    }
    out = await respond(state)
    reply = out["response"]["messages"][-1]["content"]
    assert "Dimmi dove cercare" in reply
