"""Unit tests for the deterministic TPL discovery chains (tpl.py)."""

from snap4city_mobility_mcp.tpl import (
    _unwrap_tpl,
    extract_tpl_data,
    run_tpl_flow,
    slim_tpl_result,
    tpl_template_answer,
)

# Mirrors the live tpl_agencies catalogue (probe_tpl STEP 1): NO single "Autolinee Toscane"
# entry — only per-network sub-agencies, with ExtraUrbano Arezzo ordered BEFORE the Florence
# urban one (the regression guard: the empty/brand default must skip Arezzo for 888-48).
TRENITALIA_URI = "http://www.disit.org/km4city/resource/Bus_roma_Agency_OP3"
FI_URBAN_URI = (
    "http://www.disit.org/km4city/resource/48-UrbanoAreaMetropolitanaFiorentina-gtfs_Agency_888-48"
)
AGENCIES = {"agencies": [
    {"name": "Trenitalia", "uri": TRENITALIA_URI},
    {"name": "Autolinee Toscane - ExtraUrbano Arezzo",
     "uri": "http://www.disit.org/km4city/resource/28-ExtraurbanoArezzo-gtfs_Agency_888-28"},
    {"name": "Autolinee Toscane - Urbano Area Metropolitana Fiorentina", "uri": FI_URBAN_URI},
]}


def _routes(n=3):
    """Observed tpl_routes_by_line item shape (probe_tpl STEP 5): route URI under `route`,
    geometry under `wktGeometry` (NOT `wkt`)."""
    return [
        {
            "line": "6",
            "route": f"http://r/{i}",
            "firstBusStop": "Novelli",
            "lastBusStop": "Ospedale Torre Galli",
            "routeName": "",
            "wktGeometry": "LINESTRING(1 2,3 4)",
        }
        for i in range(n)
    ]


def _stops_payload(wrapped=False, with_service_uri=False):
    """Documented tpl_stops_by_route shape: [service URI array, GeoJSON]."""
    uris = ["http://s/1", "http://s/2"]
    feats = []
    for uri, name in zip(uris, ("STAZIONE SMN", "SAN MARCO")):
        props = {"name": name}
        if with_service_uri:
            props["serviceUri"] = uri
        feats.append({"properties": props})
    payload = [uris, {"type": "FeatureCollection", "features": feats}]
    return {"result": payload} if wrapped else payload


# --- _unwrap_tpl ---------------------------------------------------------------

def test_unwrap_tpl_accepts_both_shapes():
    bare = [1, 2]
    assert _unwrap_tpl(bare) == bare                       # documented bare shape
    assert _unwrap_tpl({"result": bare}) == bare           # FastMCP non-object wrap
    multi = {"result": bare, "count": 2}                   # NOT a pure wrapper
    assert _unwrap_tpl(multi) == multi
    assert _unwrap_tpl({"error": "boom"}) == {"error": "boom"}


# --- run_tpl_flow: agency resolution + chains -----------------------------------

async def test_tpl_lines_defaults_to_florence_agency(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=[{"shortName": "6"}, {"shortName": "14"}]),
    ])
    out = await run_tpl_flow(client, {"intent": "tpl_lines", "agency_text": ""})
    assert out["unsupported"] is False
    # Empty text → Florence urban (888-48), skipping the earlier-listed ExtraUrbano Arezzo.
    assert client.calls == [("tpl_agencies", {}), ("tpl_lines", {"agency": FI_URBAN_URI})]


async def test_tpl_lines_brand_autolinee_toscane_prefers_florence(make_client, make_result):
    # Reproduces chat test 7: the brand "Autolinee Toscane" has no single entry; it must
    # resolve to the Florence urban network (888-48), NOT the first ExtraUrbano Arezzo.
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=[{"shortName": "6"}]),
    ])
    await run_tpl_flow(client, {"intent": "tpl_lines", "agency_text": "Autolinee Toscane"})
    assert client.calls[1] == ("tpl_lines", {"agency": FI_URBAN_URI})


async def test_tpl_lines_matches_named_agency(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=[]),
    ])
    await run_tpl_flow(client, {"intent": "tpl_lines", "agency_text": "linee di Trenitalia"})
    assert client.calls[1][1]["agency"] == TRENITALIA_URI


async def test_tpl_lines_unknown_agency_stops_after_listing(make_client, make_result):
    client = make_client([make_result(structured=AGENCIES)])
    out = await run_tpl_flow(client, {"intent": "tpl_lines", "agency_text": "FooBus"})
    # Chain stops — respond gets the agencies audit entry and asks the user to pick.
    assert [n for n, _ in client.calls] == ["tpl_agencies"]
    assert out["unsupported"] is False


async def test_tpl_routes_missing_line_skips_chain(make_client):
    client = make_client([])
    out = await run_tpl_flow(client, {"intent": "tpl_routes", "line_text": ""})
    assert out["unsupported"] is True
    assert client.calls == []


