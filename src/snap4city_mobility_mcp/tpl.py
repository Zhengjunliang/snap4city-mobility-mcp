"""Deterministic TPL (trasporto pubblico locale) discovery chains.

`execute` (orchestrator.py) delegates the tpl_* intents here; like the route
flow, every step is plain Python driving remote MCP tools — the LLM never picks
a tool (lesson L13). Chains per intent:

  tpl_lines    agencies → resolve agency → tpl_lines(agency)
  tpl_routes   agencies → resolve agency → tpl_routes_by_line(line, agency)
  tpl_stops    … → tpl_stops_by_route(route) for the first 2 routes (directions)
  tpl_timeline … → token-match the stop name → tpl_stop_timeline(stop)

The tpl payload shapes have never been observed live (tool descriptions only —
api-notes §3): every extractor here is defensive, and `run_tpl_flow` logs raw
payload heads to debug.log (gitignored) so the first JupyterHub run can
calibrate them. This module also owns the tpl slim views (L12) and the tpl
widget-data extraction, keeping orchestrator.py to dispatch + prompts.
"""
import json
import logging
from typing import Any

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import _label_tokens, exec_tool

logger = logging.getLogger(__name__)

TPL_INTENTS = ("tpl_lines", "tpl_routes", "tpl_stops", "tpl_timeline")

# Required slots per tpl intent: (answer label, slot key). Shared by run_tpl_flow
# (skip the chain) and respond's missing-slot ask (orchestrator._missing_slots).
REQUIRED_SLOTS = {
    "tpl_routes": (("line", "line_text"),),
    "tpl_stops": (("line", "line_text"),),
    # No stops-near-GPS tool is exposed: resolving a stop NAME to its service URI
    # deterministically needs the line's stop list, so the line is required too.
    "tpl_timeline": (("line", "line_text"), ("stop", "stop_text")),
}

# First N routes of a line probed for stops — usually the two directions.
STOPS_ROUTES_PROBED = 2

# LLM-view caps (L12: lines can be 100+, a stop list carries a full GeoJSON).
TPL_LLM_KEEPS = {
    "tpl_agencies": 20,
    "tpl_lines": 30,
    "tpl_routes_by_line": 10,
    "tpl_stops_by_route": 30,
    "tpl_stop_timeline": 15,
}
TPL_TOOL_NAMES = frozenset(TPL_LLM_KEEPS)

# Widget-data caps (full fidelity is pointless for a chat widget; the audit in
# tool_results keeps everything).
TPL_DATA_KEEP = 50
ROUTES_DATA_KEEP = 10

# Florence default network. km4city has NO single "Autolinee Toscane"/"AtF"/"ATAF" entry
# (the URI in the server's own tpl_lines example, '.../resource/AtF', does NOT exist live —
# probe_tpl STEP 1): the brand is split into ~40 sub-networks (ExtraUrbano <provincia> /
# Urbano <città> / Linee Regionali). Florence city lines (incl. line 6) live under
# "Autolinee Toscane - Urbano Area Metropolitana Fiorentina" (…_Agency_888-48), so a bare or
# brand-only agency request resolves there. Token-based so it survives minor name variants.
_FLORENCE_URBAN_TOKENS = frozenset({"firenze", "fiorentina", "metropolitana"})


def _is_florence_urban(name: str | None) -> bool:
    toks = _label_tokens(str(name or ""))
    return "urbano" in toks and bool(toks & _FLORENCE_URBAN_TOKENS)


def _unwrap_tpl(payload: Any) -> Any:
    """Strip the FastMCP non-object wrapper. Servers deliver non-dict structured
    output as {"result": [...]} (which `_unwrap` returns verbatim), while the
    documented tpl shapes are bare arrays — accept both."""
    if (
        isinstance(payload, dict)
        and len(payload) == 1
        and isinstance(payload.get("result"), (list, dict))
    ):
        return payload["result"]
    return payload


def _generic_list(payload: Any) -> list[Any]:
    """The item list inside a tpl payload: bare list, or the first list value of
    a one-purpose dict (tpl_agencies is documented as a dict holding the array)."""
    payload = _unwrap_tpl(payload)
    if isinstance(payload, dict) and "error" not in payload:
        for v in payload.values():
            if isinstance(v, list):
                return v
        return []
    return payload if isinstance(payload, list) else []


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _agency_entries(payload: Any) -> list[dict[str, Any]]:
    """[{name, uri}] from a tpl_agencies payload (key names unverified — probe)."""
    out = []
    for item in _generic_list(payload):
        if not isinstance(item, dict):
            continue
        uri = _first_str(item, ("uri", "agency", "serviceUri"))
        if uri:
            out.append({"name": _first_str(item, ("name", "agencyName", "label")), "uri": uri})
    return out


