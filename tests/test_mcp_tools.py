"""Unit tests for the deterministic MCP layer (mcp_tools.py)."""
import json
import pathlib

import pytest

from snap4city_mobility_mcp import mcp_tools
from snap4city_mobility_mcp.mcp_tools import (
    EXPOSED_TOOLS,
    GEOCODE_LLM_KEEP,
    TOOL_NAMES,
    _unwrap,
    exec_tool,
    fetch_tool_schemas,
    routing_with_retry,
    slim_result_for_llm,
)


# --- _unwrap -----------------------------------------------------------------

def test_unwrap_structured(make_result):
    assert _unwrap(make_result(structured={"a": 1})) == {"a": 1}


def test_unwrap_content_text(make_result):
    assert _unwrap(make_result(text='{"a": 2}')) == {"a": 2}


def test_unwrap_empty(make_result):
    assert _unwrap(make_result()) is None


# --- routing_with_retry (L2/L3/L7/L8 envelope shapes) ------------------------

_ROUTE_ARGS = {
    "startlatitude": 43.77, "startlongitude": 11.25,
    "endlatitude": 43.76, "endlongitude": 11.26, "routetype": "foot_shortest",
}


async def test_routing_happy(make_client, make_result):
    env = {
        "journey": {"routes": [{"distance": 0.68, "eta": "10:00:00", "wkt": "LINESTRING(1 2,3 4)"}]},
        "response": {"error_code": "0", "error_message": "successful"},
    }
    client = make_client([make_result(structured=env)])
    out = await routing_with_retry(client, _ROUTE_ARGS)
    assert out["journey"]["routes"][0]["distance"] == 0.68


async def test_routing_error_code_shape_b(make_client, make_result):
    env = {"journey": {"routes": []}, "response": {"error_code": "-2", "error_message": "not found"}}
    out = await routing_with_retry(make_client([make_result(structured=env)]), _ROUTE_ARGS)
    assert out["error"] == "routing failed: not found (code=-2)"


async def test_routing_empty_routes_shape_c(make_client, make_result):
    env = {"journey": {"routes": []}, "response": {"error_code": "0", "error_message": "successful"}}
    out = await routing_with_retry(make_client([make_result(structured=env)]), _ROUTE_ARGS)
    assert out["error"] == "no route found (empty routes list)"


async def test_routing_stale_retry_shape_a(make_client, make_result, monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mcp_tools.asyncio, "sleep", _noop)
    stale = make_result(structured={"error": ""})
    client = make_client([stale, stale, stale])  # initial + 2 retries, all stale
    out = await routing_with_retry(client, _ROUTE_ARGS)
    assert "empty response" in out["error"]
    assert len(client.calls) == 3


async def test_routing_stale_recovers_on_first_retry(make_client, make_result, monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mcp_tools.asyncio, "sleep", _noop)
    env = {
        "journey": {"routes": [{"distance": 0.68}]},
        "response": {"error_code": "0", "error_message": "successful"},
    }
    client = make_client([make_result(structured={"error": ""}), make_result(structured=env)])
    out = await routing_with_retry(client, _ROUTE_ARGS)
    assert out["journey"]["routes"][0]["distance"] == 0.68
    assert len(client.calls) == 2  # recovered → loop exits, no third attempt


# --- exec_tool (dispatch; never raises) --------------------------------------

async def test_exec_tool_unknown(make_client):
    assert await exec_tool(make_client([]), "nope", {}) == {"error": "unknown tool 'nope'"}


async def test_exec_tool_routing_goes_through_retry(make_client, make_result):
    env = {"journey": {"routes": [{"distance": 1.0}]}, "response": {"error_code": "0"}}
    client = make_client([make_result(structured=env)])
    args = {**_ROUTE_ARGS, "routetype": "car", "authentication": "secret"}
    out = await exec_tool(client, "routing", args)
    assert "journey" in out
    name, sent = client.calls[0]
    assert name == "routing"
    assert "authentication" not in sent  # stripped
    assert sent["routetype"] == "car"


# --- slim_result_for_llm (shrink the model's context; audit keeps full payload) ---

def test_slim_geocode_caps_and_keeps_only_needed_fields():
    fc = {"type": "FeatureCollection", "count": 100, "features": [
        {"geometry": {"coordinates": [11.25 + i / 1000, 43.77]},
         "properties": {"address": f"Via {i}", "city": "Firenze", "score": "9", "serviceUri": "http://x"}}
        for i in range(20)
    ]}
    slim = slim_result_for_llm("address_search_location", fc)
    assert slim["count"] == 100
    assert len(slim["features"]) == GEOCODE_LLM_KEEP  # capped
    assert slim["features"][0] == {"address": "Via 0", "city": "Firenze", "coordinates": [11.25, 43.77]}
    assert "serviceUri" not in slim["features"][0] and "score" not in slim["features"][0]


