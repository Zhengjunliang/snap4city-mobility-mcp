"""Unit tests for the local MCP server (mcp_server.py) — geocode + the all-modes route tool.

No network: httpx is monkeypatched. Covers the ServiceMap geocode (endpoint pick from
excludePOI, feature normalization, error path) and the What-If `route` tool (per-vehicle
request/response assembly, leg slicing, arc synthesis).
"""
import httpx

from snap4city_mobility_mcp import mcp_server
from snap4city_mobility_mcp.geo import wkt_length_km, wkt_points
from snap4city_mobility_mcp.mcp_server import (
    _bus_arcs,
    _fmt_hms,
    _journey_duration_ms,
    _leg_slices,
    _rome_local_iso,
    _normalize_feature,
    _servicemap_search,
    _street_arcs,
)


def _install_fake_httpx(monkeypatch, *, body=None, raise_exc=None):
    """Swap mcp_server.httpx.AsyncClient for a fake; return a dict capturing the GET url/params."""
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return body

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):  # accept follow_redirects= etc.
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["timeout"] = timeout
            if raise_exc is not None:
                raise raise_exc
            return _FakeResp()

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _FakeAsyncClient)
    return captured


def test_normalize_maps_name_to_address_for_fulltext():
    # Full-text features carry properties.name (no address/city/score); the client reads
    # `address`, so name must land there while the original name is kept. civic/serviceType
    # are absent on full-text hits but the shape stays uniform (None).
    feat = {"geometry": {"coordinates": [11.2556, 43.7731]}, "properties": {"name": "Duomo"}}
    out = _normalize_feature(feat)
    assert out["properties"]["address"] == "Duomo"
    assert out["properties"]["name"] == "Duomo"
    assert out["properties"]["civic"] is None and out["properties"]["serviceType"] is None
    assert out["geometry"]["coordinates"] == [11.2556, 43.7731]


def test_normalize_keeps_address_city_score_civic_for_location():
    # /location/ house-number hits carry serviceType "StreetNumber" + civic (L32); both
    # must survive normalization — _pick_feature narrows to the exact civic (L52).
    feat = {
        "geometry": {"coordinates": [11.25, 43.77]},
        "properties": {"address": "VIA ZARA", "city": "FIRENZE", "score": 12.6, "name": None,
                       "serviceType": "StreetNumber", "civic": "3"},
    }
    out = _normalize_feature(feat)["properties"]
    assert out == {"address": "VIA ZARA", "city": "FIRENZE", "score": 12.6, "name": None,
                   "civic": "3", "serviceType": "StreetNumber"}


async def test_search_excludepoi_true_hits_location_endpoint(monkeypatch):
    body = {"features": [{"geometry": {"coordinates": [11.25, 43.77]}, "properties": {"city": "FIRENZE"}}]}
    captured = _install_fake_httpx(monkeypatch, body=body)
    out = await _servicemap_search("via zara", excludePOI=True, lang="it", maxresults=100)
    assert captured["url"].endswith("/api/v1/location/")
    assert captured["params"]["search"] == "via zara"
    # No proximity bias params: probed 2026-07-09, selection/maxDists have zero effect on
    # text-search ordering — GPS-nearest picking is client-side (orchestrator._pick_coord).
    assert "selection" not in captured["params"] and "maxDists" not in captured["params"]
    assert out["type"] == "FeatureCollection" and out["count"] == 1


async def test_search_excludepoi_false_hits_fulltext_endpoint(monkeypatch):
    body = {"features": []}
    captured = _install_fake_httpx(monkeypatch, body=body)
    out = await _servicemap_search("Duomo", excludePOI=False, lang="it", maxresults=100)
    assert captured["url"].endswith("/api/v1")  # full-text base, no /location/
    assert out == {"type": "FeatureCollection", "features": [], "count": 0}


