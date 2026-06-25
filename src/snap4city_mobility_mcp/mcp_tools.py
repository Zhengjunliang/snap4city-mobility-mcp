"""Client-side MCP layer.

This module implements no tools: they live on referente's remote
snap4agentic_advisor_native server. Here we only connect to it and run the graph's
tool calls via client.call_tool, unwrapping the response and smoothing km4city's
known quirks. The flows (route + tpl_*) drive their tool chains in Python; no LLM
ever picks a tool.

The dashboard's intranet IP is reachable directly from the JupyterHub, so
DASHBOARD_URL defaults to it; override with S4C_DASHBOARD_URL if the dashboard is
elsewhere. /apps.json carries the multi-server config: we keep the native server and
rewrite the internal IP to DASHBOARD_URL.

exec_tool is the single execution seam and never raises: every failure comes back as
{"error": ...} so the graph can recover. routing goes through routing_with_retry
(km4city envelope quirks), address_search_location through geocode_with_retry (2-pass
address/POI, Tuscany bbox pin, retry for the flaky zero-region window); other tools
pass straight through _unwrap.
"""
import asyncio
import json
import logging
import os
import re
import unicodedata
from typing import Any

import httpx
from fastmcp import Client

logger = logging.getLogger(__name__)

# The routing wrapper occasionally returns an empty body on cold start; retry after a
# delay to mask the transient. Three attempts total also tell the transient apart from
# the stable wrapper bug: still empty on the third attempt means the server-side bug.
ROUTING_STALE_RETRIES = 2
ROUTING_STALE_RETRY_DELAY_S = 6.0

# km4city's geocoder is no longer region-locked: its index now also covers Valencia and
# southern France, so a fuzzy Florence query can rank Spanish streets first ("Piazza del
# Duomo, Firenze" once returned 100 foreign hits and zero Tuscan). We pin results to a
# Tuscany bbox client-side and geocode in two passes, addresses first then POIs
# (_geocode_address_first). Bounds cover the whole region, not just Florence.
TUSCANY_BBOX = {"min_lng": 9.6, "max_lng": 12.5, "min_lat": 42.2, "max_lat": 44.5}

# The intranet dashboard IP is reachable directly from the JupyterHub, so it's the
# default. Override via S4C_DASHBOARD_URL if the dashboard is exposed elsewhere.
DASHBOARD_URL = os.environ.get("S4C_DASHBOARD_URL", "http://192.168.1.117:8000")
INTERNAL_DASHBOARD_URL = "http://192.168.1.117:8000"
NATIVE_SERVER_ID = "snap4agentic_advisor_native"

# The core mobility tools the flows may call. TOOL_NAMES (below) is the allowlist
# exec_tool checks before forwarding a call: an unlisted name returns {"error": ...}
# instead of hitting the network.
EXPOSED_TOOLS = (
    "address_search_location",
    "coordinates_to_address",  # reverse geocode (GPS point -> address); foundation for near-me
    "routing",
    "tpl_agencies",
    "tpl_lines",
    "tpl_routes_by_line",
    "tpl_stops_by_route",
    "tpl_stop_timeline",
)
TOOL_NAMES = frozenset(EXPOSED_TOOLS)


async def _build_config() -> dict[str, Any]:
    """Fetch dashboard /apps.json, keep only native server, rewrite URL to DASHBOARD_URL."""
    async with httpx.AsyncClient() as h:
        cfg = (await h.get(f"{DASHBOARD_URL}/apps.json", timeout=10)).json()
    native = cfg["mcpServers"][NATIVE_SERVER_ID]
    return {
        "mcpServers": {
            NATIVE_SERVER_ID: {
                **native,
                "url": native["url"].replace(INTERNAL_DASHBOARD_URL, DASHBOARD_URL),
            }
        }
    }


# Our own local MCP server (mcp_server.py) hosts forward geocoding (referente's remote
# address_search_location is server-side broken, L28/L29). It is a separate single-server
# client so the remote client stays single-server with bare tool names (no FastMCP server
# prefix, L6); the single "local" key here likewise yields bare names.
LOCAL_MCP_URL = os.environ.get("S4C_LOCAL_MCP_URL", "http://127.0.0.1:8020/mcp")


def _local_config() -> dict[str, Any]:
    """FastMCP single-server config for the local MCP server (mcp_server.py)."""
    return {"mcpServers": {"local": {"url": LOCAL_MCP_URL}}}


