"""Real GTFS shape geometry for bus ride legs, from the public km4city tpl API.

The What-If GraphHopper router draws a PT ride as one vertex per stop — straight chords
that cut diagonally across blocks — because its GTFS importer ignores shapes.txt (L44).
The km4city ServiceMap ingests the same regional GTFS *with* shapes and serves them per
line variant (tpl/bus-routes?geometry=true, probed 2026-07-16: dense wktGeometry
LINESTRINGs, no auth — the same data Gea-Night's GTFS visualizations draw). This module
swaps each ride leg's chord geometry for the matching variant's shape, cut between the
board and alight stops.

It is internal to the local `route` tool (no new MCP tool, no new payload fields): legs
keep their [{type, wkt}] shape, only the bus wkt content improves — so the orchestrator
pass-through and the dashboard drawing need zero changes, exactly the seam L44 left.

Matching is geometric, not by id: the router's trip_id/route_id name *its* feed snapshot
while the tpl URIs name km4city's own ingest, so the only keys shared reliably are the
line short name and the stop coordinates themselves. A candidate variant is accepted
only when the ride's stop vertices all hug its shape (mean distance under
MATCH_THRESHOLD_KM) and its board/alight projections land close *in shape order* (which
is what tells the two directions of a line apart — both directions score alike on a
shared roadbed, but the reverse one projects board after alight). Any failure — network,
no candidate line, threshold miss, budget timeout — leaves the original chord geometry
in place: a degrade back to today's drawing, never an error.
"""
import asyncio
import logging
import math
import os
from typing import Any

import httpx

from snap4city_mobility_mcp.geo import fmt_linestring, haversine_km, wkt_points

logger = logging.getLogger(__name__)

# Public km4city tpl (GTFS) API — agencies / bus-lines / bus-routes(+geometry).
# Same host family as the geocoder in mcp_server; override for other deployments.
TPL_API_URL = os.environ.get(
    "S4C_TPL_API_URL", "https://servicemap.snap4city.org/WebAppGrafo/api/v1/tpl"
)
HTTP_TIMEOUT_S = 15.0
# Whole-enhancement budget per route call: a bus turn already runs 30-45s (PT graph
# reload, L46/L47), so a bounded extra wait is acceptable; past it, every leg not yet
# enhanced keeps its chords.
ENHANCE_BUDGET_S = 15.0
# Mean stop-vertex -> shape distance to accept a variant. Stops sit on the roadside and
# the tpl shapes are dense, so a true match scores well under this; another city's
# same-numbered line scores hundreds of metres above.
MATCH_THRESHOLD_KM = 0.05
_KM_PER_DEG = 111.32  # equirectangular scale; fine at Tuscany trip sizes (<50 km)

# Process-level caches: the GTFS ingest behind the tpl API is static, like the geocode
# index (same reasoning as mcp_tools' geocode LRU). reset_caches() exists for the tests'
# autouse fixture — a stale entry would silently bypass a queued fake response.
_lines_index: dict[str, list[str]] | None = None  # agency uri -> shortNames (as served)
_shapes_cache: dict[tuple[str, str], list[list[tuple[float, float]]]] = {}


def reset_caches() -> None:
    """Empty the module caches (tests: same autouse reasoning as geocode_cache_clear)."""
    global _lines_index
    _lines_index = None
    _shapes_cache.clear()


