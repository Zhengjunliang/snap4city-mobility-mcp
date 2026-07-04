"""Unit tests for the deterministic MCP layer (mcp_tools.py).

Lean core suite: routing envelope shapes (L2/L3/L7/L8), the two-pass + bbox +
flaky-retry geocode (L11/L17/L20), the LLM-context slimming (L12), and the
contract that every exposed tool exists in the live probe.
"""
import json
import pathlib

import pytest

from snap4city_mobility_mcp import mcp_tools
from snap4city_mobility_mcp.mcp_tools import (
    LOCAL_ONLY_TOOLS,
    STOPS_LLM_KEEP,
    TOOL_NAMES,
    _unwrap,
    exec_tool,
    group_arc_legs,
    reverse_geocode,
    routing_with_retry,
    slim_result_for_llm,
)


# --- _unwrap -----------------------------------------------------------------

def test_unwrap_structured(make_result):
    assert _unwrap(make_result(structured={"a": 1})) == {"a": 1}


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


async def test_routing_empty_routes_shape_c(make_client, make_result):
    # L2: success envelope (error_code "0") but empty routes — NOT a route.
    env = {"journey": {"routes": []}, "response": {"error_code": "0", "error_message": "successful"}}
    out = await routing_with_retry(make_client([make_result(structured=env)]), _ROUTE_ARGS)
    assert out["error"] == "no route found (empty routes list)"


async def test_routing_stale_retry_shape_a(make_client, make_result, monkeypatch):
    # L3/L8: bare {"error": ""} — stale ladder runs 3 attempts, then surfaces empty-body error.
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mcp_tools.asyncio, "sleep", _noop)
    stale = make_result(structured={"error": ""})
    client = make_client([stale, stale, stale])  # initial + 2 retries, all stale
    out = await routing_with_retry(client, _ROUTE_ARGS)
    assert "empty response" in out["error"]
    assert len(client.calls) == 3


# --- exec_tool: geocode two-pass + bbox + flaky retry (L11/L17/L20) ----------

def _feature(lng, lat, addr="x", city="FIRENZE"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {"address": addr, "city": city}}


async def test_exec_tool_geocode_filters_to_tuscany(make_client, make_result):
    """L11/L17: out-of-region (Valencia/France) hits are dropped; Tuscan ones kept.
    The address pass (excludePOI=true) hits, so the POI pass is never sent."""
    fc = {"type": "FeatureCollection", "count": 3, "features": [
        _feature(-0.3068184, 39.59272, "Valencia"),   # Spain — drop
        _feature(11.2560, 43.7714, "Firenze Duomo"),  # Tuscany — keep
        _feature(4.531295, 44.212044, "France"),      # France — drop
    ]}
    client = make_client([make_result(structured=fc)])
    out = await exec_tool(client, "address_search_location", {"search": "Piazza del Duomo, Firenze"})
    assert out["count"] == 1
    assert out["features"][0]["properties"]["address"] == "Firenze Duomo"
    assert len(client.calls) == 1
    _, sent = client.calls[0]
    assert sent["excludePOI"] is True  # addresses first — POIs only as fallback (L17)
    assert sent["lang"] == "it" and sent["logic"] == "or"  # Italy/Florence bias on every pass


async def test_exec_tool_geocode_falls_back_to_poi_pass(make_client, make_result):
    """L17: address pass finds nothing in-region (stations/landmarks are POI-only)
    → retried with excludePOI=false."""
    spain = {"type": "FeatureCollection", "count": 1, "features": [_feature(-0.3068, 39.5927)]}
    poi = {"type": "FeatureCollection", "count": 1, "features": [_feature(11.2482, 43.8047)]}
    client = make_client([make_result(structured=spain), make_result(structured=poi)])
    out = await exec_tool(client, "address_search_location", {"search": "stazione di Firenze Rifredi"})
    assert out["count"] == 1
    assert out["features"][0]["geometry"]["coordinates"] == [11.2482, 43.8047]
    assert [sent["excludePOI"] for _, sent in client.calls] == [True, False]


