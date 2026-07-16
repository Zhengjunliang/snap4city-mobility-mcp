"""Client-side MCP layer.

This module implements no tools: they live on referente's remote
snap4agentic_advisor_native server and on our local mcp_server.py. Here we only
connect and run the graph's tool calls via client.call_tool, unwrapping the response.
The route flow drives its tool chain in Python; no LLM ever picks a tool.

The dashboard's intranet IP is reachable directly from the JupyterHub, so
DASHBOARD_URL defaults to it; override with S4C_DASHBOARD_URL if the dashboard is
elsewhere. /apps.json carries the multi-server config: we keep the native server and
rewrite the internal IP to DASHBOARD_URL.

exec_tool is the single execution seam and never raises: every failure comes back as
{"error": ...} so the graph can recover. address_search_location goes through
_geocode_address_first (2-pass address/POI, named-city preference); other tools pass
straight through _unwrap.
"""
import json
import logging
import os
import re
import unicodedata
from collections import OrderedDict
from typing import Any

import httpx
from fastmcp import Client

logger = logging.getLogger(__name__)

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
    "coordinates_to_address",  # reverse geocode: labels a GPS-defaulted origin for the reply
    "route",  # local: all-modes (foot/car/bus) route via the What-If router (mcp_server.py, L19/L46)
    "service_search_near_gps_position",  # nearest-category POIs: car parks + "farmacia più vicina" destinations
    "service_info_dev",  # latest realtime free-spaces for a car park (serviceUri + time window)
)
TOOL_NAMES = frozenset(EXPOSED_TOOLS)

# Tools served only by our local MCP server (mcp_server.py), with no referente remote
# equivalent — so they are NOT expected in the referente probe. `route` wraps the What-If
# router for every mode (L19/L46 — the referente remote `routing` tool is retired);
# geocode reuses referente's `address_search_location` name (it exists remotely, just
# broken) so it is NOT local-only here.
LOCAL_ONLY_TOOLS = frozenset({"route"})

# Parking discovery (car routes): search car parks near the destination, then read live
# free-spaces per spot. Calibrated by a JupyterHub probe (2026-06-26; the one-shot script is
# gone, see git history):
# - PARKING_CATEGORY="Car_park" CONFIRMED (serviceType "TransferServiceAndRenting_Car_park").
# - The search result carries NO free-spaces; the live count is on the orion/IoT car-park
#   entities and is fetched per-spot via service_info_dev, whose `realtime.results.bindings`
#   (newest first) carry freeParkingLots/capacity as string values (read_parking_realtime).
#   Plain POI car parks have no realtime → free stays None (the agreed degraded display).
PARKING_CATEGORY = "Car_park"  # probe-confirmed (2026-06-26)
PARKING_RADIUS_KM = 0.5
PARKING_MAX = 5
PARKING_REALTIME_FROMTIME = "1-hour"  # service_info_dev window; bindings[0] is the latest reading


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


def _narrow_by_city(features: list[dict[str, Any]], search: str) -> list[dict[str, Any]] | None:
    """Named-city subset of `features` (score order kept), or None.

    A feature's city counts as named when all its tokens appear in the search text
    ("via Roma, Pietrasanta"). None means the user named no city (or no feature belongs
    to it), and the caller decides the next step (next geocode pass, then the raw list —
    where GPS proximity ranking in orchestrator._pick_coord takes over).
    """
    want = _label_tokens(search)

    def city_toks(f: dict[str, Any]) -> set[str]:
        return _label_tokens(str((f.get("properties") or {}).get("city") or ""))

    named = [f for f in features if (ct := city_toks(f)) and ct <= want]
    return named or None


# The geocoder defaults to lang="en"/logic="or". km4city is an Italian dataset, so bias
# it to Italian: better ranking and labels that match the Italian search text in
# _pick_coord/_narrow_by_city. logic stays "or" (broad) by default; A/B "and" (stricter)
# against real queries before committing a change.
GEOCODE_LANG = "it"
GEOCODE_LOGIC = "or"


# Geocode results are cached for the life of the process: the km4city address index is
# static, so the same place text always resolves to the same FeatureCollection, and a demo
# (or a multi-turn trip that keeps re-stating its endpoints) re-asks for the same handful of
# places. The cache holds the FINAL 2-pass result keyed by the raw search text; the caller
# still picks its own feature from it (_pick_coord ranks by the GPS/anchor of THIS turn, so
# caching upstream of that changes no answer). Failures are never cached — a transient
# network error must not pin an {"error": ...} for the whole session.
_GEOCODE_CACHE: "OrderedDict[str, Any]" = OrderedDict()
GEOCODE_CACHE_MAX = 128