def _unwrap(result: Any) -> Any:
    """fastmcp.Client.call_tool result → structured payload (dict / list / scalar)."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    content = getattr(result, "content", None) or []
    if content:
        return json.loads(content[0].text)
    return None


async def _call_routing_once(client: Client, args: dict[str, Any]) -> dict[str, Any]:
    """Single routing tool call -> {data} | {error}. Stale retry handled by the caller."""
    try:
        result = await client.call_tool("routing", args)
    except Exception as e:
        return {"error": f"routing call failed: {type(e).__name__}: {e}"}
    data = _unwrap(result)
    if not isinstance(data, dict):
        return {"error": f"routing returned non-dict payload: {type(data).__name__}"}
    return {"data": data}


def _looks_stale(data: dict[str, Any]) -> bool:
    """Does this payload look like the cold-start stale shape?

    Stale = no `journey` dict (an empty wrap or an unrecognized envelope).
    """
    return not isinstance(data.get("journey"), dict)


async def routing_with_retry(
    client: Client, args: dict[str, Any], *, attempts: int | None = None
) -> dict[str, Any]:
    """km4city routing with stale retry and envelope checks.

    args = {startlatitude, startlongitude, endlatitude, endlongitude, routetype, [startdatetime]}.
    Returns {"journey": {...}} on success or {"error": "<msg>"} on any failure shape.
    `attempts` overrides the stale-retry ladder: the foot-profile fallback probe passes
    1, since the requested profile's full ladder already ruled the transient out (each
    failing attempt costs ~5 s call + 6 s delay).
    """
    # First attempt plus bounded retries for the cold-start stale window.
    if attempts is None:
        attempts = ROUTING_STALE_RETRIES + 1
    res = await _call_routing_once(client, args)
    for attempt in range(1, attempts):
        if "error" in res:
            break
        if not _looks_stale(res["data"]):
            break
        logger.debug(
            "routing stale payload (attempt %d/%d, routetype=%s): %s",
            attempt, attempts, args.get("routetype"), json.dumps(res["data"])[:500],
        )
        await asyncio.sleep(ROUTING_STALE_RETRY_DELAY_S)
        res = await _call_routing_once(client, args)

    if "error" in res:
        return {"error": res["error"]}
    data = res["data"]

    # Failure shape A: still no journey after retries. Either the transient didn't clear
    # or it's the stable wrapper bug (a bare {"error": ""}). The raw payload goes to the
    # debug log so the two can be told apart offline; the user-facing message stays plain.
    if not isinstance(data.get("journey"), dict):
        logger.debug(
            "routing still stale after %d attempts (routetype=%s): %s",
            attempts, args.get("routetype"), json.dumps(data)[:500],
        )
        err = data.get("error")
        if not err:
            return {
                "error": f"routing failed: empty response from routing service "
                f"({attempts} attempts); try a different travel mode or a more "
                f"specific address"
            }
        return {"error": f"routing failed: {err}"}
    journey = data["journey"]

    # Failure shape B: km4city envelope error_code != "0". error_message can read
    # "successful" even on success, so only error_code is authoritative ("0" means OK).
    resp = data.get("response") or {}
    err_code = resp.get("error_code")
    if err_code not in (None, "", "0", 0):
        err_msg = resp.get("error_message") or "unknown"
        return {"error": f"routing failed: {err_msg} (code={err_code})"}

    # Failure shape C: a success-looking envelope but empty routes. km4city returns this
    # for car-in-pedestrian-zone, src == dst, etc., with no 4xx.
    if not journey.get("routes"):
        return {"error": "no route found (empty routes list)"}
    return {"journey": journey}


def _in_tuscany(coords: Any) -> bool:
    """True when a GeoJSON `[lng, lat]` pair falls inside the Tuscany bbox."""
    if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
        return False
    lng, lat = coords[0], coords[1]
    if not (isinstance(lng, (int, float)) and isinstance(lat, (int, float))):
        return False
    return (
        TUSCANY_BBOX["min_lng"] <= lng <= TUSCANY_BBOX["max_lng"]
        and TUSCANY_BBOX["min_lat"] <= lat <= TUSCANY_BBOX["max_lat"]
    )


# Italian function words carry no signal when matching a feature label or city against
# the user's place text ("Piazza del Duomo" vs "PIAZZA DUOMO").
_LABEL_STOPWORDS = frozenset(
    "di del dell della dello dei degli delle da de la il lo le li gli l d e a i in".split()
)


def _label_tokens(text: str) -> set[str]:
    """Accent-stripped, casefolded word tokens minus Italian function words."""
    flat = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return {t for t in re.findall(r"\w+", flat.casefold()) if t not in _LABEL_STOPWORDS}


# The advisor is Florence-centric: a bare "Piazza Duomo" must resolve in Florence even
# though exact address matches exist in other Tuscan towns (Castelnuovo di Garfagnana,
# Pietrasanta). A city the user names explicitly always beats the default.
DEFAULT_CITY_TOKENS = frozenset({"firenze"})


def _narrow_by_city(features: list[dict[str, Any]], search: str) -> list[dict[str, Any]] | None:
    """City-confident subset of `features` (score order kept), or None.

    A feature's city counts as named when all its tokens appear in the search text
    ("via Roma, Pietrasanta"). A named city beats the Florence default. None means no
    feature belongs to a named city or Florence, and the caller decides the next step
    (next geocode pass, then the raw in-bbox list).
    """
    want = _label_tokens(search)

    def city_toks(f: dict[str, Any]) -> set[str]:
        return _label_tokens(str((f.get("properties") or {}).get("city") or ""))

    named = [f for f in features if (ct := city_toks(f)) and ct <= want]
    if named:
        return named
    florence = [f for f in features if city_toks(f) == DEFAULT_CITY_TOKENS]
    return florence or None


def _filter_geocode_to_tuscany(payload: Any, search: str) -> Any:
    """Keep only Tuscany-area features from a geocode result.

    Since the geocoder is no longer region-locked, a fuzzy Florence query can rank
    Spanish streets first. We drop out-of-region features but keep score order, so
    execute still reads the best in-region hit from the first feature. An empty
    in-region set becomes an actionable {"error": ...} that respond explains to the
    user. Non-FeatureCollection payloads (e.g. a backend error) pass straight through.
    """
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return payload
    features = payload.get("features")
    if not isinstance(features, list):
        return payload
    kept = [f for f in features if _in_tuscany((f.get("geometry") or {}).get("coordinates"))]
    if not kept:
        if logger.isEnabledFor(logging.DEBUG):
            sample = [
                {
                    "city": (f.get("properties") or {}).get("city"),
                    "coordinates": (f.get("geometry") or {}).get("coordinates"),
                }
                for f in features[:3]
            ]
            logger.debug(
                "geocode %r: %d raw hits, none in Tuscany bbox; first raw hits: %s",
                search, len(features), json.dumps(sample),
            )
        return {"error": f"no Tuscany-area match for {search!r}, try a more specific address"}
    return {**payload, "features": kept, "count": len(kept)}


# The geocoder defaults to lang="en"/logic="or". This project is Italy/Florence-only, so
# bias it to Italian: better ranking and labels that match the Italian search text in
# _pick_coord/_narrow_by_city. logic stays "or" (broad) by default; flip GEOCODE_LOGIC to
# "and" (stricter) and A/B it via scripts/probe_geocode.py before committing the change.
GEOCODE_LANG = "it"
GEOCODE_LOGIC = "or"


async def _geocode_address_first(client: Client, args: dict[str, Any]) -> Any:
    """Two-pass geocode: addresses first, POIs as fallback, city-confident hits first.

    With POIs included the server can rank a fuzzy catalogue hit above the real place
    (once a company 1.1 km west of "Piazza Duomo"), while pure address entries
    (excludePOI=true) sit on the routable street graph. But exact address matches exist
    all over Tuscany ("PIAZZA DUOMO" in Castelnuovo di Garfagnana and Pietrasanta
    outranked Florence), so a pass only wins outright when it has features in the city
    the user named (or Florence, the default). Ladder, all pinned to the Tuscany bbox:
      1. address pass, named-city/Florence subset
      2. POI pass, named-city/Florence subset (stations/landmarks are POI-only)
      3. whole-Tuscany address hits
      4. whole-Tuscany POI hits / {"error": ...}
    """
    search = str(args.get("search", ""))
    addresses = None  # rung 3: in-bbox address hits without city confidence
    try:
        first = _filter_geocode_to_tuscany(
            _unwrap(await client.call_tool(
                "address_search_location",
                {**args, "excludePOI": True, "lang": GEOCODE_LANG, "logic": GEOCODE_LOGIC},
            )),
            search,
        )
        if isinstance(first, dict) and first.get("type") == "FeatureCollection":
            narrowed = _narrow_by_city(first["features"], search)
            if narrowed is not None:
                logger.debug("geocode %r: address pass hit (excludePOI=true)", search)
                return {**first, "features": narrowed, "count": len(narrowed)}
            addresses = first
        logger.debug("geocode %r: address pass not city-confident, trying the POI pass", search)
    except Exception as e:
        logger.debug("geocode %r: address pass failed (%s), trying the POI pass", search, e)
    try:
        pois = _filter_geocode_to_tuscany(
            _unwrap(await client.call_tool(
                "address_search_location",
                {**args, "excludePOI": False, "lang": GEOCODE_LANG, "logic": GEOCODE_LOGIC},
            )),
            search,
        )
    except Exception:
        if addresses is not None:
            logger.debug("geocode %r: POI pass failed, keeping whole-Tuscany address hits", search)
            return addresses
        raise  # nothing left to try; exec_tool's outer handler turns this into {"error": ...}
    if isinstance(pois, dict) and pois.get("type") == "FeatureCollection":
        narrowed = _narrow_by_city(pois["features"], search)
        if narrowed is not None:
            logger.debug("geocode %r: POI pass hit", search)
            return {**pois, "features": narrowed, "count": len(narrowed)}
        if addresses is not None:
            logger.debug("geocode %r: no city-confident hit anywhere, keeping whole-Tuscany address hits", search)
            return addresses
        return pois
    return addresses if addresses is not None else pois


# The geocoder is non-deterministic over time: the same query returns 100 in-region
# hits one moment and 100% foreign hits (zero Tuscan) the next ("Università ... Morgagni"
# failed mid-chat yet geocoded fine minutes later when probed directly). The 2-pass +
# bbox filter recovers whenever any Tuscan hit comes back, so the only failure is the
# transient zero-Tuscan window; a bounded retry usually clears it.
GEOCODE_FLAKY_RETRIES = 2
GEOCODE_FLAKY_RETRY_DELAY_S = 1.5
_GEOCODE_TRANSIENT_HINT = "no Tuscany-area match"


async def geocode_with_retry(client: Client, args: dict[str, Any]) -> Any:
    """`_geocode_address_first` with bounded retries for the flaky zero-region window."""
    result = await _geocode_address_first(client, args)
    for attempt in range(1, GEOCODE_FLAKY_RETRIES + 1):
        if not (isinstance(result, dict) and _GEOCODE_TRANSIENT_HINT in str(result.get("error", ""))):
            break
        logger.debug(
            "geocode %r: transient zero-region result, retry %d/%d",
            args.get("search"), attempt, GEOCODE_FLAKY_RETRIES,
        )
        await asyncio.sleep(GEOCODE_FLAKY_RETRY_DELAY_S)
        result = await _geocode_address_first(client, args)
    return result


# Llama4 has a modest context window and degrades (hallucinates, or its backend 500s)
# when fed large tool payloads. slim_result_for_llm returns a compact view for the
# message history: top-K geocode hits with only the fields the model needs, routing
# without the huge WKT and per-arc objects. The orchestrator keeps the full result in
# its audit (tool_results), so the dashboard widget still gets complete data.
GEOCODE_LLM_KEEP = 5
PT_LEGS_LLM_KEEP = 10


def group_arc_legs(arcs: list[Any]) -> list[dict[str, Any]]:
    """Group consecutive routing arcs into journey legs by transport identity.

    Grouping key = (transport, transport_provider). This is provisional: the field
    carrying the bus line number hasn't been observed live yet (execute dumps the raw
    PT arcs to debug.log on the first real run). If the line actually lives in `desc`,
    two lines meeting at the same stop would merge, so recalibrate then. Every emitted
    field comes from an observed arc field (api-notes §2); missing ones are skipped.
    A `desc` of "nd" (no data) never names a leg endpoint.
    """
    legs: list[dict[str, Any]] = []
    last_key: tuple[Any, Any] | None = None
    for arc in arcs:
        if not isinstance(arc, dict):
            continue
        key = (arc.get("transport"), arc.get("transport_provider"))
        if not legs or key != last_key:
            leg: dict[str, Any] = {}
            if arc.get("transport") is not None:
                leg["transport"] = arc["transport"]
            if arc.get("transport_provider") is not None:
                leg["provider"] = arc["transport_provider"]
            if arc.get("start_datetime"):
                leg["start_datetime"] = arc["start_datetime"]
            legs.append(leg)
            last_key = key
        leg = legs[-1]
        desc = arc.get("desc")
        if desc and desc != "nd":
            leg.setdefault("from", desc)
            leg["to"] = desc
        if isinstance(arc.get("distance"), (int, float)):
            leg["distance_km"] = round(leg.get("distance_km", 0.0) + arc["distance"], 6)
        if arc.get("end_datetime"):
            leg["end_datetime"] = arc["end_datetime"]
    return legs


def slim_result_for_llm(name: str, result: Any) -> Any:
    """Compact a tool result for the LLM context. Full fidelity stays in the audit;
    this only shrinks what the model re-reads each turn. Errors and unknown shapes
    (TPL lists, etc.) pass through unchanged.

    Raw coordinates are withheld on purpose: the respond LLM once used geocode
    coordinates to fabricate a distance/ETA estimate when routing had failed, and with
    no coordinates in view there is nothing to improvise from. The widget and the
    execute node read the full payloads, never this view."""
    if not isinstance(result, dict) or "error" in result:
        return result
    if name == "address_search_location" and isinstance(result.get("features"), list):
        feats = [
            {
                "address": (f.get("properties") or {}).get("address"),
                "city": (f.get("properties") or {}).get("city"),
            }
            for f in result["features"][:GEOCODE_LLM_KEEP]
        ]
        return {"count": result.get("count"), "features": feats}
    if name == "routing" and isinstance(result.get("journey"), dict):
        journey = result["journey"]
        first = (journey.get("routes") or [{}])[0]
        base = {
            "distance_km": first.get("distance"),
            "eta": first.get("eta"),
            "time": first.get("time"),
        }
        legs = group_arc_legs(first.get("arc") or [])
        if len(legs) > 1:
            # A change of transport (a public-transport journey: walk + ride
            # segments). The model needs legs to narrate the trip, not a flat street
            # list. Single-group journeys (foot, car, or a PT request satisfied
            # entirely on foot) keep the street view below.
            return {"journey": {**base, "legs": legs[:PT_LEGS_LLM_KEEP]}}
        streets: list[str] = []
        for arc in first.get("arc") or []:
            desc = arc.get("desc")
            if desc and desc != "nd" and desc not in streets:  # drop unnamed and dupes
                streets.append(desc)
        return {"journey": {**base, "streets": streets}}
    return result


async def exec_tool(
    client: Client, name: str, args: dict[str, Any], *, routing_attempts: int | None = None
) -> Any:
    """Execute one tool call by forwarding it to the remote server. Never raises:
    returns the payload or {"error": ...}.

    routing goes through routing_with_retry (km4city quirk handling; routing_attempts
    caps its stale ladder). Every other tool passes straight through client.call_tool +
    _unwrap. The `authentication` arg is stripped (public backend).
    """
    try:
        if name not in TOOL_NAMES:
            return {"error": f"unknown tool {name!r}"}

        clean = {k: v for k, v in args.items() if k != "authentication"}

        if name == "routing":
            route_args = {
                "startlatitude": clean.get("startlatitude"),
                "startlongitude": clean.get("startlongitude"),
                "endlatitude": clean.get("endlatitude"),
                "endlongitude": clean.get("endlongitude"),
                "routetype": clean.get("routetype", "car"),
            }
            if clean.get("startdatetime"):
                route_args["startdatetime"] = clean["startdatetime"]
            return await routing_with_retry(client, route_args, attempts=routing_attempts)

        if name == "address_search_location":
            return await geocode_with_retry(client, clean)

        return _unwrap(await client.call_tool(name, clean))
    except Exception as e:
        return {"error": f"{name} call failed: {type(e).__name__}: {e}"}


async def reverse_geocode(client: Client, lat: float, lng: float) -> Any:
    """Reverse geocode a GPS point to an address via the `coordinates_to_address` tool.

    Foundation for a future near-me flow (browser GPS / map click -> coordinates -> the
    street/POI there): the user's exact point is unambiguous, so this avoids the weak
    forward-geocoding of a typed name (the native What-If widget is accurate for the same
    reason). NOT wired into the route flow yet (no GPS capture, intent classification
    unchanged). coordinates_to_address takes latitude/longitude as separate floats and
    returns {"result": [{number, address, municipality, province, roadUri, ...}, ...]}
    (the address candidates at that point; the first is the km4city street-number match),
    or {"error": ...}."""
    return await exec_tool(client, "coordinates_to_address", {"latitude": lat, "longitude": lng})
