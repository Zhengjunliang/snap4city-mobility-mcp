"""Local MCP server — our own tools, hosted alongside the client.

referente's remote `address_search_location` returns a broken result set (a query for
"chiesa ..., Firenze" comes back as 100 Spanish/Greek/Belgian hits and zero Tuscan;
see docs/lessons.md L28/L29). So forward geocoding is served here instead, by wrapping
the *public* km4city ServiceMap API (servicemap.disit.org — the same index the native
Snap4City What-If autocomplete uses, which returns clean hits for the queries the MCP
tool misses). The tool returns the *same* GeoJSON FeatureCollection shape the remote
tool did, so the client's existing 2-pass / pick-coord logic is reused unchanged
(mcp_tools._geocode_address_first et al.).

This server is generic on purpose (named `mcp_server`, not `geocode_server`): only the
geocode tool lives here today, but more local tools can be added later.

Run it on the JupyterHub (it only needs outbound HTTP to the public ServiceMap):
    python -m snap4city_mobility_mcp.mcp_server
It serves Streamable HTTP at http://0.0.0.0:8020/mcp/ ; the client reaches it via
S4C_LOCAL_MCP_URL (orchestrator._local_config), defaulting to http://127.0.0.1:8020/mcp.
"""
import logging
import math
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Public km4city ServiceMap "Smart City API". The text/POI full-text search is the base
# path; the address-focused geocoder is the /location/ sub-path. Both return GeoJSON.
# No selection/maxDists bias is sent: probed 2026-07-09, the parameter has zero effect on
# text-search ordering (byte-identical output with/without), so proximity ranking is done
# client-side (orchestrator._pick_coord haversine against the user's GPS).
SERVICEMAP_BASE = "https://servicemap.disit.org/WebAppGrafo/api/v1"
HTTP_TIMEOUT_S = 40.0

# Snap4City What-If GraphHopper router. The referente remote `routing` tool's
# public_transport mode never returns transit (it degrades to a walking journey or -2 for
# any date/OD, see docs/lessons.md L19); the Gea-Night What-If dashboard draws its bus line
# by calling this endpoint with vehicle=bus, which returns a bus-road route as WKT. We wrap
# it here (same local-server pattern as the geocode tool, L29) so forward bus routing is a
# local MCP tool the client drives, never a raw HTTP call from the orchestrator.
# S4C_WHATIF_ROUTER_URL overrides the base. The default points at the LOCALLY-run whatif-router
# on the JupyterHub (loaded with Tuscany GTFS, see whatif-local/) because the online instance has
# no GTFS yet and returns a walking degrade (L31/L34). REVERT this default to
# "https://www.snap4city.org/whatif-router/route" once referente loads the GTFS + perf patch on the
# online instance (or just set S4C_WHATIF_ROUTER_URL to it) — then the local Tomcat is unneeded.
WHATIF_ROUTER_URL = os.environ.get(
    "S4C_WHATIF_ROUTER_URL", "http://localhost:8080/whatif-router/route"
)

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


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng points (mirrors orchestrator._haversine_km)."""
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _wkt_length_km(wkt: str) -> float | None:
    """Total geodesic length (km) of a 'LINESTRING (lng lat, lng lat, ...)'.

    The router's paths[0].distance counts only the walking-access metres — a GTFS transit
    leg's in-vehicle ride contributes 0 to it (whatif-local/test-output.json: distance 2017 m
    is exactly the walk to/from the stops), so the real door-to-door distance is recovered by
    measuring the full drawn geometry instead. None when the WKT can't be parsed.
    """
    lo, hi = wkt.find("("), wkt.rfind(")")
    if lo < 0 or hi <= lo:
        return None
    pts: list[tuple[float, float]] = []
    for pair in wkt[lo + 1 : hi].split(","):
        xy = pair.split()
        if len(xy) >= 2:
            try:
                pts.append((float(xy[1]), float(xy[0])))  # (lat, lng) from "lng lat"
            except ValueError:
                continue
    if len(pts) < 2:
        return None
    total = sum(
        _haversine_km(a_lat, a_lng, b_lat, b_lng)
        for (a_lat, a_lng), (b_lat, b_lng) in zip(pts, pts[1:])
    )
    return round(total, 3)


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
async def bus_route(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    startdatetime: str | None = None,
) -> dict[str, Any]:
    """Public-transport (bus) route between two GPS points, via the What-If GraphHopper router.

    The referente remote `routing` tool's public_transport mode never returns transit
    (L19); this wraps the What-If `vehicle=bus` endpoint the Gea-Night dashboard uses and
    returns a routing-shaped {"journey": {"routes": [...]}} so the client renders/narrates
    it like any route. The route carries an ordered walk -> ride -> walk arc list (real
    line/operator/stops, see _bus_arcs); a walking-only itinerary (no GTFS, or walking beats
    any bus on a short trip, L39) yields honest foot arcs the client presents as a walk.
    Returns {"error": ...} on any failure.

    `distance` is the door-to-door geometry length (from the WKT, since paths[0].distance
    counts only walking-access metres). `time` is a "H:MM:SS" estimate summed from the
    instructions (walking + in-vehicle `travelTime`, excluding platform wait) — real for a
    GTFS ride and equally real for a pure walk; the raw GraphHopper paths[0].time is never
    used (it is walking-pace / semantically mixed).
    """
    params = {
        "vehicle": "bus",
        "waypoints": f"{start_longitude},{start_latitude};{end_longitude},{end_latitude}",
        "weighting": "fastest",
        "wkt": "true",
    }
    if startdatetime:
        params["startDatetime"] = startdatetime
    try:
        async with httpx.AsyncClient(follow_redirects=True) as h:
            resp = await h.get(WHATIF_ROUTER_URL, params=params, timeout=HTTP_TIMEOUT_S)
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:  # noqa: BLE001 - surface any failure as a routing error
        logger.debug("whatif-router bus route failed: %s", e)
        return {"error": f"whatif-router bus route failed: {type(e).__name__}: {e}"}
    paths = body.get("paths") if isinstance(body, dict) else None
    if not (isinstance(paths, list) and paths and isinstance(paths[0], dict)):
        return {"error": "whatif-router returned no bus path"}
    first = paths[0]
    wkt = first.get("wkt")
    if not isinstance(wkt, str) or not wkt:
        return {"error": "whatif-router bus path has no wkt"}
    # Door-to-door length from the geometry; paths[0].distance is walking-access only (see
    # _wkt_length_km). Fall back to the router metres only if the WKT can't be measured.
    distance_km = _wkt_length_km(wkt)
    if distance_km is None:
        dist = first.get("distance")
        distance_km = round(dist / 1000, 3) if isinstance(dist, (int, float)) else None
    # Journey arcs: real transit legs (line/operator/stops) when the itinerary rides a bus,
    # plain foot arcs when it is walking-only (see _bus_arcs).
    arc = _bus_arcs(first)
    route: dict[str, Any] = {"wkt": wkt, "distance": distance_km, "arc": arc}
    duration_ms = _journey_duration_ms(first)
    if duration_ms > 0:
        route["time"] = _fmt_hms(duration_ms)
    return {"journey": {"routes": [route]}}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8020)