def _route_uris(payload: Any) -> list[str]:
    """Route URIs from a tpl_routes_by_line payload (key per the sibling
    transport_routes_search_* schema: routeUri)."""
    uris = []
    for item in _generic_list(payload):
        if isinstance(item, dict):
            uri = _first_str(item, ("routeUri", "uri", "route"))
            if uri:
                uris.append(uri)
    return uris


def _stop_entries(payload: Any) -> list[dict[str, Any]]:
    """[{name, uri}] from a tpl_stops_by_route payload.

    Documented shape: [service URI array, GeoJSON FeatureCollection]. Assumed
    mapping (calibrate on the first live run — run_tpl_flow logs raw heads):
    name from feature properties.name (fallback .address); URI from
    properties.serviceUri, else POSITIONAL alignment with the URI array.
    """
    payload = _unwrap_tpl(payload)
    uris: list[Any] | None = None
    geo: dict[str, Any] | None = None
    if isinstance(payload, list):
        for part in payload:
            if isinstance(part, list) and uris is None:
                uris = part
            elif isinstance(part, dict) and part.get("type") == "FeatureCollection":
                geo = part
    elif isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        geo = payload
    entries = []
    for i, f in enumerate((geo or {}).get("features") or []):
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        uri = _first_str(props, ("serviceUri", "uri"))
        if uri is None and isinstance(uris, list) and i < len(uris) and isinstance(uris[i], str):
            uri = uris[i]
        if uri:
            entries.append({"name": _first_str(props, ("name", "address")), "uri": uri})
    if not entries and isinstance(uris, list):  # no usable GeoJSON — URIs only
        entries = [{"name": None, "uri": u} for u in uris if isinstance(u, str)]
    return entries


def _match_stop(entries: list[dict[str, Any]], stop_text: str) -> dict[str, Any] | None:
    """First stop whose name tokens are all covered by the user's stop text —
    same strict subset direction as orchestrator._pick_coord (L17)."""
    want = _label_tokens(stop_text)
    if not want:
        return None
    for e in entries:
        toks = _label_tokens(str(e.get("name") or ""))
        if toks and toks <= want:
            return e
    return None


def _resolve_agency(agencies: list[dict[str, Any]], agency_text: str) -> str | None:
    """Agency URI for the user's text, or the Florence-urban default; None = unknown
    (the agencies audit entry reaches respond, which asks the user to pick).

    Brand match is bidirectional: a generic brand ("Autolinee Toscane") is a SUBSET of the
    specific sub-network names, while a verbose user phrase can be a SUPERSET — accept either
    direction. When a brand matches many sub-networks, the Florence-centric advisor prefers
    the Florence urban one (proven live: 888-48 + line "6" → 22 routes; ExtraUrbano Arezzo
    → []). Empty text resolves to that same Florence-urban default. See lesson L21.
    """
    if agency_text.strip():
        want = _label_tokens(agency_text)
        cands = [
            a
            for a in agencies
            if (toks := _label_tokens(str(a.get("name") or ""))) and (toks <= want or want <= toks)
        ]
        if cands:
            return next(
                (a["uri"] for a in cands if _is_florence_urban(a.get("name"))), cands[0]["uri"]
            )
        return None
    return next((a["uri"] for a in agencies if _is_florence_urban(a.get("name"))), None)


async def run_tpl_flow(client: Client, slots: dict[str, Any]) -> dict[str, Any]:
    """Deterministically run the discovery chain for a tpl_* intent (NO LLM).

    Returns {"tool_results", "unsupported"} like execute's route flow — never a
    `missing` key (AdvisorState has no such channel; respond re-derives missing
    slots from `slots` via REQUIRED_SLOTS).
    """
    intent = slots.get("intent") or ""
    results: list[dict[str, Any]] = []

    if any(not (slots.get(key) or "").strip() for _, key in REQUIRED_SLOTS.get(intent, ())):
        return {"tool_results": results, "unsupported": True}

    async def _call(name: str, args: dict[str, Any]) -> Any:
        result = await exec_tool(client, name, args)
        results.append({"name": name, "args": json.dumps(args), "result": result})
        if logger.isEnabledFor(logging.DEBUG):
            # Raw head incl. the outermost shape (bare list vs {"result": ...}) —
            # the calibration data for every assumption in this module.
            logger.debug(
                "tool %s %s -> raw head: %s",
                name, args, json.dumps(result, ensure_ascii=False, default=str)[:1500],
            )
        return result

    agencies = _agency_entries(await _call("tpl_agencies", {}))
    agency_uri = _resolve_agency(agencies, slots.get("agency_text") or "")
    if agency_uri is None:
        return {"tool_results": results, "unsupported": False}

    if intent == "tpl_lines":
        await _call("tpl_lines", {"agency": agency_uri})
        return {"tool_results": results, "unsupported": False}

    line_text = (slots.get("line_text") or "").strip()
    routes_payload = await _call("tpl_routes_by_line", {"line": line_text, "agency": agency_uri})
    if intent == "tpl_routes":
        return {"tool_results": results, "unsupported": False}

    stop_payloads = [
        await _call("tpl_stops_by_route", {"route": uri})
        for uri in _route_uris(routes_payload)[:STOPS_ROUTES_PROBED]
    ]
    if intent == "tpl_stops":
        return {"tool_results": results, "unsupported": False}

    for payload in stop_payloads:  # tpl_timeline
        match = _match_stop(_stop_entries(payload), slots.get("stop_text") or "")
        if match:
            await _call("tpl_stop_timeline", {"stop": match["uri"]})
            break
    return {"tool_results": results, "unsupported": False}


