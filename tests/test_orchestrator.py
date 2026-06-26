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
    """service_search_near_gps_position envelope from (name, lng, lat, free) tuples.
    free may be None (realtime not loaded — the degraded case)."""
    return {"features": [
        {
            "geometry": {"coordinates": [lng, lat]},
            "properties": {
                "name": name,
                "serviceUri": f"http://www.disit.org/km4city/resource/{name}",
                **({"freeParking": free} if free is not None else {}),
            },
        }
        for (name, lng, lat, free) in spots
    ]}


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
        '{"request_type":"journey","info_kind":"","origin_text":"Duomo",'
        '"destination_text":"Santa Croce","mode":"foot_shortest"}'
    )])
    out = await understand(
        {"messages": [{"role": "user", "content": "from Duomo to Santa Croce on foot"}]}, llm=llm
    )
    assert out["intent"] == "route"  # journey folded into the internal route intent
    assert out["slots"]["origin_text"] == "Duomo"
    assert out["slots"]["mode"] == "foot_shortest"


def test_request_to_intent_maps_both_axes():
    """The two-axis classification folds into the internal `intent` vocabulary."""
    assert _request_to_intent({"request_type": "journey"}) == "route"
    assert _request_to_intent({"request_type": "transit_info", "info_kind": "lines"}) == "tpl_lines"
    assert _request_to_intent({"request_type": "transit_info", "info_kind": "timeline"}) == "tpl_timeline"
    assert _request_to_intent({"request_type": "transit_info", "info_kind": ""}) == "other"  # unsupported
    assert _request_to_intent({"request_type": "other"}) == "other"


def test_extract_slots_schema_requires_all_fields():
    """Llama4 only fills required params — a real run dropped destination_text when
    it was optional. All slots must stay required ('' marks an absent one)."""
    params = _EXTRACT_SLOTS_SCHEMA["function"]["parameters"]
    assert set(params["required"]) == {
        "request_type", "info_kind", "origin_text", "destination_text", "mode",
        "agency_text", "line_text", "stop_text",
    }
    assert "" in params["properties"]["mode"]["enum"]  # required mode needs an 'absent' value
    assert "" in params["properties"]["info_kind"]["enum"]  # '' = not a transit_info request


# --- _pick_coord (L17) -------------------------------------------------------

def test_pick_coord_rejects_labels_with_extra_tokens():
    # "PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S." (a company, L17) contains the
    # search tokens but adds its own → no match; the real square matches even
    # across case and the Italian function word "del".
    fc = _fc_with_addresses(
        (11.2421, 43.7736, "PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S."),
        (11.2560, 43.7731, "PIAZZA DEL DUOMO"),
    )
    assert _pick_coord(fc, "Piazza Duomo") == [11.2560, 43.7731]


# --- execute -----------------------------------------------------------------

async def test_execute_route_success(make_client, make_result):
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # routing
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": "foot_shortest"}
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


async def test_execute_dispatches_tpl_intents(make_client, make_result):
    client = make_client([
        make_result(structured={"agencies": [
            {"name": "Autolinee Toscane - Urbano Area Metropolitana Fiorentina", "uri": "http://a/888-48"},
        ]}),
        make_result(structured=[{"shortName": "6"}]),
    ])
    out = await execute({"slots": {"intent": "tpl_lines", "agency_text": ""}}, client=client)
    assert out["unsupported"] is False
    assert [n for n, _ in client.calls] == ["tpl_agencies", "tpl_lines"]


async def test_execute_foot_quiet_falls_back_to_foot_shortest(make_client, make_result):
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey(routes=[])),                 # foot_quiet → empty routes error
        make_result(structured=_journey()),                          # foot_shortest → success
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": "foot_quiet"}
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
        make_result(structured=_parking_search(("P1", 11.26, 43.76, 20))),  # parking (car triggers it)
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert len([n for n, _ in client.calls if n == "routing"]) == 3  # ladder only, no 4th probe
    assert "route_error" in _extract_data(out["tool_results"])
    # car route failed but parking near the destination still came back (S4: independent).
    assert _extract_parking(out["tool_results"], {"lat": 43.76, "lng": 11.26})[0]["name"] == "P1"


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


