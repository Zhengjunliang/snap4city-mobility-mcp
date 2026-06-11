"""Unit tests for the deterministic graph nodes (orchestrator.py)."""
import json

from snap4city_mobility_mcp import mcp_tools
from snap4city_mobility_mcp.llm import Llama4Error
from snap4city_mobility_mcp.orchestrator import (
    _EXTRACT_SLOTS_SCHEMA,
    _build_graph,
    _extract_data,
    _pick_coord,
    _results_view,
    _template_answer,
    execute,
    respond,
    understand,
)


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
        '{"intent":"route","origin_text":"Duomo","destination_text":"Santa Croce","mode":"foot_shortest"}'
    )])
    out = await understand(
        {"messages": [{"role": "user", "content": "from Duomo to Santa Croce on foot"}]}, llm=llm
    )
    assert out["intent"] == "route"
    assert out["slots"]["origin_text"] == "Duomo"
    assert out["slots"]["mode"] == "foot_shortest"


def test_extract_slots_schema_requires_all_fields():
    """Llama4 only fills required params — a real run dropped destination_text when
    it was optional. All slots must stay required ('' marks an absent one)."""
    params = _EXTRACT_SLOTS_SCHEMA["function"]["parameters"]
    assert set(params["required"]) == {"intent", "origin_text", "destination_text", "mode"}
    assert "" in params["properties"]["mode"]["enum"]  # required mode needs an 'absent' value


async def test_understand_invalid_args_falls_back(make_llm):
    llm = make_llm([_slots_response("NOT JSON")])
    out = await understand({"messages": [{"role": "user", "content": "hi"}]}, llm=llm)
    assert out["intent"] == "other"


# --- _pick_coord -------------------------------------------------------------

def _fc_with_addresses(*entries) -> dict:
    """FeatureCollection from (lng, lat, address) triples (address may be None)."""
    return {"type": "FeatureCollection", "features": [
        {"geometry": {"coordinates": [lng, lat]},
         "properties": {"address": addr, "city": "FIRENZE"}}
        for lng, lat, addr in entries
    ]}


def test_pick_coord_reads_lng_lat():
    assert _pick_coord(_feature_collection(11.25, 43.77), "Duomo") == [11.25, 43.77]


def test_pick_coord_none_on_error_or_empty():
    assert _pick_coord({"error": "no Tuscany-area match"}, "x") is None
    assert _pick_coord({"type": "FeatureCollection", "features": []}, "x") is None
    assert _pick_coord(None, "x") is None


def test_pick_coord_prefers_address_matching_search():
    # The server ranks fuzzy POIs above the real square — the address-matched
    # feature must win even when it ranks last ("piazza Dalmazia" case).
    fc = _fc_with_addresses(
        (11.2421, 43.7736, None),
        (11.2400, 43.7900, None),
        (11.2402, 43.7956, "PIAZZA DALMAZIA"),
    )
    assert _pick_coord(fc, "piazza Dalmazia") == [11.2402, 43.7956]


def test_pick_coord_rejects_labels_with_extra_tokens():
    # "PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S." (a company, L17) contains the
    # search tokens but adds its own → no match; the real square matches even
    # across case and the Italian function word "del".
    fc = _fc_with_addresses(
        (11.2421, 43.7736, "PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S."),
        (11.2560, 43.7731, "PIAZZA DEL DUOMO"),
    )
    assert _pick_coord(fc, "Piazza Duomo") == [11.2560, 43.7731]


def test_pick_coord_matches_across_accents():
    fc = _fc_with_addresses(
        (11.2300, 43.7800, None),
        (11.2589, 43.7770, "PIAZZA DELL'UNITÀ ITALIANA"),
    )
    assert _pick_coord(fc, "piazza dell'Unita Italiana") == [11.2589, 43.7770]


