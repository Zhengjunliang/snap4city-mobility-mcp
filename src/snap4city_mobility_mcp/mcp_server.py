"""Local MCP server — our own tools, hosted alongside the client.

referente's remote `address_search_location` returns a broken result set (a query for
"chiesa ..., Firenze" comes back as 100 Spanish/Greek/Belgian hits and zero Tuscan;
see docs/lessons.md L28/L29). So forward geocoding is served here instead, by wrapping
the *public* km4city ServiceMap API (servicemap.disit.org — the same index the native
Snap4City What-If autocomplete uses, which returns clean hits for the queries the MCP
tool misses). The tool returns the *same* GeoJSON FeatureCollection shape the remote
tool did, so the client's existing 2-pass / pick-coord logic is reused unchanged
(mcp_tools._geocode_address_first et al.).

This server is generic on purpose (named `mcp_server`, not `geocode_server`): it hosts
the geocode tool (L29) and the all-modes What-If `route` tool (L19/L46).

Run it on the JupyterHub (it only needs outbound HTTP to the public ServiceMap):
    python -m snap4city_mobility_mcp.mcp_server
It serves Streamable HTTP at http://0.0.0.0:8020/mcp/ ; the client reaches it via
S4C_LOCAL_MCP_URL (orchestrator._local_config), defaulting to http://127.0.0.1:8020/mcp.
"""
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP

from snap4city_mobility_mcp.geo import haversine_km

logger = logging.getLogger(__name__)

# Public km4city ServiceMap "Smart City API". The text/POI full-text search is the base
# path; the address-focused geocoder is the /location/ sub-path. Both return GeoJSON.
# No selection/maxDists bias is sent: probed 2026-07-09, the parameter has zero effect on
# text-search ordering (byte-identical output with/without), so proximity ranking is done
# client-side (orchestrator._pick_coord haversine against the user's GPS).
# DELIBERATELY the single-region Tuscany index, not the federated SuperServiceMap
# (https://www.snap4city.org/superservicemap/api/v1) that referente's stack uses: the SSM
# does carry more regions (Antwerp/Helsinki/València/GardaLake), but its ranking is broken
# — probed 2026-07-09, "via zara firenze" ranks a Maastricht bus stop first (the L28
# failure mode this local tool exists to escape). Override via S4C_SERVICEMAP_BASE once
# referente fixes the SSM ranking and multi-region routing.
SERVICEMAP_BASE = os.environ.get(
    "S4C_SERVICEMAP_BASE", "https://servicemap.disit.org/WebAppGrafo/api/v1"
)
HTTP_TIMEOUT_S = 40.0

# Snap4City What-If GraphHopper router — ALL routing (foot/car/bus) goes through it via
# the local `route` tool, so every mode shares one request/response shape. It fully
# replaced the referente remote `routing` tool (retired 2026-07-13, L46): that tool's
# public_transport mode never returned transit (L19) and its km4city backend needed a
# stale-retry ladder (L3/L8). Same source the Gea-Night What-If dashboard draws from.
# S4C_WHATIF_ROUTER_URL overrides the base. The default is the ONLINE instance: referente
# loaded the Tuscany GTFS on it (2026-07-10) — set the env var to
# "http://localhost:8080/whatif-router/route" only to test a locally-built router (e.g.
# the perf patch in whatif-local/patches/). The online instance does NOT run the
# pt-router-singleton perf patch yet, so every vehicle=bus request reloads the PT graph
# (~30-40s measured); BUS_ROUTE_TIMEOUT_S covers that. foot/car never touch the PT graph
# (probed 2026-07-13: 0.3-0.5s) and use the generic HTTP_TIMEOUT_S. Tighten the bus
# timeout back once referente merges the patch.
WHATIF_ROUTER_URL = os.environ.get(
    "S4C_WHATIF_ROUTER_URL", "https://www.snap4city.org/whatif-router/route"
)
BUS_ROUTE_TIMEOUT_S = 120.0

mcp = FastMCP("snap4mobility-local")


def _rome_local_iso(ts: Any) -> Any:
    """Rewrite a router timestamp to Europe/Rome local time (ISO with offset).

    The router serializes stop/leg times as GraphHopper `Instant.toString()` — UTC with a
    trailing Z (e.g. "2026-07-06T06:23:00Z") — while the GTFS timetable is semantically
    Rome local time, so quoting the raw value to the user is 1-2h off. Anything that
    doesn't parse as an offset-aware ISO datetime passes through unchanged.
    """
    if not isinstance(ts, str) or not ts:
        return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if dt.tzinfo is None:  # naive: zone unknowable, don't guess a shift
        return ts
    return dt.astimezone(ZoneInfo("Europe/Rome")).isoformat()