def geocode_cache_clear() -> None:
    """Empty the geocode cache. Tests must call this between cases (autouse fixture in
    tests/conftest.py): the cache outlives a single test, and a hit would silently skip a
    queued FakeClient response, shifting every later pop by one."""
    _GEOCODE_CACHE.clear()


async def _geocode_address_first(client: Client, args: dict[str, Any]) -> Any:
    """Two-pass geocode: addresses first, POIs as fallback, named-city hits first.

    With POIs included the server can rank a fuzzy catalogue hit above the real place
    (once a company 1.1 km west of "Piazza Duomo"), while pure address entries
    (excludePOI=true) sit on the routable street graph. Exact address matches for a
    common name exist in many towns, so a pass only wins outright when it has features
    in the city the user named. Ladder:
      1. address pass, named-city subset
      2. POI pass, named-city subset (stations/landmarks are POI-only)
      3. address hits (no city named — GPS-nearest picking happens in _pick_coord)
      4. POI hits / {"error": ...}

    The result is memoized per search text (see _GEOCODE_CACHE).
    """
    search = str(args.get("search", ""))
    key = " ".join(search.casefold().split())
    if key in _GEOCODE_CACHE:
        logger.debug("geocode %r: cache hit", search)
        _GEOCODE_CACHE.move_to_end(key)
        return _GEOCODE_CACHE[key]
    found = await _geocode_uncached(client, args, search)
    # Only a real hit is worth keeping: an error (or an empty/oddly-shaped answer) may be
    # transient, and pinning it would break every later turn asking for the same place.
    if isinstance(found, dict) and found.get("features"):
        _GEOCODE_CACHE[key] = found
        if len(_GEOCODE_CACHE) > GEOCODE_CACHE_MAX:
            _GEOCODE_CACHE.popitem(last=False)  # oldest out (LRU: hits move to the end)
    return found


async def _geocode_uncached(client: Client, args: dict[str, Any], search: str) -> Any:
    """The 2-pass ladder itself (see _geocode_address_first, which memoizes it)."""
    addresses = None  # rung 3: address hits without a named city
    try:
        first = _unwrap(await client.call_tool(
            "address_search_location",
            {**args, "excludePOI": True, "lang": GEOCODE_LANG, "logic": GEOCODE_LOGIC},
        ))
        if isinstance(first, dict) and first.get("type") == "FeatureCollection":
            narrowed = _narrow_by_city(first["features"], search)
            if narrowed is not None:
                logger.debug("geocode %r: address pass hit (excludePOI=true)", search)
                return {**first, "features": narrowed, "count": len(narrowed)}
            if first["features"]:
                addresses = first
        logger.debug("geocode %r: address pass not city-confident, trying the POI pass", search)
    except Exception as e:
        logger.debug("geocode %r: address pass failed (%s), trying the POI pass", search, e)
    try:
        pois = _unwrap(await client.call_tool(
            "address_search_location",
            {**args, "excludePOI": False, "lang": GEOCODE_LANG, "logic": GEOCODE_LOGIC},
        ))
    except Exception:
        if addresses is not None:
            logger.debug("geocode %r: POI pass failed, keeping address hits", search)
            return addresses
        raise  # nothing left to try; exec_tool's outer handler turns this into {"error": ...}
    if isinstance(pois, dict) and pois.get("type") == "FeatureCollection":
        narrowed = _narrow_by_city(pois["features"], search)
        if narrowed is not None:
            logger.debug("geocode %r: POI pass hit", search)
            return {**pois, "features": narrowed, "count": len(narrowed)}
        if addresses is not None:
            logger.debug("geocode %r: no named-city hit anywhere, keeping address hits", search)
            return addresses
        return pois
    return addresses if addresses is not None else pois


# Llama4 has a modest context window and degrades (hallucinates, or its backend 500s)
# when fed large tool payloads. slim_result_for_llm returns a compact view for the
# message history: top-K geocode hits with only the fields the model needs, routing
# without the huge WKT and per-arc objects. The orchestrator keeps the full result in
# its audit (tool_results), so the dashboard widget still gets complete data.
GEOCODE_LLM_KEEP = 5
PT_LEGS_LLM_KEEP = 10


