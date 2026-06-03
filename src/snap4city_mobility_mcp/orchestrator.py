"""Langgraph agentic mobility advisor — orchestration layer.

A natural-language query drives a single agentic graph:

  understand → agent ⇄ tools → format → END

- `understand` (LLM, forced tool call): extracts {origin_text, destination_text,
  mode, intent} from the latest user turn (place TEXT only — coordinates come from
  a tool). Resolves follow-ups ("那坐公交呢?") against the conversation.
- `agent` (LLM, tool_choice=auto): decides which MCP tool to call next, or writes
  the final answer. `tools` executes each call deterministically and feeds the
  result back; the pair loops until the model stops calling tools (or MAX_STEPS).
- `format` assembles widget JSON for the Snap4City dashboard, including the FULL
  route WKT for the map widget and the updated `messages` for multi-turn.

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
from snap4city_mobility_mcp.mcp_tools import _build_config, exec_tool, fetch_tool_schemas

# Loop ceiling: longest legit chain is TPL discovery (agencies→lines→routes→stops→
# timeline = 5) or a route (2× geocode + routing = 3, +retry/reverse). 8 = headroom.
MAX_STEPS = 8

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

AGENT_SYSTEM = """\
You are a Florence (Tuscany, Italy) public-mobility advisor. You answer trip and \
public-transport questions by CALLING TOOLS, then summarizing the results for the \
user in their language.
Hard rules:
1. NEVER invent coordinates, addresses, service URIs, line names, or route IDs. \
Every coordinate comes from `address_search_location`. Every transport URI comes \
from a tpl_* tool.
2. To route between two places: call `address_search_location` for the origin, then \
for the destination, read lat/lng from each result's first feature (GeoJSON order \
is [longitude, latitude]), then call `routing` with those four numbers and the \
requested routetype. Do not call `routing` until you have both coordinate pairs \
from tool results.
3. Transport-discovery chain — always follow URIs returned by the previous tool, \
never guess them: `tpl_agencies` → pick an agency URI → `tpl_lines(agency)` → pick \
a line URI → `tpl_routes_by_line(line)` → pick a route URI → \
`tpl_stops_by_route(route)` → pick a stop URI → `tpl_stop_timeline(stop)`.
4. If a tool returns an error or an empty result, do not retry it blindly. Adjust \
the arguments (e.g. a more specific Tuscany address) or tell the user plainly what \
failed (e.g. "no car route — Piazza del Duomo is a pedestrian zone; try a walking \
route or public transport").
5. Travel modes: car, public_transport, foot_quiet, foot_shortest. There is NO \
bicycle mode.
6. When you have enough information, STOP calling tools and write a concise final \
answer. Give distance (km) and ETA for routes; list line / route / stop names for \
transport queries."""

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
    messages: list[dict[str, Any]]  # full OpenAI chat history (system+user+assistant+tool)
    intent: str  # from understand: route | tpl_lines | tpl_routes | tpl_stops | tpl_timeline | other
    slots: dict[str, Any]  # understand output: {intent, origin_text, destination_text, mode}
    tool_results: list[dict[str, Any]]  # structured audit: [{name, args, result}] per call
    steps: int  # agent⇄tools loop counter (MAX_STEPS guard)
    final: dict[str, Any]  # widget JSON assembled by format node


async def understand(state: AdvisorState, *, llm: Llama4Client) -> dict[str, Any]:
    """LLM extracts slots from the latest user turn via a forced tool call."""
    history = state["messages"]
    convo = [m for m in history if m.get("role") in ("user", "assistant", "tool")]
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
        pass  # fall back to {"intent": "other"} — the agent figures it out from history

    # Ensure a single static agent system message at the front of the conversation.
    messages = list(history)
    if not (messages and messages[0].get("role") == "system"):
        messages = [{"role": "system", "content": AGENT_SYSTEM}, *messages]
    return {"messages": messages, "slots": slots, "intent": slots.get("intent", "other"), "steps": 0}


async def agent(
    state: AdvisorState, *, llm: Llama4Client, tool_schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    """LLM picks the next tool call, or writes the final answer at the step ceiling."""
    steps = state.get("steps", 0)
    messages = state["messages"]
    choice = "none" if steps >= MAX_STEPS else "auto"
    resp = await llm.achat(messages=messages, tools=tool_schemas, tool_choice=choice, temperature=0)
    return {"messages": [*messages, assistant_message(resp)], "steps": steps + 1}


async def tools(state: AdvisorState, *, client: Client) -> dict[str, Any]:
    """Execute every tool_call on the last assistant message; feed results back."""
    messages = list(state["messages"])
    results = list(state.get("tool_results", []))
    last = messages[-1] if messages else {}
    for tc in last.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name")
        raw = fn.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError as e:
            result: Any = {"error": f"invalid tool arguments JSON: {e}"}
        else:
            result = await exec_tool(client, name, args)
        messages.append(
            {"role": "tool", "tool_call_id": tc.get("id"), "content": json.dumps(result, ensure_ascii=False)}
        )
        results.append({"name": name, "args": raw, "result": result})
    return {"messages": messages, "tool_results": results}


def _last_assistant_text(messages: list[dict[str, Any]]) -> str | None:
    """Most recent assistant message with non-empty string content."""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return None


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
                    "arcs": first.get("arc"),
                    "source_node": journey.get("source_node"),
                    "destination_node": journey.get("destination_node"),
                }
            if is_err and route_error is None:
                route_error = result["error"]
            continue
        if is_err:
            continue
        if name == "tpl_stop_timeline":
            return {"timeline": result}
        if name == "tpl_stops_by_route":
            return {"stops": result}
        if name == "tpl_routes_by_line":
            return {"routes": result}
        if name == "tpl_lines":
            return {"lines": result}
        if name == "tpl_agencies":
            return {"agencies": result}
    return {"route_error": route_error} if route_error else {}


def format_widget(state: AdvisorState) -> dict[str, Any]:
    """Assemble the dashboard widget JSON from the final answer + tool results."""
    messages = state.get("messages", [])
    return {
        "final": {
            "ok": True,
            "intent": state.get("intent", "other"),
            "answer": _last_assistant_text(messages),
            "data": _extract_data(state.get("tool_results", [])),
            "messages": messages,  # updated history for multi-turn
        }
    }


def _route_after_agent(state: AdvisorState) -> str:
    """Continue to tools while the model keeps calling them and the ceiling holds."""
    if state.get("steps", 0) > MAX_STEPS:
        return "format"
    last = state["messages"][-1] if state.get("messages") else {}
    return "tools" if last.get("tool_calls") else "format"


def _build_graph(client: Client, llm: Llama4Client, tool_schemas: list[dict[str, Any]]):
    g = StateGraph(AdvisorState)
    g.add_node("understand", partial(understand, llm=llm))
    g.add_node("agent", partial(agent, llm=llm, tool_schemas=tool_schemas))
    g.add_node("tools", partial(tools, client=client))
    g.add_node("format", format_widget)
    g.set_entry_point("understand")
    g.add_edge("understand", "agent")
    g.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", "format": "format"})
    g.add_edge("tools", "agent")
    g.add_edge("format", END)
    return g.compile()


async def run_advisor(query: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Multi-turn agentic advisor. Returns widget JSON including updated `messages`.

    Pass the previous turn's `final["messages"]` back as `history` to continue the
    conversation (the CLI REPL and the dashboard both carry state this way).
    """
    cfg = await _build_config()
    llm = Llama4Client()
    messages = list(history or [])
    messages.append({"role": "user", "content": query})
    async with Client(cfg) as client:
        tool_schemas = await fetch_tool_schemas(client)  # schemas come from the server
        graph = _build_graph(client, llm, tool_schemas)
        out: AdvisorState = await graph.ainvoke(
            {"messages": messages, "tool_results": [], "steps": 0}
        )
    return out.get("final", {"ok": False, "error": "no final state produced", "messages": messages})
