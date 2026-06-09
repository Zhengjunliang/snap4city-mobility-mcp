"""Langgraph mobility advisor — deterministic orchestration layer.

A natural-language query drives a single linear graph:

  understand → execute → respond → END

- `understand` (LLM, forced tool call): extracts {intent, origin_text,
  destination_text, mode} from the latest user turn (place TEXT only — coordinates
  come from a tool). Resolves follow-ups ("那坐公交呢?") against the conversation.
  A forced `tool_choice` guarantees structured output, so this stage is reliable.
- `execute` (pure Python, NO LLM): for a `route` intent it deterministically runs
  the fixed tool flow — geocode origin, geocode destination, then `routing` with
  the requested mode — instead of letting the model free-call tools. Other intents
  (tpl_* discovery) are not handled deterministically yet and fall through to a
  friendly "unsupported" reply.
- `respond` (LLM, tool_choice="none" — NO tools): phrases a concise, multilingual
  answer from the structured results, then assembles the widget JSON (incl. the
  FULL route WKT and the updated `messages` for multi-turn).

Why deterministic: Llama4 with `tool_choice="auto"` is unreliable — when it wants
to narrate ("retrying with a different profile…") it emits tool calls as pythonic
TEXT instead of structured `tool_calls`, which leak into the final answer (see
lesson L13). Letting the model pick *slots* (forced) and *prose* (none), while
Python drives the tools, removes that failure mode entirely.

Deterministic MCP execution + km4city quirk handling live in mcp_tools.py; the
Llama4 client in llm.py. Runs end-to-end only on the Snap4City JupyterHub.
"""
import json
from functools import partial
from typing import Any, TypedDict

from fastmcp import Client
from langgraph.graph import END, StateGraph

from snap4city_mobility_mcp.llm import (
    Llama4Client,
    Llama4Error,
    assistant_message,
    tool_calls,
)
from snap4city_mobility_mcp.mcp_tools import (
    _build_config,
    exec_tool,
    fetch_tool_schemas,
    slim_result_for_llm,
)

UNDERSTAND_SYSTEM = """\
You are the intent-extraction stage of a Florence (Tuscany, Italy) public-mobility \
advisor. Read the conversation and the user's LATEST message, then call \
`extract_slots` exactly once.
Rules:
- Extract PLACE TEXT only (e.g. "Piazza del Duomo, Firenze"). NEVER output \
coordinates — a separate tool geocodes places.
- For a follow-up that omits a place (e.g. "what about by bus?", "那坐公交呢?"), \
reuse the origin/destination from earlier in the conversation and change only what \
the user changed (here: mode → public_transport).
- Map travel mode: walk / on foot → foot_shortest (quiet or scenic walk → \
foot_quiet); drive / car → car; bus / tram / public transport / 公交 → \
public_transport.
- Pick intent: a point-to-point trip → "route"; "which lines does agency X run" → \
"tpl_lines"; "routes of line N" → "tpl_routes"; "stops on a route" → "tpl_stops"; \
"timetable at a stop" → "tpl_timeline"; anything else → "other".
- The service area is Tuscany only; do not invent places outside it."""

RESPOND_SYSTEM = """\
You are a Florence (Tuscany, Italy) public-mobility advisor. Write a concise final \
answer for the user, in the user's own language, based ONLY on the RESULTS given to \
you. Do not call any tools. Do not invent coordinates, distances, line names, or \
route IDs.
- For a route: state the distance in km and the ETA (and walking/driving time if \
present). You may mention a few main streets if listed.
- If RESULTS holds an error, explain it plainly and suggest an alternative (e.g. \
"no car route — Piazza del Duomo is a pedestrian zone; try a walking route or \
public transport").
- If the request is unsupported, say you currently answer point-to-point trip \
questions (foot, car, or public transport), e.g. "from Piazza Duomo to Santa Croce \
on foot", and invite the user to rephrase."""

# Synthetic function (not an MCP tool) — forces structured output from `understand`.
_EXTRACT_SLOTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "extract_slots",
        "description": "Record the structured interpretation of the user's latest mobility request.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["route", "tpl_lines", "tpl_routes", "tpl_stops", "tpl_timeline", "other"],
                    "description": "What the user wants.",
                },
                "origin_text": {
                    "type": "string",
                    "description": "Free-text origin place name, '' if absent.",
                },
                "destination_text": {
                    "type": "string",
                    "description": "Free-text destination place name, '' if absent.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["car", "public_transport", "foot_quiet", "foot_shortest"],
                    "description": "Travel mode; default foot_shortest for walking, public_transport for bus/tram.",
                },
            },
            "required": ["intent"],
        },
    },
}