def test_pick_coord_never_matches_a_bare_city_label():
    # A municipality entry (address="FIRENZE") is a subset of any search ending
    # in ", Firenze" — it must not beat the real street entry.
    fc = _fc_with_addresses(
        (11.2000, 43.7700, "FIRENZE"),
        (11.2560, 43.7731, "PIAZZA DEL DUOMO"),
    )
    assert _pick_coord(fc, "Piazza del Duomo, Firenze") == [11.2560, 43.7731]


def test_pick_coord_falls_back_to_first_feature():
    # Station queries: POI features carry no address → the server order stands.
    fc = _fc_with_addresses(
        (11.2386, 43.8045, None),
        (11.2482, 43.8047, None),
    )
    assert _pick_coord(fc, "stazione di Firenze Rifredi") == [11.2386, 43.8045]


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
    out = await execute({"slots": {"intent": "tpl_lines"}}, client=client)
    assert out["unsupported"] is True
    assert out["tool_results"] == []
    assert client.calls == []


async def test_execute_missing_place_is_unsupported(make_client):
    client = make_client([])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert out["unsupported"] is True
    assert client.calls == []


async def test_execute_geocode_error_skips_routing(make_client, make_result):
    client = make_client([
        make_result(structured=_feature_collection(-0.37, 39.47)),  # origin, address pass → Valencia
        make_result(structured=_feature_collection(-0.37, 39.47)),  # origin, POI pass → Valencia again
        make_result(structured=_feature_collection(11.26, 43.76)),  # dest, address pass → Tuscan hit
    ])
    slots = {"intent": "route", "origin_text": "x", "destination_text": "y", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert out["unsupported"] is False
    assert [e["name"] for e in out["tool_results"]] == ["address_search_location", "address_search_location"]
    assert "routing" not in [e["name"] for e in out["tool_results"]]  # never routed


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


async def test_execute_foot_shortest_falls_back_to_foot_quiet(make_client, make_result):
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey(routes=[])),                 # foot_shortest → empty routes error
        make_result(structured=_journey()),                          # foot_quiet → success
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": "foot_shortest"}
    out = await execute({"slots": slots}, client=client)
    routings = [e for e in out["tool_results"] if e["name"] == "routing"]
    assert len(routings) == 2
    assert json.loads(routings[0]["args"])["routetype"] == "foot_shortest"
    assert json.loads(routings[1]["args"])["routetype"] == "foot_quiet"
    assert _extract_data(out["tool_results"])["distance_km"] == 1.83


async def test_execute_foot_fallback_is_a_single_probe(make_client, make_result, monkeypatch):
    """After the requested profile burns its full stale ladder (3 attempts), the
    fallback profile gets exactly ONE attempt — the transient is already ruled out
    and each extra attempt costs ~11 s of user-visible latency."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mcp_tools.asyncio, "sleep", _noop)
    stale = {"error": ""}
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=stale),                               # foot_shortest #1
        make_result(structured=stale),                               # foot_shortest #2
        make_result(structured=stale),                               # foot_shortest #3
        make_result(structured=stale),                               # foot_quiet: single probe
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": "foot_shortest"}
    out = await execute({"slots": slots}, client=client)
    assert len([n for n, _ in client.calls if n == "routing"]) == 4  # 3 + 1, not 3 + 3
    assert "route_error" in _extract_data(out["tool_results"])


async def test_execute_both_foot_profiles_fail_keeps_requested_mode_error(make_client, make_result):
    """When the mode AND its fallback fail, surface the requested mode's error."""
    client = make_client([
        make_result(structured=_feature_collection(11.24, 43.77)),  # geocode origin
        make_result(structured=_feature_collection(11.26, 43.76)),  # geocode dest
        make_result(structured=_journey(routes=[])),                 # foot_shortest → empty routes
        make_result(structured={"journey": {"routes": []},           # foot_quiet → envelope error
                                "response": {"error_code": "-2", "error_message": "not found"}}),
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": "foot_shortest"}
    out = await execute({"slots": slots}, client=client)
    assert _extract_data(out["tool_results"]) == {"route_error": "no route found (empty routes list)"}


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
    final = out["final"]
    assert final["ok"] is True
    assert "answer" not in final  # reply lives in messages[-1], not a custom field
    reply = final["messages"][-1]
    assert reply["role"] == "assistant"
    assert "1.83 km" in reply["content"]
    assert final["data"]["distance_km"] == 1.83


async def test_respond_falls_back_to_template_on_llm_error():
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "q"}],
        "tool_results": [{"name": "routing", "result": {"journey": _journey()["journey"]}}],
        "unsupported": False,
    }
    out = await respond(state, llm=_RaisingLLM())
    reply = out["final"]["messages"][-1]["content"]
    assert reply.startswith("📍")
    assert "1.83 km" in reply


