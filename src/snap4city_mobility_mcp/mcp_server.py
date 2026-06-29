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
WHATIF_ROUTER_URL = "https://www.snap4city.org/whatif-router/route"

mcp = FastMCP("snap4mobility-local")


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
    it like any route. The single route carries one synthetic `bus` arc so the client does
    not treat it as a foot-only degrade (L31). Returns {"error": ...} on any failure.

    The GraphHopper bus `time` is unreliable (it clocks a ~4 km route at ~50 min, walking
    pace), so only the distance and geometry are surfaced — never a fabricated ETA.
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
    dist = first.get("distance")  # metres
    distance_km = round(dist / 1000, 3) if isinstance(dist, (int, float)) else None
    return {
        "journey": {
            "routes": [
                {
                    "wkt": wkt,
                    "distance": distance_km,
                    # One synthetic bus leg marks this as real public transport so the client
                    # keeps it (not a foot-only degrade) and draws/narrates a bus route. No
                    # `time`/`eta`: the GraphHopper bus duration is unreliable (see docstring).
                    "arc": [{"transport": "bus", "transport_provider": "public", "desc": "nd"}],
                }
            ]
        }
    }


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8020)
