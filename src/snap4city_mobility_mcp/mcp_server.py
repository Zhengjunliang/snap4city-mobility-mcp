"""Local MCP server — our own tools, hosted alongside the client.

referente's remote `address_search_location` returns a broken result set (a query for
"chiesa ..., Firenze" comes back as 100 Spanish/Greek/Belgian hits and zero Tuscan;
see docs/lessons.md L28/L29). So forward geocoding is served here instead, by wrapping
the *public* km4city ServiceMap API (servicemap.disit.org — the same index the native
Snap4City What-If autocomplete uses, which returns clean Florence hits for the queries
the MCP tool misses). The tool returns the *same* GeoJSON FeatureCollection shape the
remote tool did, so the client's existing 2-pass / bbox-filter / pick-coord logic is
reused unchanged (mcp_tools._geocode_address_first et al.).

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
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Public km4city ServiceMap "Smart City API". The text/POI full-text search is the base
# path; the address-focused geocoder is the /location/ sub-path. Both return GeoJSON and
# accept selection=<lat>;<lng> + maxDists=<km> to bias results toward an area (a soft
# bias, not a hard filter: far Tuscan towns can still come back, so the client keeps its
# own Tuscany bbox filter).
SERVICEMAP_BASE = "https://servicemap.disit.org/WebAppGrafo/api/v1"
# Florence centre, used as the relevance bias for every search (this advisor is
# Florence-centric; the client bbox covers all of Tuscany).
FLORENCE_SELECTION = "43.7731;11.2558"
SEARCH_MAX_DISTS_KM = "30"
HTTP_TIMEOUT_S = 40.0

# Snap4City What-If GraphHopper router. The referente remote `routing` tool's
# public_transport mode never returns transit (it degrades to a walking journey or -2 for
# any date/OD, see docs/lessons.md L19); the Gea-Night What-If dashboard draws its bus line
# by calling this endpoint with vehicle=bus, which returns a bus-road route as WKT. We wrap
# it here (same local-server pattern as the geocode tool, L29) so forward bus routing is a
# local MCP tool the client drives, never a raw HTTP call from the orchestrator.
# S4C_WHATIF_ROUTER_URL overrides the base so bus_route can be pointed at a locally-run
# whatif-router container (loaded with Tuscany GTFS, see whatif-local/) for end-to-end
# testing before referente loads the data on the online instance; unset = online default.
WHATIF_ROUTER_URL = os.environ.get(
    "S4C_WHATIF_ROUTER_URL", "https://www.snap4city.org/whatif-router/route"
)

mcp = FastMCP("snap4mobility-local")


def _pt_stops(legmap: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered [{name, time}] for one PT leg's stop list.

    A GraphHopper GTFS transit leg nests its stops as
    leg.map.stop.myArrayList[].map with stop_name / stop_arrivalTime (observed shape,
    whatif-local/test-output.json: line 57 PORTE NUOVE BELFIORE -> ACC. DEL CIMENTO ARTOM).
    Anything else (no GTFS, malformed) yields an empty list.
    """
    stop = legmap.get("stop")
    items = stop.get("myArrayList") if isinstance(stop, dict) else None
    out: list[dict[str, Any]] = []
    if isinstance(items, list):
        for it in items:
            m = it.get("map") if isinstance(it, dict) else None
            if isinstance(m, dict) and m.get("stop_name"):
                out.append({"name": m["stop_name"], "time": m.get("stop_arrivalTime")})
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

    When the router has no Tuscany GTFS it returns walking-only instructions (the online
    degrade, L31): no pt leg is ever seen, so we fall back to one synthetic bus arc per main
    street, keeping the route shown as a bus route rather than dropped as a foot-only journey.
    The fallback is gated on "no pt leg seen" (`saw_pt`), NOT "no arc produced" — walk arcs now
    populate the list even in the degrade, so an `if arc:` gate would wrongly return a foot-only
    journey and _pt_is_foot_only would drop the whole route.

    Consecutive rides by the same operator merge into one leg in group_arc_legs (its grouping
    key is (transport, provider)); transfers between two lines of the same operator are not yet
    split out. Single-line urban trips (the common Florence case) are unaffected.
    """
    arcs: list[dict[str, Any]] = []
    saw_pt = False
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
            saw_pt = True
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
    if saw_pt:
        return arcs
    # No GTFS PT ride in the response (online instance without Tuscany data): keep the legacy
    # behaviour — one synthetic bus arc per main street so the road line is still shown as a
    # bus route rather than dropped as a foot-only degrade (L31). Capped to keep it concise.
    streets: list[str] = []
    for ins in first.get("instructions") or []:
        name = ins.get("street_name") if isinstance(ins, dict) else None
        if isinstance(name, str) and name.strip() and name not in streets:
            streets.append(name.strip())
    return [
        {"transport": "bus", "transport_provider": "public", "desc": s} for s in streets[:10]
    ] or [{"transport": "bus", "transport_provider": "public", "desc": "nd"}]


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
        "selection": FLORENCE_SELECTION,
        "maxDists": SEARCH_MAX_DISTS_KM,
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
    """Forward-geocode a place name to a GeoJSON FeatureCollection (Florence-biased).

    Drop-in for the remote tool of the same name: the client calls it with
    {search, excludePOI, lang, logic}. `logic` is accepted for signature compatibility but
    unused (the ServiceMap full-text search has no AND/OR logic parameter). Results are
    biased to Florence via selection/maxDists; the client applies the Tuscany bbox filter.
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
    it like any route. With Tuscany GTFS loaded the route carries an ordered walk -> ride ->
    walk arc list (real line/operator/stops, see _bus_arcs); with no GTFS it degrades to one
    synthetic `bus` arc per street so it is still kept, not dropped as foot-only (L31).
    Returns {"error": ...} on any failure.

    `distance` is the door-to-door geometry length (from the WKT, since paths[0].distance
    counts only walking-access metres). `time` (a "H:MM:SS" ride-time estimate = walking +
    in-vehicle, excluding platform wait) is surfaced ONLY when a real GTFS ride is present;
    the raw GraphHopper paths[0].time is never used (it is walking-pace / semantically mixed).
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
    # Journey arcs: real transit legs (line/operator/stops) when the router has Tuscany GTFS,
    # else a synthetic bus arc per street when it degrades to a road line (see _bus_arcs).
    arc = _bus_arcs(first)
    route: dict[str, Any] = {"wkt": wkt, "distance": distance_km, "arc": arc}
    # A real GTFS ride (a bus arc carrying a `line`) yields a trustworthy walk+ride duration;
    # on the no-GTFS degrade (synthetic street arcs, no line) we have no timetable, so no time.
    if any(a.get("transport") == "bus" and a.get("line") for a in arc):
        route["time"] = _fmt_hms(_journey_duration_ms(first))
    return {"journey": {"routes": [route]}}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8020)