def test_slim_routing_drops_wkt_and_lists_streets():
    full = {"journey": {
        "routes": [{
            "wkt": "LINESTRING(11.25 43.77, 11.26 43.78, ...)",  # huge — must be dropped
            "distance": 0.83, "eta": "10:11:00", "time": "00:11:00",
            "arc": [{"desc": "Via Ricasoli"}, {"desc": "nd"}, {"desc": "Via Ricasoli"},
                    {"desc": "Borgo degli Albizi"}],
        }],
        "source_node": {"lat": 43.77, "lon": 11.25}, "destination_node": {"lat": 43.773, "lon": 11.258},
    }}
    slim = slim_result_for_llm("routing", full)
    j = slim["journey"]
    assert "wkt" not in json.dumps(slim)  # WKT fully gone from the model's view
    assert j["distance_km"] == 0.83 and j["eta"] == "10:11:00"
    assert j["streets"] == ["Via Ricasoli", "Borgo degli Albizi"]  # deduped, "nd" dropped


def test_slim_passthrough_errors_and_unknown():
    assert slim_result_for_llm("routing", {"error": "boom"}) == {"error": "boom"}
    assert slim_result_for_llm("tpl_agencies", {"agencies": [1, 2]}) == {"agencies": [1, 2]}


def _feature(lng, lat, addr="x"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {"address": addr}}


async def test_exec_tool_geocode_filters_to_tuscany(make_client, make_result):
    """Out-of-region (Valencia/France) hits are dropped; Tuscan ones kept, score order."""
    fc = {"type": "FeatureCollection", "count": 3, "features": [
        _feature(-0.3068184, 39.59272, "Valencia"),   # Spain — drop
        _feature(11.2560, 43.7714, "Firenze Duomo"),  # Tuscany — keep
        _feature(4.531295, 44.212044, "France"),      # France — drop
    ]}
    client = make_client([make_result(structured=fc)])
    out = await exec_tool(client, "address_search_location", {"search": "Piazza del Duomo, Firenze"})
    assert out["count"] == 1
    assert out["features"][0]["properties"]["address"] == "Firenze Duomo"
    _, sent = client.calls[0]
    assert sent["excludePOI"] is False  # forced so landmarks are findable


async def test_exec_tool_geocode_no_tuscan_match_errors(make_client, make_result):
    fc = {"type": "FeatureCollection", "count": 1, "features": [_feature(-0.3068, 39.5927)]}
    client = make_client([make_result(structured=fc)])
    out = await exec_tool(client, "address_search_location", {"search": "nowhere"})
    assert "error" in out and "no Tuscany-area match" in out["error"]


async def test_exec_tool_passthrough_strips_auth(make_client, make_result):
    client = make_client([make_result(structured={"agencies": [{"uri": "u"}]})])
    out = await exec_tool(client, "tpl_agencies", {"authentication": "secret"})
    assert out == {"agencies": [{"uri": "u"}]}
    _, sent = client.calls[0]
    assert "authentication" not in sent


async def test_exec_tool_never_raises(make_client):
    out = await exec_tool(make_client([RuntimeError("boom")]), "tpl_agencies", {})
    assert "error" in out and "boom" in out["error"]


# --- fetch_tool_schemas (schemas come from the server's own list_tools) -------

async def test_fetch_tool_schemas_filters_and_converts(make_client, make_tool):
    tools = [
        make_tool("routing", "route between points", {
            "type": "object",
            "properties": {"startlatitude": {"type": "number"}, "authentication": {"type": "string"}},
            "required": ["startlatitude", "authentication"],
        }),
        make_tool("tpl_agencies", "agencies", {"type": "object", "properties": {}}),
        make_tool("service_info", "not exposed", {"type": "object", "properties": {}}),
    ]
    schemas = await fetch_tool_schemas(make_client(tools=tools))
    names = [s["function"]["name"] for s in schemas]
    assert names == ["routing", "tpl_agencies"]  # service_info dropped, EXPOSED order
    routing = schemas[0]["function"]
    assert routing["description"] == "route between points"
    assert "authentication" not in routing["parameters"]["properties"]  # stripped
    assert routing["parameters"]["required"] == ["startlatitude"]
    assert all(s["type"] == "function" for s in schemas)


async def test_fetch_tool_schemas_skips_absent(make_client, make_tool):
    schemas = await fetch_tool_schemas(make_client(tools=[make_tool("tpl_lines", "lines")]))
    assert [s["function"]["name"] for s in schemas] == ["tpl_lines"]


def test_exposed_tools_count():
    assert len(EXPOSED_TOOLS) == 7
    assert TOOL_NAMES == frozenset(EXPOSED_TOOLS)


def _collect_names(obj, acc):
    if isinstance(obj, dict):
        name = obj.get("name")
        if isinstance(name, str):
            acc.add(name)
        for v in obj.values():
            _collect_names(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect_names(v, acc)


def test_exposed_tools_exist_in_probe():
    probe = pathlib.Path(__file__).resolve().parents[1] / "probe-native-tools.json"
    if not probe.exists():
        pytest.skip("probe-native-tools.json not present")
    names: set = set()
    _collect_names(json.loads(probe.read_text(encoding="utf-8")), names)
    missing = TOOL_NAMES - names
    assert not missing, f"exposed tools absent from probe: {missing}"
