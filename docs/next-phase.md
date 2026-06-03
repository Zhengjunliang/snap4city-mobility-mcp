# Next Phase Tracker — Snap4City Mobility Advisor MCP

> **Purpose**: starting context for a **new Claude Code conversation**. Self-contained with `README.md` + `CLAUDE.md` + `docs/lessons.md`.

## TL;DR

UNIFI Sistemi Distribuiti **elaborato Tipo A**: a **FastMCP client + Langgraph agentic orchestrator + CLI glue** that answers Florence/Tuscany trip & public-transport questions by driving DISIT's Snap4City **Llama4** agentic LLM over the remote `snap4agentic_advisor_native` MCP server. The MCP server is referente-managed; this project ships only the client. Runtime = **Snap4City JupyterHub** (LLM + intranet MCP reachable only there).

## Architecture (current)

Single agentic graph, multi-turn conversation:

```
query → understand → agent ⇄ tools → format → JSON
```

- **understand** (LLM, forced `extract_slots` call): pulls `{origin_text, destination_text, mode, intent}` from the latest turn (place TEXT only — coords come from a tool); resolves follow-ups ("那坐公交呢?") against history.
- **agent ⇄ tools** (LLM `tool_choice=auto` ↔ deterministic MCP exec): the model picks the next tool; `tools` runs it and feeds the result back; loops until a final answer or `MAX_STEPS`.
- **format**: widget JSON `{ok, intent, answer, data, messages}` — full route WKT for the map, lists for TPL, `messages` for multi-turn.

Modules:
- `src/snap4city_mobility_mcp/mcp_tools.py` — client layer: Client config, `fetch_tool_schemas` (the 7 exposed schemas pulled from the server's own `list_tools()`), `routing_with_retry` (km4city quirk handling L2/L3/L7/L8), `exec_tool` (forwards calls to the remote tools).
- `src/snap4city_mobility_mcp/orchestrator.py` — the graph: `AdvisorState`, prompts, `understand`/`agent`/`tools`/`format_widget`, `run_advisor`.
- `src/snap4city_mobility_mcp/cli.py` — multi-turn REPL + one-shot.
- `src/snap4city_mobility_mcp/llm.py` — `Llama4Client` (OpenAI-compatible agentic endpoint).

Core 7 tools exposed to the LLM: `address_search_location`, `routing`, `tpl_agencies`, `tpl_lines`, `tpl_routes_by_line`, `tpl_stops_by_route`, `tpl_stop_timeline`. (Real signatures: `docs/snap4city-api-notes.md §3`.)

## Done
- Remote referente MCP server connected; transport = HTTP Streamable, intranet-direct from JupyterHub.
- Llama4 agentic LLM client (`llm.py`) — endpoint `llama4-agentic-inference`, native tool calling.
- Agentic orchestrator + REPL CLI; local mock unit tests (`tests/`, no LLM/MCP needed) green.

## Next
1. **JupyterHub end-to-end smoke** (see README §8): `git clone` → conda 3.11 env → `pip install -e .` → `snap4city-mobility-cli "..."`. Validate route happy-path (≈0.68 km, full WKT), a multi-turn follow-up, a TPL chain, and car-ZTL graceful failure (L8).
2. **Dashboard widget wiring** — chat UI → `run_advisor`; map widget renders `data.wkt` LINESTRING. Widget URL pattern: confirm with referente.
3. **Final report + ZIP** (disit.org/5986) — code + report + screenshots.

## Open questions
1. Does the referente server require an auth token for any tool? (none seen on the core 7 so far)
2. Is the native server endpoint path long-term stable? (confirm with referente)
3. Dashboard widget URL pattern for embedding rendered routes?
4. Report language: it / en / zh?