class AdvisorState(TypedDict, total=False):
    messages: list[dict[str, Any]]  # chat history (system + user + assistant) for multi-turn
    intent: str  # from understand: route | tpl_lines | tpl_routes | tpl_stops | tpl_timeline | other
    slots: dict[str, Any]  # understand output: {intent, origin_text, destination_text, mode}
    tool_results: list[dict[str, Any]]  # structured audit: [{name, args, result}] per call
    unsupported: bool  # True when execute could not run a deterministic flow (tpl_* / missing places)
    final: dict[str, Any]  # widget JSON assembled by respond node


async def understand(state: AdvisorState, *, llm: Llama4Client) -> dict[str, Any]:
    """LLM extracts slots from the latest user turn via a forced tool call.

    A forced `tool_choice` makes the gateway return structured `tool_calls`, so this
    stage never falls back to the pythonic-text shape that plagues free tool use.
    """
    history = state["messages"]
    convo = [m for m in history if m.get("role") in ("user", "assistant")]
    slots: dict[str, Any] = {"intent": "other"}
    try:
        resp = await llm.achat(
            messages=[{"role": "system", "content": UNDERSTAND_SYSTEM}, *convo],
            tools=[_EXTRACT_SLOTS_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "extract_slots"}},
            temperature=0,
        )
        calls = tool_calls(resp)
        if calls:
            raw = (calls[0].get("function") or {}).get("arguments")
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict) and parsed.get("intent"):
                slots = parsed
    except (json.JSONDecodeError, Llama4Error):
        pass  # fall back to {"intent": "other"} — execute treats it as unsupported
    return {"slots": slots, "intent": slots.get("intent", "other")}


def _first_coord(geocode: Any) -> list[float] | None:
    """First feature's `[lng, lat]` from an `address_search_location` result, or None.

    `exec_tool` already pins the result to Tuscany and returns `{"error": ...}` when
    nothing matches, so a non-FeatureCollection / empty payload yields None here.
    """
    if not isinstance(geocode, dict) or "error" in geocode:
        return None
    features = geocode.get("features")
    if not (isinstance(features, list) and features):
        return None
    coords = (features[0].get("geometry") or {}).get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lng, lat = coords[0], coords[1]
        if isinstance(lng, (int, float)) and isinstance(lat, (int, float)):
            return [float(lng), float(lat)]
    return None


# Walking modes share a deterministic fallback: a foot_quiet route that comes back
# empty (km4city L3 cold-start stale) is retried as foot_shortest — the same recovery
# the model used to attempt by hand, now driven by Python so it always runs.
_FOOT_FALLBACK = {"foot_quiet": "foot_shortest"}


async def execute(state: AdvisorState, *, client: Client) -> dict[str, Any]:
    """Deterministically run the tool flow for the extracted intent (NO LLM).

    Only `route` is handled today: geocode both endpoints, then `routing` with the
    requested mode (+ a foot_quiet→foot_shortest retry). Every call is recorded in
    `tool_results` so `_extract_data` can build the widget. Anything else (tpl_*,
    missing places) sets `unsupported` for the respond node.
    """
    slots = state.get("slots") or {}
    results: list[dict[str, Any]] = []

    if slots.get("intent") != "route":
        return {"tool_results": results, "unsupported": True}

    origin_text = (slots.get("origin_text") or "").strip()
    dest_text = (slots.get("destination_text") or "").strip()
    if not (origin_text and dest_text):
        return {"tool_results": results, "unsupported": True}

    async def _geocode(search: str) -> list[float] | None:
        args = {"search": search}
        result = await exec_tool(client, "address_search_location", args)
        results.append({"name": "address_search_location", "args": json.dumps(args), "result": result})
        return _first_coord(result)

    origin = await _geocode(origin_text)
    dest = await _geocode(dest_text)
    if origin is None or dest is None:
        return {"tool_results": results, "unsupported": False}  # geocode error → respond explains

    mode = slots.get("mode") or "foot_shortest"

    async def _route(routetype: str) -> dict[str, Any]:
        # GeoJSON coordinate order is [longitude, latitude].
        args = {
            "startlatitude": origin[1],
            "startlongitude": origin[0],
            "endlatitude": dest[1],
            "endlongitude": dest[0],
            "routetype": routetype,
        }
        result = await exec_tool(client, "routing", args)
        results.append({"name": "routing", "args": json.dumps(args), "result": result})
        return result

    routed = await _route(mode)
    if isinstance(routed, dict) and "error" in routed and mode in _FOOT_FALLBACK:
        await _route(_FOOT_FALLBACK[mode])  # deterministic walking-profile fallback

    return {"tool_results": results, "unsupported": False}


