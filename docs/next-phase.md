# Next Phase Tracker — Snap4City Mobility Advisor MCP

> **Purpose**: starting context for a **new Claude Code conversation**. Self-contained with `README.md` + `CLAUDE.md` + `docs/lessons.md`.

## TL;DR

UNIFI Sistemi Distribuiti **elaborato Tipo A**: a **FastMCP client + Langgraph deterministic orchestrator + Chainlit test chat UI** that answers Florence/Tuscany trip questions by driving DISIT's Snap4City **Llama4** LLM over the remote `snap4agentic_advisor_native` MCP server. The MCP server is referente-managed; this project ships only the client. Runtime = **Snap4City JupyterHub** (LLM + intranet MCP reachable only there).

## Architecture (current)

Linear **deterministic** graph (no agentic loop), multi-turn conversation:

```
query → understand → execute → respond → JSON
```

- **understand** (LLM, forced `extract_slots` call): pulls `{origin_text, destination_text, mode, intent}` from the latest turn (place TEXT only — coords come from a tool); resolves follow-ups ("那坐公交呢?") against history. A forced `tool_choice` guarantees structured output — reliable.
- **execute** (pure Python, NO LLM): for a `route` intent, deterministically runs the fixed flow — geocode origin, geocode destination, then `routing(mode)` (+ a foot_quiet→foot_shortest fallback). The LLM never free-calls tools. Other intents (tpl_*) are not handled yet → `unsupported`.
- **respond** (LLM `tool_choice="none"`, NO tools): phrases a concise multilingual reply from the structured results, appends it as the last `assistant` turn, then assembles widget JSON `{ok, intent, data, messages}` — the reply is `messages[-1].content` (OpenAI-standard, no custom `answer` field); `data` holds the route WKT for the map; `messages` is the multi-turn history. Falls back to a template if the LLM errors.

**Why deterministic** (lesson L13): Llama4 with `tool_choice="auto"` is unreliable — when it narrates it emits tool calls as pythonic TEXT that leak into the answer. Letting the model pick *slots* (forced) and *prose* (none) while Python drives the tools removes that failure mode. The old `agent`/`tools` nodes + `recover_pythonic_tool_calls` are deleted.

Modules:
- `src/snap4city_mobility_mcp/mcp_tools.py` — client layer: Client config, `fetch_tool_schemas` (the 7 exposed schemas pulled from the server's own `list_tools()`), `routing_with_retry` (km4city quirk handling L2/L3/L7/L8), `exec_tool` (forwards calls to the remote tools).
- `src/snap4city_mobility_mcp/orchestrator.py` — the graph: `AdvisorState`, prompts, `understand`/`execute`/`respond`, `run_advisor`.
- `chainlit_app.py` (repo root) — Chainlit multi-turn chat UI for testing; UI shows the LLM reply, full output JSON appended to `outputs.txt` per turn.
- `src/snap4city_mobility_mcp/llm.py` — `Llama4Client` (OpenAI-compatible agentic endpoint).

Core tools the client uses: `address_search_location`, `routing` (route flow today); `tpl_agencies`/`tpl_lines`/`tpl_routes_by_line`/`tpl_stops_by_route`/`tpl_stop_timeline` still fetched as schemas but the tpl_* flow is not wired yet. (Real signatures: `docs/snap4city-api-notes.md §3`.)

## Done
- Remote referente MCP server connected; transport = HTTP Streamable, intranet-direct from JupyterHub.
- Llama4 LLM client (`llm.py`) — endpoint `llama4-agentic-inference`.
- **Deterministic orchestrator** (`understand → execute → respond`) + **Chainlit multi-turn test chat UI** (`chainlit_app.py`; full output JSON → `outputs.txt`); local mock unit tests (`tests/`, no LLM/MCP needed) green.
- **JupyterHub end-to-end (route)**: foot route happy-path verified (clean multilingual answer + full WKT, no pythonic leak); car/public_transport return graceful "routing failed" messages (server-side empty-body — see below).

## Next
1. **Classify the car / public_transport routing failures** (server-side): re-run car a few times (transient L3 vs stable L8 ZTL) and test car/PT outside the ZTL; raise with referente if the profile is unsupported.
2. **Dashboard widget wiring** — chat UI → `run_advisor`; map widget renders `data.wkt` LINESTRING. Widget URL pattern: confirm with referente.
3. **tpl_* flow** — deterministic discovery chain (agency→line→route→stop→timeline), currently returns an "unsupported" reply.
4. **Final report + ZIP** (disit.org/5986) — code + report + screenshots.

## Open questions
1. Does the referente server require an auth token for any tool? (none seen on the core tools so far)
2. car / public_transport `routing` returns empty body — is it transient (L3), the stable car-ZTL wrapper bug (L8), or is `public_transport` routetype unsupported server-side?
3. Does the dashboard widget consume `data.arcs` (per-segment detail)? Currently commented out in `_extract_data` to slim the payload ~90%; re-enable if needed.
4. Dashboard widget URL pattern for embedding rendered routes?
5. Report language: it / en / zh?
