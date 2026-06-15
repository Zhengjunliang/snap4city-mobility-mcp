"""Unit tests for the deterministic TPL discovery chains (tpl.py).

Lean core suite: agency resolution (L21 — the brand "Autolinee Toscane" must
resolve to the Florence urban network, not the first-listed ExtraUrbano), the
happy routes chain, stop-name matching, the live BusStops payload shape, and
the timeline view (serving lines + honest "no timetable").
"""

from snap4city_mobility_mcp.tpl import (
    _match_stop,
    extract_tpl_data,
    run_tpl_flow,
    slim_tpl_result,
)

# Mirrors the live tpl_agencies catalogue (L21): NO single "Autolinee Toscane"
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
    """Observed tpl_routes_by_line item shape (L21): route URI under `route`,
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


def _stops_payload(wrapped=False):
    """Documented tpl_stops_by_route shape: [service URI array, GeoJSON]."""
    uris = ["http://s/1", "http://s/2"]
    feats = [{"properties": {"name": name}} for name in ("STAZIONE SMN", "SAN MARCO")]
    payload = [uris, {"type": "FeatureCollection", "features": feats}]
    return {"result": payload} if wrapped else payload


def _stops_payload_bus():
    """Live tpl_stops_by_route shape (L21): the GeoJSON is nested under `BusStops`
    (no top-level `type`), each feature's properties carry `name` + `serviceUri`."""
    uris = ["http://s/1", "http://s/2"]
    feats = [
        {"properties": {"name": "Novelli", "serviceUri": "http://s/1"}},
        {"properties": {"name": "San Marco", "serviceUri": "http://s/2"}},
    ]
    return [uris, {"BusStops": {"features": feats}}]


def _timeline_payload(timetable=None):
    """Live tpl_stop_timeline shape (L21): stop under `BusStop`, serving lines under
    `busLines.results.bindings`, `realtime`/`timetable` EMPTY in the observed run."""
    return {
        "BusStop": {"features": [{"properties": {"name": "Novelli", "code": "FM0083"}}]},
        "busLines": {"results": {"bindings": [
            {"busLine": {"value": "6"}, "lineDesc": {"value": "Novelli-Smn-Torregalli"},
             "lineUri": {"value": "http://l/6"}},
            {"busLine": {"value": "84"}, "lineDesc": {"value": "S.Marco Vecchio-Comparetti"},
             "lineUri": {"value": "http://l/84"}},
        ]}},
        "realtime": {},
        "timetable": timetable or {},
    }


# --- run_tpl_flow: agency resolution + chains (L21) ------------------------------

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
    # The brand "Autolinee Toscane" has no single entry; it must resolve to the Florence
    # urban network (888-48), NOT the first-listed ExtraUrbano Arezzo.
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=[{"shortName": "6"}]),
    ])
    await run_tpl_flow(client, {"intent": "tpl_lines", "agency_text": "Autolinee Toscane"})
    assert client.calls[1] == ("tpl_lines", {"agency": FI_URBAN_URI})


async def test_tpl_routes_happy_chain(make_client, make_result):
    client = make_client([
        make_result(structured=AGENCIES),
        make_result(structured=_routes()),
    ])
    out = await run_tpl_flow(client, {"intent": "tpl_routes", "line_text": "6"})
    assert out["unsupported"] is False
    assert client.calls[1] == ("tpl_routes_by_line", {"line": "6", "agency": FI_URBAN_URI})


# --- _match_stop / slim / extract (L21 live shapes) ------------------------------

def test_match_stop_user_words_subset_of_official_name():
    # Live stop names are longer than what users type ("San Marco" -> "Museo Di San Marco").
    entries = [{"name": "Museo Di San Marco", "uri": "u1"}, {"name": "Novelli", "uri": "u2"}]
    assert _match_stop(entries, "San Marco")["uri"] == "u1"
    assert _match_stop(entries, "Novelli")["uri"] == "u2"
    assert _match_stop(entries, "Vattelapesca") is None
    # exact token match preferred over a longer superset
    entries2 = [{"name": "San Marco Vecchio", "uri": "v"}, {"name": "San Marco", "uri": "exact"}]
    assert _match_stop(entries2, "San Marco")["uri"] == "exact"


def test_slim_tpl_stops_names_for_both_shapes():
    for payload in (_stops_payload(), _stops_payload(wrapped=True)):
        slim = slim_tpl_result("tpl_stops_by_route", payload)
        assert slim == {"count": 2, "stops": ["STAZIONE SMN", "SAN MARCO"]}
    # Live BusStops-wrapper shape (L21) — names now resolve (were None before).
    assert slim_tpl_result("tpl_stops_by_route", _stops_payload_bus()) == {
        "count": 2, "stops": ["Novelli", "San Marco"]
    }


def test_extract_tpl_data_timeline_real_shape_and_empty():
    results = [{"name": "tpl_stop_timeline", "result": _timeline_payload()}]
    data = extract_tpl_data("tpl_timeline", results)
    assert data["stop"] == "Novelli"
    assert [ln["line"] for ln in data["lines"]] == ["6", "84"]
    assert "timetable" not in data  # empty in the live probe → not surfaced (never invent times)
    assert extract_tpl_data("tpl_timeline", []) == {}
    assert extract_tpl_data("route", results) == {}