async def _get_json(path: str, params: dict[str, str] | None = None) -> Any:
    """GET {TPL_API_URL}{path} -> parsed JSON body. Raises on any HTTP/parse failure."""
    async with httpx.AsyncClient(follow_redirects=True) as h:
        resp = await h.get(f"{TPL_API_URL}{path}", params=params, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()


async def _agency_lines(agency: str) -> tuple[str, list[str]]:
    """(agency uri, its line shortNames). A failing agency just contributes none."""
    try:
        body = await _get_json("/bus-lines/", {"agency": agency})
        items = body.get("BusLines") if isinstance(body, dict) else None
        names = [
            str(it["shortName"])
            for it in items or []
            if isinstance(it, dict) and it.get("shortName")
        ]
    except Exception as e:  # noqa: BLE001 - one broken agency must not kill the index
        logger.debug("tpl bus-lines %s failed: %s", agency, e)
        names = []
    return agency, names


async def _load_lines_index() -> dict[str, list[str]]:
    """agency uri -> line shortNames, fetched once per process (one agencies call plus
    one bus-lines call per agency, gathered). A failure here propagates (the caller's
    catch-all keeps the legs as chords) and is NOT cached, so a later turn retries."""
    global _lines_index
    if _lines_index is None:
        body = await _get_json("/agencies/")
        agencies = [
            a["agency"]
            for a in (body.get("Agencies") or [] if isinstance(body, dict) else [])
            if isinstance(a, dict) and a.get("agency")
        ]
        pairs = await asyncio.gather(*(_agency_lines(a) for a in agencies))
        _lines_index = {agency: names for agency, names in pairs if names}
    return _lines_index


async def _line_shapes(agency: str, line: str) -> list[list[tuple[float, float]]]:
    """Parsed shape vertex lists for every variant of (agency, line), cached.

    The tpl route entry's wktGeometry is the full line-variant shape (one per direction/
    pattern). Failures return [] without caching, so a later turn retries."""
    key = (agency, line.lower())
    if key not in _shapes_cache:
        try:
            body = await _get_json(
                "/bus-routes/", {"agency": agency, "line": line, "geometry": "true"}
            )
        except Exception as e:  # noqa: BLE001 - degrade to no-candidates
            logger.debug("tpl bus-routes %s %r failed: %s", agency, line, e)
            return []
        shapes: list[list[tuple[float, float]]] = []
        for it in body.get("BusRoutes") or [] if isinstance(body, dict) else []:
            pts = wkt_points(it.get("wktGeometry") or "") if isinstance(it, dict) else None
            if pts and len(pts) >= 2:
                shapes.append(pts)
        _shapes_cache[key] = shapes
    return _shapes_cache[key]


def _base_name(line: str) -> str:
    """Comparison key for a line shortName: km4city suffixes sub-patterns after a dot
    ("T1.3" for the router's "T1"), so equality is checked on the pre-dot base too."""
    return line.lower().split(".")[0]


def _name_match(short_name: str, line: str) -> bool:
    return short_name.lower() == line.lower() or _base_name(short_name) == _base_name(line)


# --- pure geometry (equirectangular km plane) ---------------------------------------------

def _flat(pt: tuple[float, float], cos_lat: float) -> tuple[float, float]:
    """(lng, lat) -> (x_km, y_km) on the local equirectangular plane."""
    return (pt[0] * _KM_PER_DEG * cos_lat, pt[1] * _KM_PER_DEG)


def _seg_project(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> tuple[float, float]:
    """(squared distance, clamped t) of point p onto segment a-b, all in flat km."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    qx, qy = a[0] + t * dx, a[1] + t * dy
    return (p[0] - qx) ** 2 + (p[1] - qy) ** 2, t


def _mean_stop_distance_km(
    stop_pts: list[tuple[float, float]], flat_shape: list[tuple[float, float]], cos_lat: float
) -> float:
    """Mean over the ride's stop vertices of their distance to the shape polyline."""
    total = 0.0
    for sp in stop_pts:
        f = _flat(sp, cos_lat)
        d2 = min(
            _seg_project(f, flat_shape[k], flat_shape[k + 1])[0]
            for k in range(len(flat_shape) - 1)
        )
        total += math.sqrt(d2)
    return total / len(stop_pts)


def _len_km(pts: list[tuple[float, float]]) -> float:
    return sum(haversine_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:]))


def _slice_between(
    shape: list[tuple[float, float]],
    flat_shape: list[tuple[float, float]],
    board: tuple[float, float],
    alight: tuple[float, float],
    cos_lat: float,
) -> tuple[list[tuple[float, float]], float] | None:
    """(shape cut between the ordered projections of board/alight, projection cost km).

    The cut runs from board's projection to alight's projection with alight's segment not
    before board's — the ordered pair with the least summed projection distance. On the
    reverse-direction variant of the same roadbed that constraint forces a far pairing,
    so its cost exposes the mismatch (the caller compares costs across variants). None
    only for a degenerate same-segment backwards cut.
    """
    fb, fa = _flat(board, cos_lat), _flat(alight, cos_lat)
    n = len(shape) - 1
    b_pro = [_seg_project(fb, flat_shape[k], flat_shape[k + 1]) for k in range(n)]
    a_pro = [_seg_project(fa, flat_shape[k], flat_shape[k + 1]) for k in range(n)]
    best: tuple[float, int, float, int, float] | None = None
    best_i = 0
    for j in range(n):
        if b_pro[j][0] < b_pro[best_i][0]:
            best_i = j
        cost = math.sqrt(b_pro[best_i][0]) + math.sqrt(a_pro[j][0])
        if best is None or cost < best[0]:
            best = (cost, best_i, b_pro[best_i][1], j, a_pro[j][1])
    if best is None:
        return None
    cost, i, ti, j, tj = best
    if i == j and tj < ti:  # both on one segment but backwards: no real cut
        return None

    def _lerp(k: int, t: float) -> tuple[float, float]:
        (x1, y1), (x2, y2) = shape[k], shape[k + 1]
        return (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)

    return [_lerp(i, ti)] + shape[i + 1 : j + 1] + [_lerp(j, tj)], cost


