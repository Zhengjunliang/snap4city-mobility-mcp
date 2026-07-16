"""Unit tests for gtfs_shapes — real GTFS shape geometry for bus ride legs (L51).

No network: _get_json is monkeypatched. Covers the variant matching (geometric score +
ordered projection cost picks the right direction), the shape cut (projections included,
original boundary vertices kept for the foot-leg joins), every degrade path (no line
name, no candidate, threshold miss, HTTP failure -> chords untouched), the base-name
line match ("T1" -> km4city "T1.3") and the process-level caches.
"""
from snap4city_mobility_mcp import gtfs_shapes
from snap4city_mobility_mcp.geo import fmt_linestring, wkt_points

# A ride along an east-west street with an n-shaped detour between the two stops: the
# router chord S1 -> S2 is straight (326 m), the true road runs 548 m through the detour.
# Both stops sit exactly on the shape; the shape extends beyond them on both sides so the
# cut must trim it.
S1 = (11.0, 43.0)
S2 = (11.004, 43.0)
DETOUR = (11.002, 43.001)
SHAPE_FWD = [
    (10.998, 43.0), (10.999, 43.0),
    S1,
    (11.001, 43.0), (11.002, 43.0), (11.002, 43.0005), DETOUR,
    (11.003, 43.001), (11.004, 43.001), (11.004, 43.0005),
    S2,
    (11.005, 43.0), (11.006, 43.0),
]
SHAPE_REV = list(reversed(SHAPE_FWD))
# Same geometry a kilometre north: scores far beyond MATCH_THRESHOLD_KM.
SHAPE_FAR = [(lng, lat + 0.01) for lng, lat in SHAPE_FWD]

# Path geometry the legs were cut from: walk / ride(S1->S2) / walk, boundary vertices
# shared exactly as mcp_server._leg_slices produces them.
PATH_PTS = [(10.9995, 43.0), S1, S2, (11.0045, 43.0)]


def _legs():
    return [
        {"type": "foot", "wkt": fmt_linestring(PATH_PTS[0:2])},
        {"type": "bus", "wkt": fmt_linestring(PATH_PTS[1:3])},
        {"type": "foot", "wkt": fmt_linestring(PATH_PTS[2:4])},
    ]


def _instructions(route_name="57"):
    legmap = {"type": "pt", "travelTime": 120000}
    if route_name is not None:
        legmap["route_name"] = route_name
    return [
        {"text": "Continue", "leg": None, "interval": [0, 1]},
        {"text": "Pt_start_trip", "leg": {"map": legmap}, "interval": [1, 2]},
        {"text": "Continue", "leg": None, "interval": [2, 3]},
    ]


def _install_fake_tpl(monkeypatch, *, agencies=(), lines=None, routes=None, raise_exc=None):
    """Swap gtfs_shapes._get_json for a scripted fake; return the recorded calls."""
    calls = []

    async def fake(path, params=None):
        calls.append((path, dict(params or {})))
        if raise_exc is not None:
            raise raise_exc
        if path == "/agencies/":
            return {"Agencies": [{"agency": a, "name": a} for a in agencies]}
        if path == "/bus-lines/":
            return {"BusLines": [
                {"agency": params["agency"], "shortName": sn}
                for sn in (lines or {}).get(params["agency"], [])
            ]}
        if path == "/bus-routes/":
            return {"BusRoutes": [
                {"line": params["line"], "route": "uri", "wktGeometry": fmt_linestring(shape)}
                for shape in (routes or {}).get((params["agency"], params["line"]), [])
            ]}
        raise AssertionError(f"unexpected tpl path {path!r}")

    monkeypatch.setattr(gtfs_shapes, "_get_json", fake)
    return calls


def _close(a, b, tol=1e-9):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


