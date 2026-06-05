"""Client-side MCP layer for the agentic advisor.

This module does NOT implement any tools — the tools live on referente's remote
`snap4agentic_advisor_native` server. Here we only: (1) connect to it, (2) ask it
for its own tool schemas via `list_tools()` and hand the LLM the subset we expose,
and (3) forward the LLM's chosen calls back to the server with `client.call_tool`,
unwrapping the response and smoothing referente's known km4city quirks.

Runtime = Snap4City JupyterHub: the dashboard's intranet IP is directly reachable
(GET http://192.168.1.117:8000/apps.json -> 200), so DASHBOARD_URL defaults to it.
Override with S4C_DASHBOARD_URL if the dashboard is exposed elsewhere. Dashboard
/apps.json carries the multi-server config; we narrow to the `native` server and
rewrite the internal IP to DASHBOARD_URL so the client hits the right entry point.

`exec_tool` is the single execution seam: it never raises — every failure comes
back as `{"error": ...}` so the agent loop can feed it to the model and recover.
The `routing` tool routes through `routing_with_retry`, which preserves the
km4city envelope quirks (lessons L2/L3/L7/L8); all other tools pass straight
through `_unwrap`.
"""
import asyncio
import json
import os
from typing import Any, Literal

import httpx
from fastmcp import Client

# L3 short-window stale workaround: referente's routing wrapper occasionally returns
# an empty body on cold start. Auto-retry once after a delay to mask the transient.
ROUTING_STALE_RETRIES = 1
ROUTING_STALE_RETRY_DELAY_S = 6.0

# km4city's geocoder is NOT region-locked anymore — its index now also covers
# Valencia (ES) and southern France, so a fuzzy `address_search_location` match for
# a Florence place can rank Spanish streets first (e.g. "Piazza del Duomo, Firenze"
# → 100 Valencia/France hits, zero Tuscan). We pin results to a Tuscany bbox
# client-side and force excludePOI=false so squares/landmarks are findable. See L11.
# Bounds are generous (whole region, not just Florence) since the advisor serves Tuscany.
TUSCANY_BBOX = {"min_lng": 9.6, "max_lng": 12.5, "min_lat": 42.2, "max_lat": 44.5}

# Runtime = JupyterHub: the intranet dashboard IP is directly reachable, so it's the
# default. Override via S4C_DASHBOARD_URL if the dashboard is exposed elsewhere.
DASHBOARD_URL = os.environ.get("S4C_DASHBOARD_URL", "http://192.168.1.117:8000")
INTERNAL_DASHBOARD_URL = "http://192.168.1.117:8000"
NATIVE_SERVER_ID = "snap4agentic_advisor_native"

RouteType = Literal["public_transport", "foot_shortest", "foot_quiet", "car"]

# The subset of the server's tools we expose to the LLM (core mobility set). This is
# a *selection*, not an implementation — the actual schemas are fetched from the
# server in `fetch_tool_schemas`.
EXPOSED_TOOLS = (
    "address_search_location",
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


def _to_openai_schema(tool: Any) -> dict[str, Any]:
    """MCP Tool (from list_tools) → OpenAI function schema. Drops `authentication`
    (public backend — never ask the model for a token)."""
    params = dict(getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}})
    props = dict(params.get("properties") or {})
    props.pop("authentication", None)
    params["properties"] = props
    if "required" in params:
        params["required"] = [r for r in params["required"] if r != "authentication"]
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "parameters": params,
        },
    }


async def fetch_tool_schemas(client: Client) -> list[dict[str, Any]]:
    """OpenAI function schemas for the exposed tools, taken from the server itself.

    The schemas come from the MCP server's own `list_tools()` — we never hand-write
    them, so signatures are always whatever the server currently declares.
    """
    by_name = {t.name: t for t in await client.list_tools()}
    return [_to_openai_schema(by_name[n]) for n in EXPOSED_TOOLS if n in by_name]