async def test_respond_unsupported_template_on_llm_error():
    state = {"intent": "other", "messages": [{"role": "user", "content": "hi"}],
             "tool_results": [], "unsupported": True}
    out = await respond(state, llm=_RaisingLLM())
    assert "punto-punto" in out["final"]["messages"][-1]["content"]


async def test_respond_missing_place_asks_instead_of_unsupported():
    """route intent with blank places → targeted ask, not the 'unsupported' pitch."""
    state = {
        "intent": "route",
        "messages": [{"role": "user", "content": "voglio andare a piedi"}],
        "tool_results": [],
        "unsupported": True,
        "slots": {"intent": "route", "origin_text": "", "destination_text": ""},
    }
    out = await respond(state, llm=_RaisingLLM())
    reply = out["final"]["messages"][-1]["content"]
    assert "partenza" in reply and "destinazione" in reply
    assert "punto-punto" not in reply


# --- _template_answer --------------------------------------------------------

def test_template_answer_route():
    data = {"distance_km": 1.83, "duration": "00:23:18", "eta": "15:59:09"}
    assert _template_answer("route", data, unsupported=False) == "📍 1.83 km · ~00:23:18 · arrivo 15:59:09"


def test_template_answer_route_error():
    assert _template_answer("route", {"route_error": "no route"}, unsupported=False) == "⚠ no route"


def test_template_answer_unsupported():
    assert "punto-punto" in _template_answer("other", {}, unsupported=True)


def test_template_answer_missing_place():
    answer = _template_answer("route", {}, unsupported=True, missing=["destination"])
    assert "destinazione" in answer
    assert "punto-punto" not in answer


# --- _results_view -----------------------------------------------------------

def test_results_view_missing_place_beats_unsupported():
    view = _results_view([], unsupported=True, missing=["destination"])
    assert view == {"status": "missing_place", "missing": ["destination"]}


# --- _extract_data -----------------------------------------------------------

def test_extract_data_route_full_wkt():
    results = [
        {"name": "address_search_location", "result": {"features": []}},
        {"name": "routing", "result": {"journey": {"routes": [
            {"wkt": "LINESTRING(1 2,3 4)", "distance": 0.68, "eta": "10:00:00", "time": "00:10:00"}
        ]}}},
    ]
    data = _extract_data(results)
    assert data["wkt"] == "LINESTRING(1 2,3 4)"
    assert data["distance_km"] == 0.68
    assert data["duration"] == "00:10:00"


def test_extract_data_route_error():
    results = [{"name": "routing", "result": {"error": "no route found (empty routes list)"}}]
    assert _extract_data(results) == {"route_error": "no route found (empty routes list)"}


def test_extract_data_preserves_full_wkt():
    long_wkt = "LINESTRING(" + "9 4," * 50 + "1 1)"
    results = [{"name": "routing", "result": {"journey": {"routes": [{"wkt": long_wkt, "distance": 1.0}]}}}]
    data = _extract_data(results)
    assert data["wkt"] == long_wkt  # not truncated
    assert len(data["wkt"]) > 80


def test_extract_data_none():
    assert _extract_data([]) == {}


# --- graph wiring ------------------------------------------------------------

def test_graph_compiles(make_client, make_llm):
    """StateGraph.compile() validates node/edge wiring — catches typos statically."""
    graph = _build_graph(make_client([]), make_llm([]))
    assert graph is not None