async def test_enhance_swaps_chord_for_shape_cut_and_picks_direction(monkeypatch):
    # Both direction variants are served (reverse first): the geometric score ties on the
    # shared roadbed, so the ordered projection cost must pick the forward one; the cut
    # keeps the original boundary vertices (foot-leg joins / stop pins) and pulls in the
    # detour vertices the chord skipped.
    _install_fake_tpl(
        monkeypatch,
        agencies=["ag1"],
        lines={"ag1": ["57"]},
        routes={("ag1", "57"): [SHAPE_REV, SHAPE_FWD]},
    )
    legs = _legs()
    changed = await gtfs_shapes.enhance_bus_legs(legs, _instructions(), len(PATH_PTS))
    assert changed is True
    pts = wkt_points(legs[1]["wkt"])
    assert pts[0] == S1 and pts[-1] == S2  # exact originals: adjacent legs still share them
    assert len(pts) > 2
    assert any(_close(p, DETOUR) for p in pts)  # the ride now follows the road
    # Walking legs untouched.
    assert wkt_points(legs[0]["wkt"]) == PATH_PTS[0:2]
    assert wkt_points(legs[2]["wkt"]) == PATH_PTS[2:4]


async def test_enhance_rejects_reverse_only_and_far_variants(monkeypatch):
    # Only the opposite-direction variant exists: its ordered projection cost exposes the
    # mismatch, the chord stays. Same for a same-name line a kilometre away.
    for shapes in ([SHAPE_REV], [SHAPE_FAR]):
        gtfs_shapes.reset_caches()
        _install_fake_tpl(
            monkeypatch, agencies=["ag1"], lines={"ag1": ["57"]},
            routes={("ag1", "57"): shapes},
        )
        legs = _legs()
        changed = await gtfs_shapes.enhance_bus_legs(legs, _instructions(), len(PATH_PTS))
        assert changed is False
        assert wkt_points(legs[1]["wkt"]) == [S1, S2]


async def test_enhance_without_line_name_touches_no_network(monkeypatch):
    # The live router always names the line; a leg.map without route_name (also the
    # existing unit fixtures) must short-circuit before any tpl call.
    calls = _install_fake_tpl(monkeypatch)
    legs = _legs()
    changed = await gtfs_shapes.enhance_bus_legs(legs, _instructions(route_name=None), len(PATH_PTS))
    assert changed is False and calls == []
    assert wkt_points(legs[1]["wkt"]) == [S1, S2]


async def test_enhance_degrades_on_http_failure_and_no_candidates(monkeypatch):
    # tpl down: enhance_bus_legs never raises, the chord stays.
    _install_fake_tpl(monkeypatch, raise_exc=RuntimeError("tpl down"))
    legs = _legs()
    assert await gtfs_shapes.enhance_bus_legs(legs, _instructions(), len(PATH_PTS)) is False
    assert wkt_points(legs[1]["wkt"]) == [S1, S2]
    # No agency serves the line: same degrade.
    gtfs_shapes.reset_caches()
    _install_fake_tpl(monkeypatch, agencies=["ag1"], lines={"ag1": ["23"]})
    legs = _legs()
    assert await gtfs_shapes.enhance_bus_legs(legs, _instructions(), len(PATH_PTS)) is False


async def test_base_name_match_queries_the_km4city_short_name(monkeypatch):
    # km4city suffixes sub-patterns ("T1.3") while the router names the base line ("T1"):
    # the candidate must be found and bus-routes queried with km4city's own shortName.
    calls = _install_fake_tpl(
        monkeypatch,
        agencies=["gest"],
        lines={"gest": ["T1.3"]},
        routes={("gest", "T1.3"): [SHAPE_FWD]},
    )
    legs = _legs()
    changed = await gtfs_shapes.enhance_bus_legs(legs, _instructions("T1"), len(PATH_PTS))
    assert changed is True
    route_calls = [p for p in calls if p[0] == "/bus-routes/"]
    assert route_calls and route_calls[0][1]["line"] == "T1.3"


async def test_lines_index_and_shapes_are_cached_until_reset(monkeypatch):
    calls = _install_fake_tpl(
        monkeypatch, agencies=["ag1"], lines={"ag1": ["57"]},
        routes={("ag1", "57"): [SHAPE_FWD]},
    )
    await gtfs_shapes.enhance_bus_legs(_legs(), _instructions(), len(PATH_PTS))
    await gtfs_shapes.enhance_bus_legs(_legs(), _instructions(), len(PATH_PTS))
    assert sum(1 for p in calls if p[0] == "/agencies/") == 1
    assert sum(1 for p in calls if p[0] == "/bus-routes/") == 1
    gtfs_shapes.reset_caches()
    await gtfs_shapes.enhance_bus_legs(_legs(), _instructions(), len(PATH_PTS))
    assert sum(1 for p in calls if p[0] == "/agencies/") == 2