async def test_tpl_routes_happy_chain(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=_routes()),
    ])
    out = await run_tpl_flow(client, {"intent": "tpl_routes", "line_text": "6"})
    assert out["unsupported"] is False
    assert client.calls[1] == ("tpl_routes_by_line", {"line": "6", "agency": FI_URBAN_URI})


async def test_tpl_stops_probes_first_two_routes_only(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=_routes(3)),
        make_result(structured=_stops_payload()),
        make_result(structured=_stops_payload()),
    ])
    out = await run_tpl_flow(client, {"intent": "tpl_stops", "line_text": "6"})
    stops_calls = [args for n, args in client.calls if n == "tpl_stops_by_route"]
    assert stops_calls == [{"route": "http://r/0"}, {"route": "http://r/1"}]  # capped at 2
    assert out["unsupported"] is False


async def test_tpl_timeline_matches_stop_by_positional_uri(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=_routes(1)),
        make_result(structured=_stops_payload()),
        make_result(structured=[{"time": "10:15"}]),
    ])
    await run_tpl_flow(
        client, {"intent": "tpl_timeline", "line_text": "6", "stop_text": "fermata San Marco"}
    )
    assert client.calls[-1] == ("tpl_stop_timeline", {"stop": "http://s/2"})


async def test_tpl_timeline_wrapped_payload_and_service_uri(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=_routes(1)),
        make_result(structured=_stops_payload(wrapped=True, with_service_uri=True)),
        make_result(structured=[{"time": "10:15"}]),
    ])
    await run_tpl_flow(
        client, {"intent": "tpl_timeline", "line_text": "6", "stop_text": "San Marco"}
    )
    assert client.calls[-1] == ("tpl_stop_timeline", {"stop": "http://s/2"})


async def test_tpl_timeline_no_stop_match_skips_timeline(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=_routes(1)),
        make_result(structured=_stops_payload()),
    ])
    out = await run_tpl_flow(
        client, {"intent": "tpl_timeline", "line_text": "6", "stop_text": "Vattelapesca"}
    )
    assert "tpl_stop_timeline" not in [n for n, _ in client.calls]
    assert out["unsupported"] is False  # respond explains via the stops list


# --- slim_tpl_result (L12 caps) --------------------------------------------------

def test_slim_tpl_lines_caps_and_counts():
    items = [{"shortName": str(i)} for i in range(100)]
    slim = slim_tpl_result("tpl_lines", items)
    assert slim["count"] == 100
    assert len(slim["lines"]) == 30


def test_slim_tpl_routes_drops_geometry():
    slim = slim_tpl_result("tpl_routes_by_line", _routes(2))
    assert slim["count"] == 2
    assert all("wktGeometry" not in r and "wkt" not in r for r in slim["routes"])
    assert slim["routes"][0]["line"] == "6"  # non-geometry fields kept


def test_slim_tpl_stops_names_for_both_shapes():
    for payload in (_stops_payload(), _stops_payload(wrapped=True)):
        slim = slim_tpl_result("tpl_stops_by_route", payload)
        assert slim == {"count": 2, "stops": ["STAZIONE SMN", "SAN MARCO"]}


def test_slim_tpl_error_passthrough():
    assert slim_tpl_result("tpl_lines", {"error": "boom"}) == {"error": "boom"}


# --- extract_tpl_data (widget payload) -------------------------------------------

def test_extract_tpl_data_lines_and_routes():
    results = [
        {"name": "tpl_agencies", "result": AGENCIES},
        {"name": "tpl_lines", "result": [{"shortName": "6"}]},
    ]
    assert extract_tpl_data("tpl_lines", results) == {"lines": [{"shortName": "6"}]}
    results = [{"name": "tpl_routes_by_line", "result": _routes(2)}]
    data = extract_tpl_data("tpl_routes", results)
    assert len(data["routes"]) == 2
    assert data["routes"][0]["wktGeometry"]  # geometry kept for the map widget


def test_extract_tpl_data_stops_dedupes_across_routes():
    results = [
        {"name": "tpl_stops_by_route", "result": _stops_payload()},
        {"name": "tpl_stops_by_route", "result": _stops_payload()},  # same stops, other direction
    ]
    data = extract_tpl_data("tpl_stops", results)
    assert [s["uri"] for s in data["stops"]] == ["http://s/1", "http://s/2"]


def test_extract_tpl_data_timeline_and_empty():
    results = [{"name": "tpl_stop_timeline", "result": [{"time": "10:15"}]}]
    assert extract_tpl_data("tpl_timeline", results) == {"timeline": [{"time": "10:15"}]}
    assert extract_tpl_data("tpl_timeline", []) == {}
    assert extract_tpl_data("route", results) == {}


# --- tpl_template_answer ----------------------------------------------------------

def test_tpl_template_answers():
    assert "6" in tpl_template_answer("tpl_lines", {"lines": [{"shortName": "6"}]})
    assert "Fermate" in tpl_template_answer("tpl_stops", {"stops": [{"name": "SAN MARCO", "uri": "u"}]})
    assert "passaggi" in tpl_template_answer("tpl_timeline", {"timeline": [{"time": "10:15"}]})
    assert tpl_template_answer("tpl_lines", {}) is None
