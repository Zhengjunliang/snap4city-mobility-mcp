"""Unit tests for the deterministic MCP layer (mcp_tools.py).

Lean core suite: the two-pass geocode with named-city preference (L17/L41, worldwide
since the Tuscany bbox removal), the nearest-service parsing, the LLM-context slimming
(L12), and the contract that every exposed tool exists in the live probe.
"""
import json
import pathlib

import pytest

from snap4city_mobility_mcp.mcp_tools import (
    LOCAL_ONLY_TOOLS,
    STOPS_LLM_KEEP,
    TOOL_NAMES,
    _unwrap,
    exec_tool,
    group_arc_legs,
    parse_service_features,
    reverse_geocode,
    slim_result_for_llm,
)


# --- _unwrap -----------------------------------------------------------------

def test_unwrap_structured(make_result):
    assert _unwrap(make_result(structured={"a": 1})) == {"a": 1}


# --- exec_tool: geocode two-pass + named-city preference (L17/L41) -----------

def _feature(lng, lat, addr="x", city="FIRENZE"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {"address": addr, "city": city}}


async def test_exec_tool_geocode_named_city_wins_first_pass(make_client, make_result):
    """A city the user names narrows the address pass outright (no POI pass sent),
    regardless of the server's score order."""
    fc = {"type": "FeatureCollection", "count": 2, "features": [
        _feature(11.2560, 43.7714, "VIA ROMA", "FIRENZE"),
        _feature(10.2270, 43.9580, "VIA ROMA", "PIETRASANTA"),
    ]}
    client = make_client([make_result(structured=fc)])
    out = await exec_tool(client, "address_search_location", {"search": "via Roma, Pietrasanta"})
    assert out["count"] == 1
    assert out["features"][0]["properties"]["city"] == "PIETRASANTA"
    assert len(client.calls) == 1
    _, sent = client.calls[0]
    assert sent["excludePOI"] is True  # addresses first — POIs only as fallback (L17)
    assert sent["lang"] == "it" and sent["logic"] == "or"  # Italian-dataset bias on every pass


async def test_exec_tool_geocode_no_city_keeps_all_hits_worldwide(make_client, make_result):
    """No city named → no narrowing and no region filter: every hit (foreign included)
    survives for the caller's GPS-nearest picking (_pick_coord). Both passes run
    (the address pass is not city-confident), address hits win over POI ones."""
    addresses = {"type": "FeatureCollection", "count": 3, "features": [
        _feature(-0.3068184, 39.59272, "PIAZZA DUOMO", "VALENCIA"),
        _feature(11.2560, 43.7714, "PIAZZA DUOMO", "FIRENZE"),
        _feature(10.2270, 43.9580, "PIAZZA DUOMO", "PIETRASANTA"),
    ]}
    poi = {"type": "FeatureCollection", "count": 1, "features": [_feature(11.0, 43.0, "poi hit", "PRATO")]}
    client = make_client([make_result(structured=addresses), make_result(structured=poi)])
    out = await exec_tool(client, "address_search_location", {"search": "piazza Duomo"})
    assert out["count"] == 3  # nothing dropped — no Florence default, no bbox
    assert [f["properties"]["city"] for f in out["features"]] == ["VALENCIA", "FIRENZE", "PIETRASANTA"]
    assert [sent["excludePOI"] for _, sent in client.calls] == [True, False]


async def test_exec_tool_geocode_falls_back_to_poi_pass(make_client, make_result):
    """L17: address pass comes back empty (stations/landmarks are POI-only)
    → the POI pass (excludePOI=false) provides the result."""
    empty = {"type": "FeatureCollection", "count": 0, "features": []}
    poi = {"type": "FeatureCollection", "count": 1, "features": [_feature(11.2482, 43.8047)]}
    client = make_client([make_result(structured=empty), make_result(structured=poi)])
    out = await exec_tool(client, "address_search_location", {"search": "stazione di Firenze Rifredi"})
    assert out["count"] == 1
    assert out["features"][0]["geometry"]["coordinates"] == [11.2482, 43.8047]
    assert [sent["excludePOI"] for _, sent in client.calls] == [True, False]


# --- parse_service_features (nearest-category search envelope) ----------------

def test_parse_service_features_reads_nested_envelope():
    """The live near-search envelope nests distance-sorted features under Services;
    the parser keeps that order (the caller takes [0] as the nearest) and maps
    name/coords/uri. free_spaces stays None for non-parking services."""
    result = {"result": [["uri1", "uri2"], {"Services": {"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": [11.25459, 43.77349]},
         "properties": {"name": "Farmacia ALL INSEGNA DEL MORO", "distance": "0.106",
                        "serviceUri": "http://km4city/f1"}},
        {"geometry": {"type": "Point", "coordinates": [11.25440, 43.77320]},
         "properties": {"name": "Farmacia S. ANTONINO", "distance": "0.1128",
                        "serviceUri": "http://km4city/f2"}},
    ]}}]}
    spots = parse_service_features(result)
    assert [s["name"] for s in spots] == ["Farmacia ALL INSEGNA DEL MORO", "Farmacia S. ANTONINO"]
    assert spots[0]["lng"] == 11.25459 and spots[0]["lat"] == 43.77349
    assert spots[0]["uri"] == "http://km4city/f1"
    assert spots[0]["free_spaces"] is None


def test_parse_service_features_error_gives_empty():
    assert parse_service_features({"error": "boom"}) == []
    assert parse_service_features(None) == []


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


# --- group_arc_legs (walk -> ride -> walk from the route tool's arc list) -----

def _multimodal_arcs():
    """The arc list the route tool's _bus_arcs produces for a GTFS ride: foot -> board -> alight -> foot.
    Times are Rome local with offset (mcp_server converts the router's UTC instants)."""
    stops = [{"name": "PORTE NUOVE BELFIORE", "time": "2026-07-06T08:23:00+02:00"},
             {"name": "ACC. DEL CIMENTO ARTOM", "time": "2026-07-06T08:34:28+02:00"}]
    return [
        {"transport": "foot", "transport_provider": None, "desc": "a piedi 100 m", "distance": 0.1},
        {"transport": "bus", "transport_provider": "at - Firenze urbano", "desc": "linea 57 da PORTE NUOVE BELFIORE",
         "line": "57", "headsign": "CALENZANO UNIVERSITA'", "stops": stops, "start_datetime": "2026-07-06T08:23:00+02:00"},
        {"transport": "bus", "transport_provider": "at - Firenze urbano", "desc": "a ACC. DEL CIMENTO ARTOM",
         "end_datetime": "2026-07-06T08:34:28+02:00"},
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
    assert ride["start_datetime"] == "2026-07-06T08:23:00+02:00" and ride["end_datetime"] == "2026-07-06T08:34:28+02:00"
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
    # Local-only tools (route) live on our own MCP server, not referente's, so they
    # are not expected in the referente probe.
    missing = TOOL_NAMES - LOCAL_ONLY_TOOLS - names
    assert not missing, f"exposed tools absent from probe: {missing}"