def _unwrap(result: Any) -> Any:
    """fastmcp.Client.call_tool result → structured payload (dict / list / scalar)."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    content = getattr(result, "content", None) or []
    if content:
        return json.loads(content[0].text)
    return None


async def _call_routing_once(client: Client, args: dict[str, Any]) -> dict[str, Any]:
    """Single `routing` tool call → {data} | {error}. Transient L3 handling lifted above."""
    try:
        result = await client.call_tool("routing", args)
    except Exception as e:
        return {"error": f"routing call failed: {type(e).__name__}: {e}"}
    data = _unwrap(result)
    if not isinstance(data, dict):
        return {"error": f"routing returned non-dict payload: {type(data).__name__}"}
    return {"data": data}


def _looks_stale(data: dict[str, Any]) -> bool:
    """Heuristic: does this payload look like the L3 cold-start stale shape?

    Stale = no `journey` dict (top-level empty wrap or unrecognized envelope).
    """
    return not isinstance(data.get("journey"), dict)


async def routing_with_retry(client: Client, args: dict[str, Any]) -> dict[str, Any]:
    """km4city routing with L3 stale retry + L2/L7/L8 envelope checks.

    args = {startlatitude, startlongitude, endlatitude, endlongitude, routetype, [startdatetime]}.
    Returns {"journey": {...}} on success or {"error": "<msg>"} on any failure shape.
    """
    # First attempt + bounded retry for L3 short-window stale (referente cold-start quirk).
    res = await _call_routing_once(client, args)
    for _ in range(ROUTING_STALE_RETRIES):
        if "error" in res:
            break
        if not _looks_stale(res["data"]):
            break
        await asyncio.sleep(ROUTING_STALE_RETRY_DELAY_S)
        res = await _call_routing_once(client, args)

    if "error" in res:
        return {"error": res["error"]}
    data = res["data"]

    # Failure shape A: still no journey after retry — L3 stale didn't clear, or L8 car-ZTL
    # wrapper bug (stable bare {"error": ""}). Surface plainly.
    if not isinstance(data.get("journey"), dict):
        err = data.get("error")
        if not err:
            return {"error": "routing failed: empty body (L3 stale didn't clear after retry)"}
        return {"error": f"routing failed: {err}"}
    journey = data["journey"]

    # Failure shape B: km4city envelope error_code != "0" (error_message can be "successful"
    # on success — only error_code distinguishes; "0" means OK). See lesson L7.
    resp = data.get("response") or {}
    err_code = resp.get("error_code")
    if err_code not in (None, "", "0", 0):
        err_msg = resp.get("error_message") or "unknown"
        return {"error": f"routing failed: {err_msg} (code={err_code})"}

    # Failure shape C: success-looking envelope but empty routes (L2: km4city returns this
    # for car-in-pedestrian-zone, src==dst, etc — no 4xx).
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


def _filter_geocode_to_tuscany(payload: Any, search: str) -> Any:
    """Keep only Tuscany-area features from an `address_search_location` result.

    km4city's geocoder is no longer region-locked (it now also indexes Valencia /
    southern France), so a fuzzy Florence query can rank Spanish streets first. Drop
    out-of-region features — score order is preserved, so the agent still reads the
    best in-region hit from the first feature. An empty in-region set becomes an
    actionable `{"error": ...}` the agent can recover from (rule 4 in AGENT_SYSTEM).
    Non-FeatureCollection payloads (e.g. a backend error) pass straight through.
    """
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return payload
    features = payload.get("features")
    if not isinstance(features, list):
        return payload
    kept = [f for f in features if _in_tuscany((f.get("geometry") or {}).get("coordinates"))]
    if not kept:
        return {"error": f"no Tuscany-area match for {search!r} — try a more specific address"}
    return {**payload, "features": kept, "count": len(kept)}


# Llama4 has a modest context window and degrades (hallucinates, or its backend
# 500s) when fed large tool payloads. `slim_result_for_llm` returns a compact view
# for the agent's MESSAGE history — top-K geocode hits with only the fields the model
# needs, routing without the huge WKT / per-arc objects. The orchestrator keeps the
# FULL result in its audit (`tool_results`), so the dashboard widget still gets
# complete data (incl. WKT). See lesson L12.
GEOCODE_LLM_KEEP = 5


def slim_result_for_llm(name: str, result: Any) -> Any:
    """Compact a tool result for the LLM context. Full fidelity stays in the audit;
    this only shrinks what the model re-reads each turn. Errors / unknown shapes
    (TPL lists, etc.) pass through unchanged."""
    if not isinstance(result, dict) or "error" in result:
        return result
    if name == "address_search_location" and isinstance(result.get("features"), list):
        feats = [
            {
                "address": (f.get("properties") or {}).get("address"),
                "city": (f.get("properties") or {}).get("city"),
                "coordinates": (f.get("geometry") or {}).get("coordinates"),  # [lng, lat]
            }
            for f in result["features"][:GEOCODE_LLM_KEEP]
        ]
        return {"count": result.get("count"), "features": feats}
    if name == "routing" and isinstance(result.get("journey"), dict):
        journey = result["journey"]
        first = (journey.get("routes") or [{}])[0]
        streets: list[str] = []
        for arc in first.get("arc") or []:
            desc = arc.get("desc")
            if desc and desc != "nd" and desc not in streets:  # drop unnamed + dupes
                streets.append(desc)
        return {
            "journey": {
                "distance_km": first.get("distance"),
                "eta": first.get("eta"),
                "time": first.get("time"),
                "streets": streets,
                "source_node": journey.get("source_node"),
                "destination_node": journey.get("destination_node"),
            }
        }
    return result


async def exec_tool(client: Client, name: str, args: dict[str, Any]) -> Any:
    """Execute one tool call by forwarding it to the remote server. NEVER raises —
    returns the payload or {"error": ...}.

    `routing` routes through routing_with_retry (keeps km4city quirk handling);
    every other tool passes straight through `client.call_tool` + `_unwrap`.
    `authentication` is stripped (public backend).
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
            return await routing_with_retry(client, route_args)

        if name == "address_search_location":
            # Force excludePOI=false so squares/landmarks are findable (not only
            # street numbers), then pin the fuzzy multi-region hits to Tuscany.
            clean["excludePOI"] = False
            payload = _unwrap(await client.call_tool(name, clean))
            return _filter_geocode_to_tuscany(payload, str(clean.get("search", "")))

        return _unwrap(await client.call_tool(name, clean))
    except Exception as e:
        return {"error": f"{name} call failed: {type(e).__name__}: {e}"}