def slim_tpl_result(name: str, result: Any) -> Any:
    """Compact LLM view of a tpl payload (L12) — counts + capped items, never a
    full GeoJSON or route WKT. Full fidelity stays in the tool_results audit."""
    if isinstance(result, dict) and "error" in result:
        return result
    keep = TPL_LLM_KEEPS.get(name, 20)
    if name == "tpl_stops_by_route":
        entries = _stop_entries(result)
        return {"count": len(entries), "stops": [e.get("name") or e["uri"] for e in entries[:keep]]}
    items = _generic_list(result)
    if name == "tpl_routes_by_line":
        slimmed = [
            {k: v for k, v in it.items() if k not in ("wkt", "wktGeometry", "polyline", "geometry")}
            if isinstance(it, dict) else it
            for it in items[:keep]
        ]
        return {"count": len(items), "routes": slimmed}
    key = {"tpl_agencies": "agencies", "tpl_lines": "lines", "tpl_stop_timeline": "events"}[name]
    return {"count": len(items), key: items[:keep]}


def _last_ok_result(results: list[dict[str, Any]], name: str) -> Any:
    for e in reversed(results):
        r = e.get("result")
        if e.get("name") == name and not (isinstance(r, dict) and "error" in r):
            return r
    return None


def extract_tpl_data(intent: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Widget payload for a tpl intent. NEW data keys (lines/routes/stops/
    timeline) — pending referente confirmation, same status as data.legs/arcs.
    Route entries keep their WKT so the map widget can draw the line."""
    if intent == "tpl_lines":
        items = _generic_list(_last_ok_result(results, "tpl_lines"))
        return {"lines": items[:TPL_DATA_KEEP]} if items else {}
    if intent == "tpl_routes":
        items = _generic_list(_last_ok_result(results, "tpl_routes_by_line"))
        return {"routes": items[:ROUTES_DATA_KEEP]} if items else {}
    if intent == "tpl_stops":
        stops, seen = [], set()
        for e in results:
            if e.get("name") != "tpl_stops_by_route":
                continue
            for s in _stop_entries(e.get("result")):
                if s["uri"] not in seen:
                    seen.add(s["uri"])
                    stops.append(s)
        return {"stops": stops[:TPL_DATA_KEEP]} if stops else {}
    if intent == "tpl_timeline":
        items = _generic_list(_last_ok_result(results, "tpl_stop_timeline"))
        return {"timeline": items[:TPL_DATA_KEEP]} if items else {}
    return {}


def _names(items: list[Any], keys: tuple[str, ...]) -> list[str]:
    out = []
    for it in items:
        if isinstance(it, dict):
            label = _first_str(it, keys)
            if label:
                out.append(label)
        elif isinstance(it, str) and it.strip():
            out.append(it)
    return out


def tpl_template_answer(intent: str, data: dict[str, Any]) -> str | None:
    """Deterministic Italian fallback when the respond LLM is unavailable
    (mirror of orchestrator._template_answer for the route intent)."""
    if intent == "tpl_lines" and data.get("lines"):
        names = _names(data["lines"], ("shortName", "short_name", "lineNumber", "name", "uri"))
        return f"Linee disponibili: {', '.join(names[:15])} ({len(data['lines'])} mostrate)."
    if intent == "tpl_routes" and data.get("routes"):
        names = _names(data["routes"], ("direction", "name", "routeUri", "uri"))
        return f"Percorsi trovati: {', '.join(names[:6])} ({len(data['routes'])} mostrati)."
    if intent == "tpl_stops" and data.get("stops"):
        names = _names(data["stops"], ("name", "uri"))
        return f"Fermate: {', '.join(names[:15])} ({len(data['stops'])} mostrate)."
    if intent == "tpl_timeline" and data.get("timeline"):
        return f"Trovati {len(data['timeline'])} passaggi programmati alla fermata."
    return None