async def test_execute_unspecified_mode_routes_foot_car_pt(make_client, make_result):
    """Empty mode → execute routes walking, driving and public transport."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # foot_shortest
        make_result(structured=_journey()),                          # car
        make_result(structured=_journey()),                          # public_transport
        make_result(structured=_parking_search(("P1", 11.26, 43.76, 5))),  # parking (car in modes)
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": ""}
    out = await execute({"slots": slots}, client=client)
    routetypes = [json.loads(e["args"])["routetype"] for e in out["tool_results"] if e["name"] == "routing"]
    assert routetypes == ["foot_shortest", "car", "public_transport"]
    # car is among the modes → the parking entry is appended after all routing entries.
    assert out["tool_results"][-1]["name"] == "service_search_near_gps_position"


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


def test_extract_parking_sorts_free_then_distance():
    """Known free-spaces first (most free first); distance breaks ties; computed from dest."""
    dest = {"lat": 43.770, "lng": 11.250}
    # A: far, 30 free; B: near, 10 free; C: nearest, no realtime.
    results = [_parking_entry(
        ("A", 11.260, 43.780, 30),
        ("B", 11.251, 43.771, 10),
        ("C", 11.2502, 43.7701, None),
    )]
    parking = _extract_parking(results, dest)
    assert [p["name"] for p in parking] == ["A", "B", "C"]  # free desc, then the unknown last
    assert parking[0]["free_spaces"] == 30
    assert parking[2]["free_spaces"] is None
    # distance computed (Haversine) from dest, not taken from the envelope
    assert all(isinstance(p["distance_km"], float) for p in parking)
    assert parking[2]["distance_km"] < parking[0]["distance_km"]  # C is nearest


def test_extract_parking_degraded_sorts_by_distance():
    """Realtime not loaded (all free None, the agreed fallback) → nearest-first."""
    dest = {"lat": 43.770, "lng": 11.250}
    results = [_parking_entry(
        ("far", 11.270, 43.790, None),
        ("near", 11.2505, 43.7705, None),
    )]
    parking = _extract_parking(results, dest)
    assert [p["name"] for p in parking] == ["near", "far"]
    assert all(p["free_spaces"] is None for p in parking)


def test_extract_parking_caps_and_handles_empty():
    dest = {"lat": 43.77, "lng": 11.25}
    many = _parking_entry(*[(f"P{i}", 11.25, 43.77 + i * 0.001, i) for i in range(12)])
    assert len(_extract_parking([many], dest)) == mcp_tools.PARKING_MAX
    assert _extract_parking([_parking_entry()], dest) is None      # empty features
    assert _extract_parking([{"name": "routing", "result": {}}], dest) is None  # no parking entry


def test_parse_parking_features_reads_nested_service_envelope():
    """Defensive: the flattened-vs-grouped envelope is probe-uncalibrated, so the parser
    also reads a nested Service.features shape and alternate free-value keys."""
    nested = {"Service": {"features": [
        {"geometry": {"coordinates": [11.25, 43.77]},
         "properties": {"serviceName": "Garage X", "serviceuri": "http://x", "free": "7"}},
    ]}}
    spots = mcp_tools.parse_parking_features(nested)
    assert spots == [{"name": "Garage X", "lat": 43.77, "lng": 11.25, "uri": "http://x", "free_spaces": 7}]
    assert mcp_tools.parse_parking_features({"error": "boom"}) == []


def test_slim_parking_compacts_to_name_and_free():
    slim = mcp_tools.slim_result_for_llm(
        "service_search_near_gps_position",
        _parking_search(("P1", 11.25, 43.77, 12), ("P2", 11.26, 43.78, None)),
    )
    assert slim["count"] == 2
    assert slim["parking"] == [
        {"name": "P1", "free_spaces": 12},
        {"name": "P2", "free_spaces": None},
    ]


async def test_execute_foot_only_skips_parking(make_client, make_result):
    """The parking search is car-specific: a foot-only request must not call it."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey()),                          # routing (foot)
    ])
    slots = {"intent": "route", "origin_text": "A", "destination_text": "B", "mode": "foot_shortest"}
    out = await execute({"slots": slots}, client=client)
    assert not any(n == "service_search_near_gps_position" for n, _ in client.calls)
    assert _extract_parking(out["tool_results"], {"lat": 43.76, "lng": 11.26}) is None


def test_extract_data_drops_foot_only_pt():
    """PT degraded to a walking-only journey (no transit leg) → no bus route, no error."""
    results = [
        _routing_entry("foot_shortest", route={"wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00"}),
        _routing_entry("public_transport", route={
            "wkt": "LINESTRING(0 0,1 1)", "distance": 2.0, "time": "00:20:00",
            "arc": [{"transport": "foot"}],  # walking only — not real PT
        }),
    ]
    data = _extract_data(results)
    assert [r["mode"] for r in data["routes"]] == ["foot_shortest"]
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
    """A car route with a parking search → data.parking carries the nearest spots."""
    llm = make_llm([_text_response("In auto 3.5 km. Parcheggio P1 (20 liberi) a 80 m.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "in auto da A a B"}],
        "endpoints": {"origin": {"lat": 43.77, "lng": 11.24}, "destination": {"lat": 43.76, "lng": 11.26}},
        "tool_results": [
            {"name": "routing", "args": json.dumps({"routetype": "car"}),
             "result": {"journey": _journey()["journey"]}},
            {"name": "service_search_near_gps_position",
             "result": _parking_search(("P1", 11.26, 43.76, 20))},
        ],
        "unsupported": False,
    }
    out = await respond(state, llm=llm)
    parking = out["response"]["data"]["parking"]
    assert parking[0]["name"] == "P1" and parking[0]["free_spaces"] == 20


async def test_respond_parking_when_car_route_empty(make_llm):
    """S4/G2: car route came back empty (ZTL) but parking still shows (independent of routes)."""
    llm = make_llm([_text_response("Non riesco a calcolare il percorso in auto, ma vicino: P1.")])
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "in auto da A a B"}],
        "endpoints": {"origin": {"lat": 43.77, "lng": 11.24}, "destination": {"lat": 43.76, "lng": 11.26}},
        "tool_results": [
            {"name": "routing", "args": json.dumps({"routetype": "car"}), "result": {"error": ""}},
            {"name": "service_search_near_gps_position",
             "result": _parking_search(("P1", 11.26, 43.76, None))},
        ],
        "unsupported": False,
    }
    out = await respond(state, llm=llm)
    data = out["response"]["data"]
    assert "routes" not in data  # car route failed
    assert data["parking"][0]["name"] == "P1"  # parking still present
    assert data["parking"][0]["free_spaces"] is None  # degraded: realtime not loaded


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
    """Non-route intents (unsupported/tpl) must never get a mode field (rule 8)."""
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
