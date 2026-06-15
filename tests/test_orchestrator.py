"""Unit tests for the deterministic graph nodes (orchestrator.py).

Lean core suite: one happy path per flow + one guard per documented lesson
(L8 car-ZTL, L17 POI ranking, L18 missing-slot ask, L19 service-error vs ZTL).
"""
import json

from snap4city_mobility_mcp import mcp_tools
from snap4city_mobility_mcp.llm import Llama4Error
from snap4city_mobility_mcp.orchestrator import (
    _EXTRACT_SLOTS_SCHEMA,
    RESPOND_SYSTEM,
    _build_graph,
    _extract_data,
    _pick_coord,
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
    assert set(params["required"]) == {
        "intent", "origin_text", "destination_text", "mode",
        "agency_text", "line_text", "stop_text",
    }
    assert "" in params["properties"]["mode"]["enum"]  # required mode needs an 'absent' value


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
    ])
    slots = {"intent": "route", "origin_text": "Duomo", "destination_text": "Santa Croce", "mode": "car"}
    out = await execute({"slots": slots}, client=client)
    assert len([n for n, _ in client.calls if n == "routing"]) == 3  # ladder only, no 4th probe
    assert "route_error" in _extract_data(out["tool_results"])


# --- _extract_data -----------------------------------------------------------

def test_extract_data_preserves_full_wkt():
    long_wkt = "LINESTRING(" + "9 4," * 50 + "1 1)"
    results = [{"name": "routing", "result": {"journey": {"routes": [{"wkt": long_wkt, "distance": 1.0}]}}}]
    data = _extract_data(results)
    assert data["wkt"] == long_wkt  # not truncated — the map widget needs the full geometry
    assert len(data["wkt"]) > 80


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
    reply = out["final"]["messages"][-1]["content"]
    assert "partenza" in reply and "destinazione" in reply
    assert "punto-punto" not in reply


def test_respond_system_separates_service_error_from_ztl():
    """L19: the L8 bare {"error":""} (car/PT broken server-side) must NOT be narrated as a
    ZTL/pedestrian restriction — that misled the user and the referente (a drivable, non-ZTL
    destination got blamed on a ZTL). ZTL phrasing is reserved for the genuine L2 empty-routes
    error string. Both error strings appear in the prompt, and the service-side one is tied to
    an explicit 'do NOT claim ZTL' instruction."""
    assert "empty response from routing service" in RESPOND_SYSTEM
    assert "no route found (empty routes list)" in RESPOND_SYSTEM
    idx = RESPOND_SYSTEM.index("empty response from routing service")
    assert "do NOT claim" in RESPOND_SYSTEM[idx:idx + 400]


# --- graph wiring ------------------------------------------------------------

def test_graph_compiles(make_client, make_llm):
    """StateGraph.compile() validates node/edge wiring — catches typos statically."""
    graph = _build_graph(make_client([]), make_llm([]))
    assert graph is not None