def _pt_stops(legmap: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered [{name, time}] for one PT leg's stop list, times in Rome local.

    A GraphHopper GTFS transit leg nests its stops as
    leg.map.stop.myArrayList[].map with stop_name / stop_arrivalTime (observed shape,
    whatif-local/test-output.json: line 57 PORTE NUOVE BELFIORE -> ACC. DEL CIMENTO ARTOM).
    Times are converted from the router's UTC instants to Europe/Rome (_rome_local_iso)
    so the narrated orari match the local timetable. Anything else (no GTFS, malformed)
    yields an empty list.
    """
    stop = legmap.get("stop")
    items = stop.get("myArrayList") if isinstance(stop, dict) else None
    out: list[dict[str, Any]] = []
    if isinstance(items, list):
        for it in items:
            m = it.get("map") if isinstance(it, dict) else None
            if isinstance(m, dict) and m.get("stop_name"):
                out.append({"name": m["stop_name"], "time": _rome_local_iso(m.get("stop_arrivalTime"))})
    return out


def _wkt_points(wkt: str) -> list[tuple[float, float]] | None:
    """(lng, lat) vertex list of a 'LINESTRING (lng lat, lng lat, ...)', or None.

    Vertex indices match the router instructions' `interval` fields (live-verified: the
    last instruction's interval names the last vertex, and a ride interval's endpoints
    land on the board/alight stops) — which is what lets _leg_slices cut the geometry.
    """
    lo, hi = wkt.find("("), wkt.rfind(")")
    if lo < 0 or hi <= lo:
        return None
    pts: list[tuple[float, float]] = []
    for pair in wkt[lo + 1 : hi].split(","):
        xy = pair.split()
        if len(xy) >= 2:
            try:
                pts.append((float(xy[0]), float(xy[1])))
            except ValueError:
                continue
    return pts or None


def _wkt_length_km(wkt: str) -> float | None:
    """Total geodesic length (km) of a 'LINESTRING (lng lat, lng lat, ...)'.

    The router's paths[0].distance counts only the walking-access metres — a GTFS transit
    leg's in-vehicle ride contributes 0 to it (whatif-local/test-output.json: distance 2017 m
    is exactly the walk to/from the stops), so the real door-to-door distance is recovered by
    measuring the full drawn geometry instead. None when the WKT can't be parsed.
    """
    pts = _wkt_points(wkt)
    if pts is None or len(pts) < 2:
        return None
    total = sum(
        haversine_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:])
    )
    return round(total, 3)


def _fmt_linestring(pts: list[tuple[float, float]]) -> str:
    """'LINESTRING (lng lat, ...)' from a (lng, lat) vertex list (router WKT shape)."""
    return "LINESTRING (" + ", ".join(f"{lng} {lat}" for lng, lat in pts) + ")"


def _leg_slices(
    instructions: list[Any], pts: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Per-leg geometry cuts of the journey line: [{"type": "foot"|"bus", "wkt": ...}].

    A PT ride instruction's `interval` is a [start, end] vertex-index pair into the path
    geometry, so the line splits into walk / ride / walk slices — the same cut the What-If
    widget makes client-side after re-fetching the route (dashboard-builder widgetMap.php).
    Shipping the slices lets the dashboard draw the walk/ride split (and the board/alight
    stop pins, the slice boundary vertices) straight from this response, with no second
    router call. Adjacent slices share their boundary vertex so the drawn segments connect.
    Rides with a missing/malformed interval are skipped; no rides yields [] (walking-only).
    """
    rides: list[tuple[int, int]] = []
    for ins in instructions:
        if not (isinstance(ins, dict) and isinstance(ins.get("leg"), dict)):
            continue
        iv = ins.get("interval")
        if not (isinstance(iv, list) and len(iv) == 2 and all(isinstance(i, int) for i in iv)):
            continue
        a, b = max(0, iv[0]), min(len(pts) - 1, iv[1])
        if a < b:
            rides.append((a, b))
    rides.sort()
    legs: list[dict[str, Any]] = []
    start = 0
    for a, b in rides:
        if a > start:
            legs.append({"type": "foot", "wkt": _fmt_linestring(pts[start : a + 1])})
        legs.append({"type": "bus", "wkt": _fmt_linestring(pts[a : b + 1])})
        start = b
    if legs and start < len(pts) - 1:
        legs.append({"type": "foot", "wkt": _fmt_linestring(pts[start:])})
    return legs