def group_arc_legs(arcs: list[Any]) -> list[dict[str, Any]]:
    """Group consecutive routing arcs into journey legs by transport identity.

    Grouping key = (transport, transport_provider): a walk arc ("foot", None) and a bus arc
    ("bus", operator) split into separate legs, so a public-transport journey comes out as
    walk -> ride -> walk. A ride's boarding arc (the first of its group) also carries the raw
    `line`, `headsign` and full `stops` (from _bus_arcs), copied onto the leg so respond can
    name the line and list the stops. Consecutive rides by the same operator still merge (two
    lines of one operator are not yet split); single-line urban trips are unaffected.
    Every emitted field comes from an observed arc field; missing ones are skipped. A `desc`
    of "nd" (no data) never names a leg endpoint.
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
            if arc.get("line") is not None:
                leg["line"] = arc["line"]
            if arc.get("headsign") is not None:
                leg["headsign"] = arc["headsign"]
            if isinstance(arc.get("stops"), list) and arc["stops"]:
                leg["stops"] = arc["stops"]
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


def read_parking_realtime(result: Any) -> dict[str, int | None]:
    """Latest free/total spaces from a service_info_dev response for a car park.

    Shape (JupyterHub probe, 2026-06-26): result["realtime"]["results"]["bindings"] is a
    list newest-first; bindings[0] carries {"freeParkingLots": {"value": "31"},
    "capacity": {"value": "202"}, ...} as string values. Returns {"free_spaces", "total_spaces"}
    with None when realtime is absent (plain POI car parks) or unparseable."""
    out: dict[str, int | None] = {"free_spaces": None, "total_spaces": None}
    if not isinstance(result, dict):
        return out
    rt = result.get("realtime")
    binds = (rt.get("results") or {}).get("bindings") if isinstance(rt, dict) else None
    if not isinstance(binds, list) or not binds or not isinstance(binds[0], dict):
        return out
    latest = binds[0]
    for key, field in (("freeParkingLots", "free_spaces"), ("capacity", "total_spaces")):
        cell = latest.get(key)
        val = cell.get("value") if isinstance(cell, dict) else None
        try:
            out[field] = int(float(val))
        except (TypeError, ValueError):
            pass
    return out


def _find_feature_list(obj: Any) -> list | None:
    """Locate the GeoJSON features list inside a service-search result of unknown nesting.

    The live envelope (JupyterHub probe, 2026-06-26) is
    {"result": [[uri, ...], {"Services": {"features": [...]}}]} — a 2-element list whose
    second item nests the features under "Services". We also accept a direct `features`,
    a `Service`/`Services` wrapper, or a `result` dict, so the parser survives shape drift."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("features"), list):
        return obj["features"]
    for k in ("Services", "Service"):
        v = obj.get(k)
        if isinstance(v, dict) and isinstance(v.get("features"), list):
            return v["features"]
    inner = obj.get("result")
    if isinstance(inner, list):
        for el in inner:
            found = _find_feature_list(el)
            if found is not None:
                return found
    elif isinstance(inner, dict):
        return _find_feature_list(inner)
    return None


def parse_service_features(result: Any) -> list[dict[str, Any]]:
    """Normalize a service_search_near_gps_position result into service dicts.

    Used for every nearest-category search: car parks near the destination AND a
    category destination ("farmacia più vicina"). The backend envelope is "an array of
    URIs, raw grouped GeoJSON, and flattened GeoJSON" (probe-native-tools.json); the live
    shape nests the features under result[1].Services (see _find_feature_list), already
    sorted by distance from the search point. Each item yields
    {name, lat, lng, uri, free_spaces} with missing fields as None. free_spaces starts as
    None on every spot: the search itself carries no occupancy (L33). A car park's live
    count is fetched afterwards, per spot, from service_info_dev (read_parking_realtime,
    driven by orchestrator._enrich_parking). Returns [] on error / unrecognized shape."""
    if not isinstance(result, dict) or "error" in result:
        return []
    feats = _find_feature_list(result)
    if not isinstance(feats, list):
        return []
    out: list[dict[str, Any]] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") if isinstance(f.get("properties"), dict) else f
        geom = f.get("geometry") if isinstance(f.get("geometry"), dict) else {}
        coords = geom.get("coordinates")
        lat = lng = None
        if isinstance(coords, list) and len(coords) >= 2:
            try:
                lng, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                lat = lng = None
        out.append({
            "name": props.get("name") or props.get("serviceName") or props.get("address"),
            "lat": lat,
            "lng": lng,
            "uri": props.get("serviceUri") or props.get("serviceuri") or f.get("serviceUri"),
            "free_spaces": None,  # filled from service_info_dev (car parks only)
        })
    return out


def _stop_view(stop: Any) -> dict[str, Any] | None:
    """One stop as the reply needs it: name + HH:MM. The ISO date/seconds are dropped —
    the reply must not print them anyway (L43), and they are pure prompt weight."""
    if not isinstance(stop, dict):
        return None
    raw = stop.get("time")
    hhmm = raw.split("T", 1)[1][:5] if isinstance(raw, str) and "T" in raw else None
    return {"name": stop.get("name"), "time": hhmm}