def _dedupe(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [pts[0]]
    for p in pts[1:]:
        if p != out[-1]:
            out.append(p)
    return out


# --- main entry ----------------------------------------------------------------------------

def _ride_instructions(instructions: list[Any], n_path_pts: int) -> list[Any]:
    """PT ride instructions in the exact order mcp_server._leg_slices cuts its bus legs
    (same leg/interval filter, same clamp, same (start, end) sort) so zip() pairs each
    bus leg with the instruction it was cut from."""
    rides: list[tuple[int, int, Any]] = []
    for ins in instructions:
        if not (isinstance(ins, dict) and isinstance(ins.get("leg"), dict)):
            continue
        iv = ins.get("interval")
        if not (isinstance(iv, list) and len(iv) == 2 and all(isinstance(i, int) for i in iv)):
            continue
        a, b = max(0, iv[0]), min(n_path_pts - 1, iv[1])
        if a < b:
            rides.append((a, b, ins))
    rides.sort(key=lambda r: (r[0], r[1]))
    return [ins for _, _, ins in rides]


def _ride_line(ride: Any) -> str | None:
    legmap = ride.get("leg", {}).get("map")
    if isinstance(legmap, dict) and isinstance(legmap.get("route_name"), str):
        return legmap["route_name"] or None
    return None


async def _enhance(legs: list[dict[str, Any]], instructions: list[Any], n_path_pts: int) -> bool:
    bus_legs = [leg for leg in legs if leg.get("type") == "bus"]
    rides = _ride_instructions(instructions, n_path_pts)
    named = [(leg, line) for leg, ride in zip(bus_legs, rides) if (line := _ride_line(ride))]
    if not named:
        return False  # nothing addressable — and no network touched
    index = await _load_lines_index()
    changed = False
    for leg, line in named:
        stop_pts = wkt_points(leg["wkt"])
        if not stop_pts or len(stop_pts) < 2:
            continue
        cands = [
            (agency, sn)
            for agency, names in index.items()
            for sn in names
            if _name_match(sn, line)
        ]
        if not cands:
            logger.debug("no tpl line matches route_name %r", line)
            continue
        shape_lists = await asyncio.gather(*(_line_shapes(a, sn) for a, sn in cands))
        cos_lat = math.cos(math.radians(stop_pts[0][1]))
        best: tuple[float, list[tuple[float, float]]] | None = None  # (cost, cut pts)
        n_shapes, best_score = 0, math.inf
        for shape in (s for lst in shape_lists for s in lst):
            n_shapes += 1
            flat_shape = [_flat(p, cos_lat) for p in shape]
            score = _mean_stop_distance_km(stop_pts, flat_shape, cos_lat)
            best_score = min(best_score, score)
            if score > MATCH_THRESHOLD_KM:
                continue
            cut = _slice_between(shape, flat_shape, stop_pts[0], stop_pts[-1], cos_lat)
            if cut is not None and (best is None or cut[1] < best[0]):
                best = (cut[1], cut[0])
        if best is None or best[0] > 2 * MATCH_THRESHOLD_KM:
            logger.debug(
                "line %r: no usable variant (%d shapes, best score %.0f m, best cut cost %s m)",
                line, n_shapes, best_score * 1000 if n_shapes else -1.0,
                f"{best[0] * 1000:.0f}" if best else "n/a",
            )
            continue
        # Sanity: the cut must look like the same ride — between ~the chord length (a
        # shape can only be longer than straight hops, minus corner-cut noise at the
        # projected ends) and a loop-sized multiple of it.
        chord_km = _len_km(stop_pts)
        cut_km = _len_km(best[1])
        if chord_km > 0 and not (0.8 * chord_km <= cut_km <= 3.0 * chord_km):
            logger.debug("shape cut for line %r rejected: %.2f km vs chord %.2f km", line, cut_km, chord_km)
            continue
        # Keep the original boundary vertices: adjacent foot legs share them, and the
        # dashboard hangs the board/alight stop pins there (frontend stays untouched).
        leg["wkt"] = fmt_linestring(_dedupe([stop_pts[0], *best[1], stop_pts[-1]]))
        changed = True
    return changed


async def enhance_bus_legs(
    legs: list[dict[str, Any]], instructions: list[Any], n_path_pts: int
) -> bool:
    """Swap each bus leg's stop-to-stop chords for the real GTFS shape cut, in place.

    True when at least one leg's wkt changed (the caller then re-derives the route-level
    wkt/distance from the legs). Never raises: on any failure — including the overall
    ENHANCE_BUDGET_S timeout — the remaining legs keep the router's chord geometry.
    """
    try:
        return await asyncio.wait_for(_enhance(legs, instructions, n_path_pts), ENHANCE_BUDGET_S)
    except Exception as e:  # noqa: BLE001 - enhancement is best-effort by contract
        logger.debug("bus shape enhancement skipped: %s", e)
        return False
