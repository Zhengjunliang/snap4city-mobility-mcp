"""Langgraph orchestrator — chain address_search_location + routing via remote MCP.

Library module. Hits referente's remote `snap4agentic_advisor_native` MCP server
over HTTP Streamable transport. CLI entry point lives in cli.py — this module
exposes only run_trip().

Runtime = Snap4City JupyterHub: the dashboard's intranet IP is directly reachable
(verified: GET http://192.168.1.117:8000/apps.json → 200), so DASHBOARD_URL
defaults to it — no VPN/SSH tunnel. Override with S4C_DASHBOARD_URL if the
dashboard is exposed elsewhere.
Dashboard /apps.json carries the multi-server config; we narrow to the `native`
server and rewrite the internal IP (192.168.1.117:8000) to DASHBOARD_URL so the
client hits whichever entry point the current environment exposes.
"""
import asyncio
import json
import os
from functools import partial
from typing import Any, Literal, TypedDict

import httpx
from fastmcp import Client
from langgraph.graph import END, StateGraph

# L3 short-window stale workaround: referente's routing wrapper occasionally returns
# an empty body on cold start. Auto-retry once after a delay to mask the transient.
ROUTING_STALE_RETRIES = 1
ROUTING_STALE_RETRY_DELAY_S = 6.0

# Runtime = JupyterHub: the intranet dashboard IP is directly reachable, so it's the
# default. Override via S4C_DASHBOARD_URL if the dashboard is exposed elsewhere.
DASHBOARD_URL = os.environ.get("S4C_DASHBOARD_URL", "http://192.168.1.117:8000")
INTERNAL_DASHBOARD_URL = "http://192.168.1.117:8000"
NATIVE_SERVER_ID = "snap4agentic_advisor_native"


async def _build_config() -> dict[str, Any]:
    """Fetch dashboard /apps.json, keep only native server, rewrite URL to localhost."""
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


RouteType = Literal["public_transport", "foot_shortest", "foot_quiet", "car"]


class TripState(TypedDict, total=False):
    origin_text: str
    destination_text: str
    route_type: RouteType
    origin_lat: float | None
    origin_lng: float | None
    destination_lat: float | None
    destination_lng: float | None
    journey: Any
    error: str | None
    final: dict[str, Any]


def _unwrap(result: Any) -> Any:
    """fastmcp.Client.call_tool result → structured payload (dict / list / scalar)."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    content = getattr(result, "content", None) or []
    if content:
        return json.loads(content[0].text)
    return None


def _first_coord(payload: Any) -> tuple[float | None, float | None]:
    """GeoJSON FeatureCollection → (lat, lng) of first feature, or (None, None).

    GeoJSON coordinates order is [lng, lat] per RFC 7946.
    """
    if not isinstance(payload, dict):
        return None, None
    features = payload.get("features")
    if not features:
        return None, None
    coords = (features[0].get("geometry") or {}).get("coordinates")
    if not coords or len(coords) < 2:
        return None, None
    lng, lat = coords[0], coords[1]
    return lat, lng


async def _resolve_location(
    client: Client, text: str
) -> tuple[tuple[float, float] | None, str | None]:
    """((lat, lng), error). coord 为 None 时 error 必非 None 且非空字串."""
    try:
        result = await client.call_tool(
            "address_search_location", {"search": text, "maxresults": 1}
        )
    except Exception as e:
        return None, f"geocode call failed: {type(e).__name__}: {e}"
    lat, lng = _first_coord(_unwrap(result))
    if lat is None or lng is None:
        return None, f"geocode returned no match for {text!r}"
    return (lat, lng), None


async def _resolve_endpoint(
    state: TripState,
    *,
    client: Client,
    in_key: Literal["origin_text", "destination_text"],
    out_lat_key: Literal["origin_lat", "destination_lat"],
    out_lng_key: Literal["origin_lng", "destination_lng"],
) -> dict[str, Any]:
    """共享 resolver: origin / destination 仅差 state 字段名."""
    coord, err = await _resolve_location(client, state[in_key])
    if err:
        return {"error": err}
    return {out_lat_key: coord[0], out_lng_key: coord[1]}


async def _call_routing_once(client: Client, args: dict[str, Any]) -> dict[str, Any]:
    """Single `routing` tool call → (data | error). Lift transient L3 cold-start handling above."""
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

    Two known forms:
      A. Top-level `{"error": ...}` with no `journey` key.
      B. Top-level dict with no `journey` key and no recognized envelope fields.
    """
    return not isinstance(data.get("journey"), dict)