def _leg_boarding(leg: dict[str, Any]) -> dict[str, Any]:
    """A journey leg with its stop list collapsed to board + alight (+ how many in total).

    Where the rider gets on and off is the whole content of a ride leg for the reply; the
    stops in between are never named. Keeping them only grew the respond prompt (a 24-stop
    bus trip carried 24 ISO timestamps) and gave the model more rows to garble. The raw ISO
    `start_datetime` / `end_datetime` go too: they are the board/alight times in the very
    format the reply must NOT print (dates and seconds, L43) — no ISO instant is left in the
    model's view to copy. Legs without stops keep their other fields. Full list: the audit."""
    stops = leg.get("stops")
    slim = {k: v for k, v in leg.items() if k not in ("stops", "start_datetime", "end_datetime")}
    if isinstance(stops, list) and stops:
        slim["board"] = _stop_view(stops[0])
        slim["alight"] = _stop_view(stops[-1])
        slim["stops_total"] = len(stops)
    return slim


def slim_result_for_llm(name: str, result: Any) -> Any:
    """Compact a tool result for the LLM context. Full fidelity stays in the audit;
    this only shrinks what the model re-reads each turn. Errors and unknown shapes
    pass through unchanged.

    Raw coordinates are withheld on purpose: the respond LLM once used geocode
    coordinates to fabricate a distance/ETA estimate when routing had failed, and with
    no coordinates in view there is nothing to improvise from. The widget and the
    execute node read the full payloads, never this view."""
    if not isinstance(result, dict) or "error" in result:
        return result
    if name == "address_search_location" and isinstance(result.get("features"), list):
        feats = []
        for f in result["features"][:GEOCODE_LLM_KEEP]:
            props = f.get("properties") or {}
            item = {"address": props.get("address"), "city": props.get("city")}
            if props.get("civic"):
                item["civic"] = props["civic"]  # house-number hit (StreetNumber, L52)
            feats.append(item)
        return {"count": result.get("count"), "features": feats}
    if name == "routing" and isinstance(result.get("journey"), dict):
        journey = result["journey"]
        first = (journey.get("routes") or [{}])[0]
        base = {"distance_km": first.get("distance"), "time": first.get("time")}
        legs = group_arc_legs(first.get("arc") or [])
        if len(legs) > 1:
            # A change of transport (a public-transport journey: walk + ride
            # segments). The model needs legs to narrate the trip, not a flat street
            # list. Single-group journeys (foot, car, or a PT request satisfied
            # entirely on foot) keep the street view below. A ride leg is cut down to
            # its BOARD and ALIGHT stop (see _leg_boarding): the intermediate stops the
            # bus rolls through are noise the reply never uses — they only inflate the
            # respond prompt (and give the model more to misread). Full list: the audit.
            kept = [_leg_boarding(leg) for leg in legs[:PT_LEGS_LLM_KEEP]]
            return {"journey": {**base, "legs": kept}}
        streets: list[str] = []
        for arc in first.get("arc") or []:
            desc = arc.get("desc")
            if desc and desc != "nd" and desc not in streets:  # drop unnamed and dupes
                streets.append(desc)
        return {"journey": {**base, "streets": streets}}
    if name == "service_search_near_gps_position":
        spots = parse_service_features(result)
        if spots:
            return {"count": len(spots), "services": [
                {"name": s["name"], "free_spaces": s["free_spaces"]} for s in spots[:PARKING_MAX]
            ]}
    return result


async def exec_tool(client: Client, name: str, args: dict[str, Any]) -> Any:
    """Execute one tool call by forwarding it to the given client. Never raises:
    returns the payload or {"error": ...}.

    address_search_location goes through _geocode_address_first (2-pass address/POI).
    Every other tool passes straight through client.call_tool + _unwrap. The
    `authentication` arg is stripped (public backend).
    """
    try:
        if name not in TOOL_NAMES:
            return {"error": f"unknown tool {name!r}"}

        clean = {k: v for k, v in args.items() if k != "authentication"}

        if name == "address_search_location":
            return await _geocode_address_first(client, clean)

        return _unwrap(await client.call_tool(name, clean))
    except Exception as e:
        return {"error": f"{name} call failed: {type(e).__name__}: {e}"}


async def reverse_geocode(client: Client, lat: float, lng: float) -> Any:
    """Reverse geocode a GPS point to an address via the `coordinates_to_address` tool.

    Used by the near-me flow: when the user gives no origin and the browser sent a GPS
    position, execute defaults the origin to that point and calls this so respond can say
    "dalla tua posizione (vicino a <address>)". coordinates_to_address takes
    latitude/longitude as separate floats and returns
    {"result": [{number, address, municipality, province, roadUri, ...}, ...]}
    (the address candidates at that point; the first is the km4city street-number match),
    or {"error": ...}."""
    return await exec_tool(client, "coordinates_to_address", {"latitude": lat, "longitude": lng})