def _journey_duration_ms(first: dict[str, Any]) -> int:
    """Trip duration in ms = walking time + in-vehicle time.

    Walking instructions carry a reliable per-step `time` (~720 ms/m ≈ 5 km/h); each PT ride
    carries its true GTFS-derived duration in leg.map.travelTime (688000 ms = 11m28s, matching
    the 06:23:00 -> 06:34:28 stop schedule). The router's paths[0].time is deliberately NOT
    used: its "walking-pace" value is calibrated on the no-GTFS foot degrade and is
    semantically mixed. This total excludes platform wait, so it is a ride-time estimate, not a
    precise arrival ETA.
    """
    total = 0
    for ins in first.get("instructions") or []:
        if not isinstance(ins, dict):
            continue
        leg = ins.get("leg")
        legmap = leg.get("map") if isinstance(leg, dict) else None
        if isinstance(legmap, dict) and legmap.get("type") == "pt":
            tt = legmap.get("travelTime")
            if isinstance(tt, (int, float)):
                total += int(tt)
        else:
            t = ins.get("time")
            if isinstance(t, (int, float)):
                total += int(t)
    return total


def _fmt_hms(ms: int) -> str:
    """Milliseconds -> 'H:MM:SS' (the duration shape _route_minutes / _template_answer expect)."""
    secs = max(0, ms // 1000)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _bus_arcs(first: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered multimodal journey arcs from a What-If GraphHopper bus path's turn-by-turn.

    Produces a full walk -> ride -> walk sequence. A run of on-street instructions (no `leg`)
    collapses into one walking arc (transport "foot", distance in km so group_arc_legs sums it
    like any leg). A public-transport ride is an instruction whose `leg.map.type == "pt"` (text
    "Pt_start_trip"), carrying line (route_name), operator (agency_name), headsign and the
    ordered stop list; it becomes a boarding + alighting bus arc — the boarding arc also carries
    the raw `line`, `headsign` and full `stops` (name + scheduled time) so group_arc_legs can
    name the leg by its true line/endpoints and list its stops. The walk arcs before/between/
    after rides make the trip read like a door-to-door journey.

    A walking-only response (no pt leg: either the router has no GTFS, or — with GTFS loaded —
    walking Pareto-dominates any bus for a short trip, L39) yields honest foot arcs. The client
    (_pt_is_foot_only) detects that and presents the journey as "no convenient direct bus,
    here is the walk" instead of a fake bus route.

    Consecutive rides by the same operator merge into one leg in group_arc_legs (its grouping
    key is (transport, provider)); transfers between two lines of the same operator are not yet
    split out. Single-line urban trips (the common Florence case) are unaffected.
    """
    arcs: list[dict[str, Any]] = []
    walk_m = 0.0

    def flush_walk() -> None:
        nonlocal walk_m
        if walk_m > 0:
            arcs.append({
                "transport": "foot",
                "transport_provider": None,
                "desc": f"a piedi {int(round(walk_m))} m",
                "distance": round(walk_m / 1000, 3),  # km, matches route distance_km unit
            })
        walk_m = 0.0

    for ins in first.get("instructions") or []:
        if not isinstance(ins, dict):
            continue
        leg = ins.get("leg")
        legmap = leg.get("map") if isinstance(leg, dict) else None
        if isinstance(legmap, dict) and legmap.get("type") == "pt":
            flush_walk()
            line = legmap.get("route_name")
            operator = legmap.get("agency_name") or "public"
            headsign = legmap.get("trip_headsign")
            stops = _pt_stops(legmap)
            board = stops[0] if stops else None
            alight = stops[-1] if stops else None
            board_txt = " ".join(
                t for t in (f"linea {line}" if line else None, f"da {board['name']}" if board else None) if t
            ) or "nd"
            alight_txt = " ".join(
                t for t in (f"a {alight['name']}" if alight else None, f"(-> {headsign})" if headsign else None) if t
            ) or "nd"
            board_arc: dict[str, Any] = {
                "transport": "bus",
                "transport_provider": operator,
                "desc": board_txt,
                "line": line,
                "headsign": headsign,
                "stops": stops,
            }
            if board and board.get("time"):
                board_arc["start_datetime"] = board["time"]
            alight_arc: dict[str, Any] = {"transport": "bus", "transport_provider": operator, "desc": alight_txt}
            if alight and alight.get("time"):
                alight_arc["end_datetime"] = alight["time"]
            arcs.extend((board_arc, alight_arc))
        else:
            d = ins.get("distance")
            if isinstance(d, (int, float)):
                walk_m += d
    flush_walk()
    return arcs


def _street_arcs(first: dict[str, Any], vehicle: str) -> list[dict[str, Any]]:
    """Ordered arcs for a single-mode (foot/car) path, one per turn-by-turn instruction.

    `desc` carries the street name so slim_result_for_llm's `streets` view — the "main
    streets" respond mentions — keeps working after the km4city routing tool (whose arcs
    carried the street in `desc`) was retired. Unnamed stretches yield "nd", which that
    view already drops. `distance` is km, like every journey arc.
    """
    arcs: list[dict[str, Any]] = []
    for ins in first.get("instructions") or []:
        if not isinstance(ins, dict):
            continue
        d = ins.get("distance")
        arcs.append({
            "transport": vehicle,
            "transport_provider": None,
            "desc": ins.get("street_name") or "nd",
            "distance": round(d / 1000, 3) if isinstance(d, (int, float)) else None,
        })
    return arcs


def _normalize_feature(feature: dict[str, Any]) -> dict[str, Any]:
    """ServiceMap feature -> the FeatureCollection feature shape the client expects.

    The address endpoint carries properties.{address, city, score}; the full-text
    endpoint carries properties.name with city/score often absent. We map name -> address
    so _pick_coord / _narrow_by_city / slim_result_for_llm read the same fields either way,
    and keep the original name. Geometry (GeoJSON [lng, lat]) passes through untouched.
    """
    props = feature.get("properties") or {}
    return {
        "geometry": feature.get("geometry") or {},
        "properties": {
            "address": props.get("address") or props.get("name"),
            "city": props.get("city"),
            "score": props.get("score"),
            "name": props.get("name"),
        },
    }


async def _servicemap_search(
    search: str, *, excludePOI: bool, lang: str, maxresults: int
) -> dict[str, Any]:
    """Query the public ServiceMap and return a normalized GeoJSON FeatureCollection.

    excludePOI=True hits the address geocoder (/location/), False hits the full-text
    search (POIs / landmarks). Any failure (network, non-200, unexpected body) comes back
    as {"error": ...} so the client surfaces it like any other geocode miss.
    """
    url = f"{SERVICEMAP_BASE}/location/" if excludePOI else SERVICEMAP_BASE
    params = {
        "search": search,
        "format": "json",
        "lang": lang,
        "maxResults": str(maxresults),
    }
    try:
        # follow_redirects: the full-text base (no trailing slash) answers a 302 to the
        # slash form; without following it httpx raises on the redirect.
        async with httpx.AsyncClient(follow_redirects=True) as h:
            resp = await h.get(url, params=params, timeout=HTTP_TIMEOUT_S)
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:  # noqa: BLE001 - surface any failure as a geocode error
        logger.debug("servicemap %r (excludePOI=%s) failed: %s", search, excludePOI, e)
        return {"error": f"servicemap search failed: {type(e).__name__}: {e}"}
    features = body.get("features") if isinstance(body, dict) else None
    if not isinstance(features, list):
        return {"error": f"servicemap returned no feature list for {search!r}"}
    kept = [_normalize_feature(f) for f in features if isinstance(f, dict)]
    return {"type": "FeatureCollection", "features": kept, "count": len(kept)}


@mcp.tool
async def address_search_location(
    search: str,
    excludePOI: bool = True,
    lang: str = "it",
    logic: str = "or",
    maxresults: int = 100,
) -> dict[str, Any]:
    """Forward-geocode a place name to a GeoJSON FeatureCollection.

    Drop-in for the remote tool of the same name: the client calls it with
    {search, excludePOI, lang, logic}. `logic` is accepted for signature compatibility but
    unused (the ServiceMap full-text search has no AND/OR logic parameter). Results come
    back in the server's text-relevance order; proximity/city preference is applied
    client-side (orchestrator._pick_coord / mcp_tools._narrow_by_city).
    """
    return await _servicemap_search(search, excludePOI=excludePOI, lang=lang, maxresults=maxresults)


@mcp.tool
async def route(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    vehicle: str = "foot",
    startdatetime: str | None = None,
) -> dict[str, Any]:
    """Point-to-point route (foot / car / bus) via the What-If GraphHopper router.

    ONE tool for every mode, so all three share a request/response shape: a routing-shaped
    {"journey": {"routes": [{wkt, distance, time, arc, ...}]}} the client renders and
    narrates uniformly. Replaces both the referente remote `routing` tool (retired
    2026-07-13: public_transport never returned transit L19, km4city quirks needed a whole
    retry ladder L3/L8) and the old bus-only `bus_route`. Returns {"error": ...} on any
    failure.

    vehicle="bus" rides the GTFS: `distance` is the door-to-door geometry length (the
    router's paths[0].distance counts only the walking-access metres), `time` sums the
    instructions' walk + in-vehicle travelTime (excludes platform wait; paths[0].time is
    walking-pace / semantically mixed there), `arc` is the walk -> ride -> walk sequence
    with line/operator/stops (_bus_arcs) — a walking-only itinerary (short trip, L39)
    yields honest foot arcs the client presents as a walk — and `legs` carries the
    walk/ride geometry slices the dashboard draws (L44). startdatetime (GTFS timetable
    window) only matters here.

    vehicle="foot"/"car" never touch the PT graph (sub-second): `distance` and `time` come
    straight from the path's true totals, `arc` is one street arc per instruction
    (_street_arcs), and there is no `legs` (single-mode line, the whole wkt draws in one
    color).
    """
    if vehicle not in ("foot", "car", "bus"):
        return {"error": f"unsupported vehicle {vehicle!r} (foot, car, bus)"}
    params = {
        "vehicle": vehicle,
        "waypoints": f"{start_longitude},{start_latitude};{end_longitude},{end_latitude}",
        "weighting": "fastest",
        "wkt": "true",
    }
    if startdatetime:
        params["startDatetime"] = startdatetime
    timeout = BUS_ROUTE_TIMEOUT_S if vehicle == "bus" else HTTP_TIMEOUT_S
    try:
        async with httpx.AsyncClient(follow_redirects=True) as h:
            resp = await h.get(WHATIF_ROUTER_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:  # noqa: BLE001 - surface any failure as a routing error
        logger.debug("whatif-router %s route failed: %s", vehicle, e)
        return {"error": f"whatif-router {vehicle} route failed: {type(e).__name__}: {e}"}
    paths = body.get("paths") if isinstance(body, dict) else None
    if not (isinstance(paths, list) and paths and isinstance(paths[0], dict)):
        return {"error": f"whatif-router returned no {vehicle} path"}
    first = paths[0]
    wkt = first.get("wkt")
    if not isinstance(wkt, str) or not wkt:
        return {"error": f"whatif-router {vehicle} path has no wkt"}
    if vehicle == "bus":
        # Door-to-door length from the geometry; paths[0].distance is walking-access only
        # (see _wkt_length_km). Fall back to the router metres only if the WKT can't be
        # measured.
        distance_km = _wkt_length_km(wkt)
        if distance_km is None:
            dist = first.get("distance")
            distance_km = round(dist / 1000, 3) if isinstance(dist, (int, float)) else None
        found: dict[str, Any] = {"wkt": wkt, "distance": distance_km, "arc": _bus_arcs(first)}
        pts = _wkt_points(wkt)
        legs = _leg_slices(first.get("instructions") or [], pts) if pts else []
        if legs:
            found["legs"] = legs
        duration_ms = _journey_duration_ms(first)
        if duration_ms > 0:
            found["time"] = _fmt_hms(duration_ms)
    else:
        # Single-mode paths carry true totals (the PT walking-access caveat doesn't apply).
        dist = first.get("distance")
        found = {
            "wkt": wkt,
            "distance": round(dist / 1000, 3) if isinstance(dist, (int, float)) else _wkt_length_km(wkt),
            "arc": _street_arcs(first, vehicle),
        }
        t = first.get("time")
        if isinstance(t, (int, float)) and t > 0:
            found["time"] = _fmt_hms(int(t))
    return {"journey": {"routes": [found]}}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8020)