async def _compute_route(state: TripState, *, client: Client) -> dict[str, Any]:
    args = {
        "startlatitude": state["origin_lat"],
        "startlongitude": state["origin_lng"],
        "endlatitude": state["destination_lat"],
        "endlongitude": state["destination_lng"],
        "routetype": state.get("route_type", "foot_shortest"),
    }
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

    # Failure shape A: still no journey after retry — L3 stale didn't clear, surface plainly.
    if not isinstance(data.get("journey"), dict):
        err = data.get("error")
        if not err:
            return {"error": "routing failed: empty body (L3 stale didn't clear after retry)"}
        return {"error": f"routing failed: {err}"}
    journey = data["journey"]

    # Failure shape B: km4city envelope error_code != "0" (error_message can be "successful"
    # on success — only error_code distinguishes; "0" means OK).
    resp = data.get("response") or {}
    err_code = resp.get("error_code")
    if err_code not in (None, "", "0", 0):
        err_msg = resp.get("error_message") or "unknown"
        return {"error": f"routing failed: {err_msg} (code={err_code})"}

    # Failure shape C: success-looking envelope but empty routes (L2: km4city returns this
    # for car-in-pedestrian-zone, src==dst, etc — no 4xx).
    routes = journey.get("routes")
    if not routes:
        return {"error": "no route found (empty routes list)"}
    return {"journey": journey}


def _format_output(state: TripState) -> dict[str, Any]:
    if state.get("error") is not None:
        return {"final": {"ok": False, "error": state["error"]}}
    journey = state.get("journey") if isinstance(state.get("journey"), dict) else {}
    routes = journey.get("routes") if isinstance(journey, dict) else None
    first = routes[0] if routes else {}
    return {
        "final": {
            "ok": True,
            "summary": {
                "origin": state.get("origin_text"),
                "destination": state.get("destination_text"),
                "route_type": state.get("route_type"),
                "origin_lat": state.get("origin_lat"),
                "origin_lng": state.get("origin_lng"),
                "destination_lat": state.get("destination_lat"),
                "destination_lng": state.get("destination_lng"),
                "distance_km": first.get("distance"),
                "eta": first.get("eta"),
                "wkt_head": (first.get("wkt") or "")[:80],
            },
            "raw_journey": journey,
        }
    }


def _build_graph(client: Client):
    g = StateGraph(TripState)
    g.add_node(
        "resolve_origin",
        partial(
            _resolve_endpoint,
            client=client,
            in_key="origin_text",
            out_lat_key="origin_lat",
            out_lng_key="origin_lng",
        ),
    )
    g.add_node(
        "resolve_destination",
        partial(
            _resolve_endpoint,
            client=client,
            in_key="destination_text",
            out_lat_key="destination_lat",
            out_lng_key="destination_lng",
        ),
    )
    g.add_node("compute_route", partial(_compute_route, client=client))
    g.add_node("format_output", _format_output)
    g.set_entry_point("resolve_origin")
    g.add_conditional_edges(
        "resolve_origin",
        lambda s: "error" if s.get("error") is not None else "ok",
        {"ok": "resolve_destination", "error": "format_output"},
    )
    g.add_conditional_edges(
        "resolve_destination",
        lambda s: "error" if s.get("error") is not None else "ok",
        {"ok": "compute_route", "error": "format_output"},
    )
    g.add_edge("compute_route", "format_output")
    g.add_edge("format_output", END)
    return g.compile()


async def run_trip(
    origin: str,
    destination: str,
    route_type: RouteType = "foot_shortest",
) -> dict[str, Any]:
    """One-shot orchestration over the remote referente MCP server."""
    cfg = await _build_config()
    async with Client(cfg) as client:
        graph = _build_graph(client)
        result: TripState = await graph.ainvoke(
            {
                "origin_text": origin,
                "destination_text": destination,
                "route_type": route_type,
            }
        )
    return result.get("final", {"ok": False, "error": "no final state produced"})