async def test_search_network_error_returns_error(monkeypatch):
    _install_fake_httpx(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    out = await _servicemap_search("Duomo", excludePOI=True, lang="it", maxresults=100)
    assert "error" in out and "servicemap search failed" in out["error"]


async def test_search_missing_feature_list_returns_error(monkeypatch):
    _install_fake_httpx(monkeypatch, body={"unexpected": "shape"})
    out = await _servicemap_search("Duomo", excludePOI=True, lang="it", maxresults=100)
    assert "error" in out


# --- _bus_arcs: turn the What-If GraphHopper turn-by-turn into journey arcs ---------------

def _pt_first():
    """A GTFS-loaded whatif-router path (shape from whatif-local/test-output.json):
    walk to the stop -> a Pt_start_trip ride carrying line/operator/headsign/stops -> walk on.
    Walking instructions carry distance (m) and time (ms); the ride carries leg.map.travelTime."""
    return {
        "instructions": [
            {"text": "Turn right", "street_name": "", "distance": 100.0, "time": 72000, "leg": None},
            {
                "text": "Pt_start_trip",
                "leg": {
                    "map": {
                        "type": "pt",
                        "route_name": "57",
                        "agency_name": "at - Firenze urbano",
                        "trip_headsign": "CALENZANO UNIVERSITA'",
                        "travelTime": 688000,
                        "stop": {
                            "myArrayList": [
                                {"map": {"stop_name": "PORTE NUOVE BELFIORE", "stop_arrivalTime": "2026-07-06T06:23:00Z"}},
                                {"map": {"stop_name": "CARRA SCARLATTI", "stop_arrivalTime": "2026-07-06T06:24:00Z"}},
                                {"map": {"stop_name": "ACC. DEL CIMENTO ARTOM", "stop_arrivalTime": "2026-07-06T06:34:28Z"}},
                            ]
                        },
                    }
                },
            },
            {"text": "Pt_end_trip", "street_name": "ACC. DEL CIMENTO ARTOM"},
            {"text": "Continue", "street_name": "", "distance": 50.0, "time": 36000, "leg": None},
        ]
    }


def test_bus_arcs_multimodal_walk_ride_walk():
    arc = _bus_arcs(_pt_first())
    # foot -> board -> alight -> foot: a real door-to-door multimodal journey.
    assert [a["transport"] for a in arc] == ["foot", "bus", "bus", "foot"]
    # Walk arcs carry distance in KM (matches route distance_km unit) and no provider.
    assert arc[0]["distance"] == 0.1 and arc[0]["transport_provider"] is None
    assert arc[3]["distance"] == 0.05
    # Boarding arc carries the raw line/headsign/full stop list for group_arc_legs to surface.
    board = arc[1]
    assert board["transport_provider"] == "at - Firenze urbano"
    assert board["line"] == "57" and board["headsign"] == "CALENZANO UNIVERSITA'"
    assert [s["name"] for s in board["stops"]] == ["PORTE NUOVE BELFIORE", "CARRA SCARLATTI", "ACC. DEL CIMENTO ARTOM"]
    # Line + boarding stop on the way in; alighting stop + headsign on the way out.
    assert "linea 57" in board["desc"] and "PORTE NUOVE BELFIORE" in board["desc"]
    assert "ACC. DEL CIMENTO ARTOM" in arc[2]["desc"] and "CALENZANO UNIVERSITA'" in arc[2]["desc"]
    # Scheduled stop times ride along as leg start/end datetimes, converted from the
    # router's UTC instants to Rome local (July = CEST, +02:00) so narrated orari are right.
    assert board["start_datetime"] == "2026-07-06T08:23:00+02:00"
    assert arc[2]["end_datetime"] == "2026-07-06T08:34:28+02:00"
    assert [s["time"] for s in board["stops"]] == [
        "2026-07-06T08:23:00+02:00", "2026-07-06T08:24:00+02:00", "2026-07-06T08:34:28+02:00"
    ]


def test_rome_local_iso():
    # Summer (CEST) and winter (CET) conversions from the router's UTC instants.
    assert _rome_local_iso("2026-07-06T06:23:00Z") == "2026-07-06T08:23:00+02:00"
    assert _rome_local_iso("2026-01-06T06:23:00Z") == "2026-01-06T07:23:00+01:00"
    # Unparseable / naive / non-string values pass through untouched (no guessed shift).
    assert _rome_local_iso("not a time") == "not a time"
    assert _rome_local_iso("2026-07-06T06:23:00") == "2026-07-06T06:23:00"
    assert _rome_local_iso(None) is None


def test_journey_duration_and_fmt():
    # Duration = walking time (72000 + 36000 ms) + ride travelTime (688000 ms) = 796000 ms.
    assert _journey_duration_ms(_pt_first()) == 796000
    assert _fmt_hms(796000) == "0:13:16"  # 796 s = 13 min 16 s
    assert _fmt_hms(0) == "0:00:00"


def test_wkt_length_km():
    # A ~1 km north hop in Florence (0.009 deg lat ≈ 1 km). Parses "lng lat" pairs, sums haversine.
    d = wkt_length_km("LINESTRING (11.2558 43.7731, 11.2558 43.7821)")
    assert d is not None and 0.9 < d < 1.1
    assert wkt_length_km("not a linestring") is None


def test_wkt_points_and_leg_slices_single_ride():
    # instruction `interval` indexes the WKT vertices (live-verified, L44): a ride over
    # vertices 2..4 cuts the line into walk / ride / walk, boundary vertices shared so the
    # drawn segments connect.
    wkt = "LINESTRING (11.0 43.0, 11.001 43.0, 11.002 43.0, 11.003 43.0, 11.004 43.0, 11.005 43.0)"
    pts = wkt_points(wkt)
    assert pts is not None and len(pts) == 6 and pts[0] == (11.0, 43.0)
    ins = [
        {"text": "Continue", "interval": [0, 2], "leg": None},
        {"text": "Pt_start_trip", "interval": [2, 4], "leg": {"map": {"type": "pt"}}},
        {"text": "Continue", "interval": [4, 5], "leg": None},
    ]
    legs = _leg_slices(ins, pts)
    assert [leg["type"] for leg in legs] == ["foot", "bus", "foot"]
    assert legs[0]["wkt"] == "LINESTRING (11.0 43.0, 11.001 43.0, 11.002 43.0)"
    assert legs[1]["wkt"] == "LINESTRING (11.002 43.0, 11.003 43.0, 11.004 43.0)"
    assert legs[2]["wkt"] == "LINESTRING (11.004 43.0, 11.005 43.0)"
    assert wkt_points("not a linestring") is None


def test_leg_slices_transfer_and_defenses():
    pts = [(float(i), 0.0) for i in range(10)]
    # Two rides (a transfer): walk / ride / walk / ride / walk.
    legs = _leg_slices(
        [{"leg": {"map": {}}, "interval": [1, 3]}, {"leg": {"map": {}}, "interval": [5, 8]}],
        pts,
    )
    assert [leg["type"] for leg in legs] == ["foot", "bus", "foot", "bus", "foot"]
    # Walking-only journey (no instruction carries a leg dict): no slices at all.
    assert _leg_slices([{"text": "walk", "leg": None, "interval": [0, 9]}], pts) == []
    # Defensive: malformed interval skipped, out-of-range end clamped to the last vertex.
    assert _leg_slices([{"leg": {"map": {}}, "interval": None}], pts) == []
    clamped = _leg_slices([{"leg": {"map": {}}, "interval": [8, 99]}], pts)
    assert [leg["type"] for leg in clamped] == ["foot", "bus"]
    assert clamped[-1]["wkt"] == "LINESTRING (8.0 0.0, 9.0 0.0)"


def test_bus_arcs_walk_only_yields_foot_arcs():
    # No PT ride in the itinerary (router has no GTFS, or — GTFS loaded — walking beats any
    # bus on a short trip, L39): honest foot arcs only, NEVER synthetic bus arcs. Downstream
    # _pt_is_foot_only then flags the journey and it is presented as a walk, not a fake bus.
    first = {
        "instructions": [
            {"text": "Turn right onto Via della Scala", "street_name": "Via della Scala", "distance": 200.0, "time": 144000, "leg": None},
            {"text": "Continue onto Viale Belfiore", "street_name": "Viale Belfiore", "distance": 300.0, "time": 216000, "leg": None},
        ]
    }
    arc = _bus_arcs(first)
    assert arc == [
        {"transport": "foot", "transport_provider": None, "desc": "a piedi 500 m", "distance": 0.5}
    ]


def test_bus_arcs_empty_instructions_yield_no_arcs():
    # Nothing to walk, nothing to ride: empty arc list (group_arc_legs/_pt_is_foot_only
    # treat it as foot-only), no placeholder invented.
    assert _bus_arcs({"instructions": []}) == []


# --- the unified `route` tool (foot/car/bus share one request/response shape) -------------

def test_street_arcs_names_streets_per_instruction():
    # One arc per instruction, desc = street name ("nd" when unnamed — the slim streets
    # view drops it), transport labeled with the vehicle, distance in km.
    first = {"instructions": [
        {"text": "Continue onto Via dei Benci", "street_name": "Via dei Benci", "distance": 356.97, "time": 42838},
        {"text": "Turn left", "street_name": "", "distance": 12.0, "time": 1400},
    ]}
    assert _street_arcs(first, "car") == [
        {"transport": "car", "transport_provider": None, "desc": "Via dei Benci", "distance": 0.357},
        {"transport": "car", "transport_provider": None, "desc": "nd", "distance": 0.012},
    ]


async def test_route_foot_uses_path_totals(monkeypatch):
    # foot/car never touch the PT graph: distance/time come straight from the path's true
    # totals (the PT walking-access caveat doesn't apply), no legs, street arcs for the
    # respond narration, generic HTTP timeout.
    body = {"paths": [{
        "wkt": "LINESTRING (11.0 43.0, 11.001 43.0)",
        "distance": 1644.3, "time": 1183906,
        "instructions": [
            {"text": "Continue onto Via dei Benci", "street_name": "Via dei Benci", "distance": 1644.3, "time": 1183906, "interval": [0, 1]},
        ],
    }]}
    captured = _install_fake_httpx(monkeypatch, body=body)
    out = await mcp_server.route.fn(43.0, 11.0, 43.0, 11.001, vehicle="foot")
    assert captured["params"]["vehicle"] == "foot"
    assert "startDatetime" not in captured["params"]
    assert captured["timeout"] == mcp_server.HTTP_TIMEOUT_S
    found = out["journey"]["routes"][0]
    assert found["distance"] == 1.644 and found["time"] == "0:19:43"
    assert "legs" not in found
    assert found["arc"][0]["desc"] == "Via dei Benci" and found["arc"][0]["transport"] == "foot"


async def test_route_bus_forwards_datetime_and_slices_legs(monkeypatch):
    # bus keeps the PT semantics: GTFS startDatetime forwarded, the dedicated long timeout
    # (graph reload until the singleton patch lands), distance measured on the geometry
    # (paths[0].distance counts only walking access), duration summed from instructions,
    # and the walk/ride leg slices attached for the dashboard split (L44).
    wkt = "LINESTRING (11.0 43.0, 11.001 43.0, 11.002 43.0, 11.003 43.0)"
    body = {"paths": [{
        "wkt": wkt, "distance": 160.0, "time": 999999,
        "instructions": [
            {"text": "Continue", "street_name": "", "distance": 80.0, "time": 57600, "leg": None, "interval": [0, 1]},
            {"text": "Pt_start_trip", "interval": [1, 2], "leg": {"map": {"type": "pt", "travelTime": 120000}}},
            {"text": "Continue", "street_name": "", "distance": 80.0, "time": 57600, "leg": None, "interval": [2, 3]},
        ],
    }]}
    captured = _install_fake_httpx(monkeypatch, body=body)
    out = await mcp_server.route.fn(43.0, 11.0, 43.0, 11.003, vehicle="bus", startdatetime="2026-07-13T10:00")
    assert captured["params"]["vehicle"] == "bus"
    assert captured["params"]["startDatetime"] == "2026-07-13T10:00"
    assert captured["timeout"] == mcp_server.BUS_ROUTE_TIMEOUT_S
    found = out["journey"]["routes"][0]
    assert [leg["type"] for leg in found["legs"]] == ["foot", "bus", "foot"]
    assert found["distance"] == wkt_length_km(wkt)  # geometry length, not the 160 m access metres
    assert found["time"] == _fmt_hms(57600 + 120000 + 57600)


async def test_route_bus_swaps_ride_chords_for_gtfs_shape(monkeypatch):
    # With the tpl API answering, the ride leg's chord geometry is replaced by the real
    # shape cut (L51) and the route-level wkt/distance are re-derived from the legs so
    # the mirror matches what the dashboard draws. Walking legs stay router geometry.
    from tests.test_gtfs_shapes import PATH_PTS, S1, S2, SHAPE_FWD, _install_fake_tpl

    from snap4city_mobility_mcp.geo import fmt_linestring
    wkt = fmt_linestring(PATH_PTS)
    body = {"paths": [{
        "wkt": wkt, "distance": 160.0,
        "instructions": [
            {"text": "Continue", "street_name": "", "distance": 80.0, "time": 57600, "leg": None, "interval": [0, 1]},
            {"text": "Pt_start_trip", "interval": [1, 2],
             "leg": {"map": {"type": "pt", "travelTime": 120000, "route_name": "57"}}},
            {"text": "Continue", "street_name": "", "distance": 80.0, "time": 57600, "leg": None, "interval": [2, 3]},
        ],
    }]}
    _install_fake_httpx(monkeypatch, body=body)
    _install_fake_tpl(
        monkeypatch, agencies=["ag1"], lines={"ag1": ["57"]},
        routes={("ag1", "57"): [SHAPE_FWD]},
    )
    out = await mcp_server.route.fn(43.0, 10.9995, 43.0, 11.0045, vehicle="bus")
    found = out["journey"]["routes"][0]
    ride = wkt_points(found["legs"][1]["wkt"])
    assert ride[0] == S1 and ride[-1] == S2 and len(ride) > 2  # boundary vertices kept
    # Mirror re-derived: full wkt spans door to door through the shape, distance grew
    # beyond the chord length.
    full = wkt_points(found["wkt"])
    assert full[0] == PATH_PTS[0] and full[-1] == PATH_PTS[-1]
    assert found["distance"] > wkt_length_km(wkt)


async def test_route_bus_keeps_chords_when_tpl_is_down(monkeypatch):
    # tpl unreachable: the output is byte-identical to the pre-enhancement behavior.
    from tests.test_gtfs_shapes import PATH_PTS, S1, S2, _install_fake_tpl

    from snap4city_mobility_mcp.geo import fmt_linestring
    wkt = fmt_linestring(PATH_PTS)
    body = {"paths": [{
        "wkt": wkt, "distance": 160.0,
        "instructions": [
            {"text": "Continue", "street_name": "", "distance": 80.0, "time": 57600, "leg": None, "interval": [0, 1]},
            {"text": "Pt_start_trip", "interval": [1, 2],
             "leg": {"map": {"type": "pt", "travelTime": 120000, "route_name": "57"}}},
            {"text": "Continue", "street_name": "", "distance": 80.0, "time": 57600, "leg": None, "interval": [2, 3]},
        ],
    }]}
    _install_fake_httpx(monkeypatch, body=body)
    _install_fake_tpl(monkeypatch, raise_exc=RuntimeError("tpl down"))
    out = await mcp_server.route.fn(43.0, 10.9995, 43.0, 11.0045, vehicle="bus")
    found = out["journey"]["routes"][0]
    assert found["wkt"] == wkt
    assert wkt_points(found["legs"][1]["wkt"]) == [S1, S2]
    assert found["distance"] == wkt_length_km(wkt)


async def test_route_rejects_unknown_vehicle_and_surfaces_errors(monkeypatch):
    out = await mcp_server.route.fn(43.0, 11.0, 43.0, 11.001, vehicle="boat")
    assert "unsupported vehicle" in out["error"]
    _install_fake_httpx(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    out = await mcp_server.route.fn(43.0, 11.0, 43.0, 11.001, vehicle="car")
    assert "whatif-router car route failed" in out["error"]
    _install_fake_httpx(monkeypatch, body={"paths": []})
    out = await mcp_server.route.fn(43.0, 11.0, 43.0, 11.001, vehicle="foot")
    assert out["error"] == "whatif-router returned no foot path"