def _extract_data(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Mine the tool-result audit for the widget payload (last successful tool wins)."""
    route_error: str | None = None
    for entry in reversed(results):
        name = entry.get("name")
        result = entry.get("result")
        if not isinstance(result, dict) and not isinstance(result, list):
            continue
        is_err = isinstance(result, dict) and "error" in result

        if name == "routing":
            if isinstance(result, dict) and isinstance(result.get("journey"), dict):
                journey = result["journey"]
                first = (journey.get("routes") or [{}])[0]
                return {
                    "wkt": first.get("wkt"),  # FULL LINESTRING — not truncated
                    "distance_km": first.get("distance"),
                    "eta": first.get("eta"),
                    "duration": first.get("time"),
                    # "arcs": first.get("arc"),  # per-segment detail hidden — bloats payload
                    # ~90%; re-enable once referente confirms the dashboard widget needs it.
                    "source_node": journey.get("source_node"),
                    "destination_node": journey.get("destination_node"),
                }
            if is_err and route_error is None:
                route_error = result["error"]
            continue
        if is_err:
            continue
    return {"route_error": route_error} if route_error else {}


def _template_answer(intent: str, data: dict[str, Any], *, unsupported: bool) -> str:
    """Deterministic fallback answer when the respond LLM is unavailable."""
    if unsupported:
        return (
            "I currently answer point-to-point trip questions (foot, car, or public "
            "transport), e.g. 'from Piazza Duomo to Santa Croce on foot'."
        )
    if data.get("distance_km") is not None:
        bits = [f"{data['distance_km']} km"]
        if data.get("duration"):
            bits.append(f"~{data['duration']}")
        if data.get("eta"):
            bits.append(f"arrivo {data['eta']}")
        return "📍 " + " · ".join(bits)
    if data.get("route_error"):
        return f"⚠ {data['route_error']}"
    return "Sorry, I couldn't find a route for that request."


def _results_view(results: list[dict[str, Any]], *, unsupported: bool) -> dict[str, Any]:
    """Compact, LLM-facing summary of what execute produced (slim — no huge WKT)."""
    if unsupported:
        return {"status": "unsupported", "supported": "point-to-point trips (foot, car, public transport)"}
    view = [
        {"name": e.get("name"), "result": slim_result_for_llm(e.get("name"), e.get("result"))}
        for e in results
    ]
    return {"status": "ok", "results": view}


async def respond(state: AdvisorState, *, llm: Llama4Client) -> dict[str, Any]:
    """LLM phrases a multilingual answer from the structured results (NO tools),
    then assembles the widget JSON. Falls back to a template if the LLM errors."""
    messages = list(state.get("messages") or [])
    intent = state.get("intent", "other")
    results = state.get("tool_results") or []
    unsupported = bool(state.get("unsupported"))
    data = _extract_data(results)

    user_query = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"), ""
    )
    view = _results_view(results, unsupported=unsupported)
    answer: str | None = None
    try:
        resp = await llm.achat(
            messages=[
                {"role": "system", "content": RESPOND_SYSTEM},
                {
                    "role": "user",
                    "content": f"User asked: {user_query}\n\nRESULTS:\n"
                    + json.dumps(view, ensure_ascii=False),
                },
            ],
            tool_choice="none",
            temperature=0,
        )
        content = assistant_message(resp).get("content")
        if isinstance(content, str) and content.strip():
            answer = content.strip()
    except Llama4Error:
        pass  # fall back to the deterministic template below
    if answer is None:
        answer = _template_answer(intent, data, unsupported=unsupported)

    messages.append({"role": "assistant", "content": answer})
    # Widget JSON: the reply lives in `messages[-1].content` (OpenAI-standard) — no
    # custom top-level `answer` field. `data` is the route payload; `messages` is the
    # multi-turn history to pass back.
    return {
        "final": {
            "ok": True,
            "intent": intent,
            "data": data,
            "messages": messages,  # updated history for multi-turn (last turn = the reply)
        }
    }


def _build_graph(client: Client, llm: Llama4Client):
    g = StateGraph(AdvisorState)
    g.add_node("understand", partial(understand, llm=llm))
    g.add_node("execute", partial(execute, client=client))
    g.add_node("respond", partial(respond, llm=llm))
    g.set_entry_point("understand")
    g.add_edge("understand", "execute")
    g.add_edge("execute", "respond")
    g.add_edge("respond", END)
    return g.compile()


async def run_advisor(query: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Multi-turn mobility advisor. Returns widget JSON including updated `messages`.

    Pass the previous turn's `final["messages"]` back as `history` to continue the
    conversation (the CLI REPL and the dashboard both carry state this way).
    """
    cfg = await _build_config()
    llm = Llama4Client()
    messages = list(history or [])
    messages.append({"role": "user", "content": query})
    async with Client(cfg) as client:
        await fetch_tool_schemas(client)  # validate connectivity / tool availability
        graph = _build_graph(client, llm)
        out: AdvisorState = await graph.ainvoke({"messages": messages, "tool_results": []})
    return out.get("final", {"ok": False, "error": "no final state produced", "messages": messages})