async def test_exec_tool_geocode_retry_recovers(make_client, make_result, monkeypatch):
    """L20: the flaky zero-Tuscany window clears on retry → the next attempt's Tuscan hit wins."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mcp_tools.asyncio, "sleep", _noop)
    miss = {"type": "FeatureCollection", "count": 1, "features": [_feature(-0.3068, 39.5927)]}  # Spain
    hit = {"type": "FeatureCollection", "count": 1, "features": [_feature(11.2560, 43.7714, "Duomo")]}
    # attempt 1: address(miss) + POI(miss) → error; attempt 2: address(hit) → success, POI not sent
    client = make_client([make_result(structured=miss), make_result(structured=miss),
                          make_result(structured=hit)])
    out = await exec_tool(client, "address_search_location", {"search": "Piazza del Duomo, Firenze"})
    assert out["count"] == 1
    assert out["features"][0]["properties"]["address"] == "Duomo"
    assert len(client.calls) == 3  # failed attempt (2 passes) + recovered address pass (1)


# --- reverse_geocode (coordinates_to_address; near-me foundation) ------------

async def test_reverse_geocode_passthrough(make_client, make_result):
    """coordinates_to_address is allowlisted; reverse_geocode forwards lat/lng as separate
    floats and returns the server's payload verbatim. The real shape wraps the address
    candidates in a `result` list (first = km4city street-number match)."""
    payload = {"result": [{"number": "3", "address": "VIA ZARA", "municipality": "FIRENZE"}]}
    client = make_client([make_result(structured=payload)])
    out = await reverse_geocode(client, 43.781834, 11.25891)
    assert out["result"][0]["address"] == "VIA ZARA"
    name, sent = client.calls[0]
    assert name == "coordinates_to_address"
    assert sent == {"latitude": 43.781834, "longitude": 11.25891}


# --- slim_result_for_llm (L12: shrink the model's context) -------------------

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
    assert "source_node" not in j and "destination_node" not in j  # no raw coordinates


# --- group_arc_legs (walk -> ride -> walk from the bus_route arc list) --------

def _multimodal_arcs():
    """The arc list bus_route._bus_arcs produces for a GTFS ride: foot -> board -> alight -> foot."""
    stops = [{"name": "PORTE NUOVE BELFIORE", "time": "2026-07-06T06:23:00Z"},
             {"name": "ACC. DEL CIMENTO ARTOM", "time": "2026-07-06T06:34:28Z"}]
    return [
        {"transport": "foot", "transport_provider": None, "desc": "a piedi 100 m", "distance": 0.1},
        {"transport": "bus", "transport_provider": "at - Firenze urbano", "desc": "linea 57 da PORTE NUOVE BELFIORE",
         "line": "57", "headsign": "CALENZANO UNIVERSITA'", "stops": stops, "start_datetime": "2026-07-06T06:23:00Z"},
        {"transport": "bus", "transport_provider": "at - Firenze urbano", "desc": "a ACC. DEL CIMENTO ARTOM",
         "end_datetime": "2026-07-06T06:34:28Z"},
        {"transport": "foot", "transport_provider": None, "desc": "a piedi 50 m", "distance": 0.05},
    ]


def test_group_arc_legs_walk_ride_walk():
    legs = group_arc_legs(_multimodal_arcs())
    # Three legs: walk, bus ride, walk (the two bus arcs merge, foot arcs bracket them).
    assert [leg.get("transport") for leg in legs] == ["foot", "bus", "foot"]
    walk_in, ride, walk_out = legs
    assert walk_in["distance_km"] == 0.1 and "provider" not in walk_in
    # The ride leg surfaces line / operator / headsign / full stops / board+alight times.
    assert ride["line"] == "57" and ride["provider"] == "at - Firenze urbano"
    assert ride["headsign"] == "CALENZANO UNIVERSITA'"
    assert [s["name"] for s in ride["stops"]] == ["PORTE NUOVE BELFIORE", "ACC. DEL CIMENTO ARTOM"]
    assert ride["from"] == "linea 57 da PORTE NUOVE BELFIORE"
    assert ride["start_datetime"] == "2026-07-06T06:23:00Z" and ride["end_datetime"] == "2026-07-06T06:34:28Z"
    assert walk_out["distance_km"] == 0.05


def test_slim_routing_multimodal_caps_stops():
    long_stops = [{"name": f"S{i}", "time": None} for i in range(STOPS_LLM_KEEP + 8)]
    full = {"journey": {"routes": [{
        "wkt": "LINESTRING(...)", "distance": 2.9, "time": "0:13:16",
        "arc": [
            {"transport": "foot", "transport_provider": None, "distance": 0.1, "desc": "a piedi 100 m"},
            {"transport": "bus", "transport_provider": "at", "line": "57", "stops": long_stops, "desc": "linea 57"},
        ],
    }]}}
    slim = slim_result_for_llm("routing", full)
    legs = slim["journey"]["legs"]
    assert [leg.get("transport") for leg in legs] == ["foot", "bus"]  # legs kept, not streets
    ride = legs[1]
    assert len(ride["stops"]) == STOPS_LLM_KEEP and ride["stops_total"] == STOPS_LLM_KEEP + 8


# --- contract: exposed tools exist in the live probe -------------------------

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
    # Local-only tools (bus_route) live on our own MCP server, not referente's, so they
    # are not expected in the referente probe.
    missing = TOOL_NAMES - LOCAL_ONLY_TOOLS - names
    assert not missing, f"exposed tools absent from probe: {missing}"
