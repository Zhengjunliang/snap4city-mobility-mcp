# snap4city-mobility-mcp

**Langgraph MCP client** for referente's remote Snap4City mobility advisor server. UNIFI — *Sistemi Distribuiti, elaborato Tipo A*.

User asks a trip question → a Langgraph **deterministic** graph (`understand → execute → respond`) resolves it: the Snap4City **Llama4** LLM only extracts the request slots (origin/destination/mode) and phrases the final answer, while Python deterministically drives the MCP tools — geocoding and routing (all modes) on a **local MCP server** (`mcp_server.py`, wrapping the public km4city ServiceMap and the Snap4City What-If GraphHopper router), reverse geocoding / nearest-service search / live parking on the remote server — the LLM never free-calls tools. Returns widget JSON to be rendered by a Snap4City dashboard widget. The remote MCP server is referente-managed and deployed on the intranet (reached directly from the Snap4City JupyterHub); this project ships the **client + Langgraph orchestrator + a FastAPI bridge (`api.py`) + dashboard chat-box front-end (`frontend/`) + the local MCP server**.

---

## 1. Status

Working end-to-end on the **Snap4City JupyterHub** (browser login, intranet-direct MCP, no VPN/SSH tunnel). The remote referente MCP server is connected over HTTP Streamable, and the **Llama4 agentic LLM client** (`src/snap4city_mobility_mcp/llm.py`, endpoint `llama4-agentic-inference`) is live there.

The deterministic advisor answers **point-to-point trip questions** — on foot, by car, or by public transport — with GPS-aware endpoint resolution:

- **Named places** geocode worldwide (no region lock); a city the user names always wins, and with the browser GPS available the candidate **nearest to the user** wins among equals. Usable km4city data is Tuscany-only, so test with Tuscan places.
- **Missing origin** ("portami al Duomo") defaults to the **user's GPS position** (reverse-geocoded once so the reply can say *"dalla tua posizione"*); without GPS the advisor asks for the starting point.
- **Generic-category destinations** ("la farmacia più vicina") resolve via the remote `service_search_near_gps_position` tool — the nearest service of that km4city category around the user (or around the named city without GPS).

The remote `routing` tool is retired on the client side: **all routing** (foot / car / public transport) goes through the local `route` tool, which wraps the Snap4City What-If GraphHopper router (`docs/lessons.md` L46 — the remote tool's `public_transport` never returned transit, L19, and its km4city backend needed a whole retry ladder, L3/L8). The remote server still provides reverse geocoding, the nearest-service search and live parking data.

---

## 2. Setup

Needs **Python ≥ 3.10** (`.python-version` pins 3.10) and [`uv`](https://github.com/astral-sh/uv) (`pip install uv`):

```powershell
git clone <repo-url>
cd snap4city-mobility-mcp
uv sync          # creates .venv/ and installs the lockfile's exact versions
uv run pytest -q # local mock tests: no LLM / MCP needed, runs anywhere
```

`uv run <cmd>` always uses the project's `.venv/` — activating it (`.venv\Scripts\Activate.ps1`, or `source .venv/bin/activate`) only saves you the prefix.

On the **JupyterHub** (the only place the advisor really runs, see §3) `uv` is usually absent: create a conda **Python 3.11** env (`s4c`) — the default kernel 3.9 is too old for fastmcp — and `pip install -e .` (`CLAUDE.md` §5.1, `docs/lessons.md` L9/L15).

The **Llama4 LLM** answers **only from the JupyterHub**: put the function-account credentials there in a `user_credentials.json` (`{"username": "...", "password": "..."}`) — it is `.gitignore`d, so upload it manually to the repo root. The client searches `S4C_CREDENTIALS_FILE` → working dir → repo root.

---

## 3. Run it (JupyterHub)

The remote MCP server lives on the intranet and is reached directly from the JupyterHub (no VPN/SSH tunnel); the orchestrator defaults to `http://192.168.1.117:8000` (override with `S4C_DASHBOARD_URL`). Log in: snap4city.org → *Strumenti di sviluppo* → *Jupyter Hub - Python*, then check the dashboard is up:

```bash
curl -s http://192.168.1.117:8000/apps.json | python -m json.tool | head
# JSON with mcpServers listing snap4agentic_advisor_native / _legacy / _experimental
```

**Two processes**, each in its own JupyterHub terminal inside the `s4c` env, the local MCP server first. Stop them with `Ctrl-C`, never by closing the tab — a closed tab can leave the port held (`Address already in use`; check with `ss -ltn | grep -E ':8020|:8010'`).

```bash
python -m snap4city_mobility_mcp.mcp_server   # terminal 1 — :8020
uvicorn api:app --host 0.0.0.0 --port 8010    # terminal 2 — :8010
```

- **Terminal 1, local MCP server** (`:8020`): forward geocoding (wrapping the public km4city ServiceMap — referente's remote `address_search_location` is server-side broken, `docs/lessons.md` L28/L29) and the `route` tool for all modes (wrapping the What-If GraphHopper router, L46). It only needs outbound HTTP. The client reaches it via `S4C_LOCAL_MCP_URL` (default `http://127.0.0.1:8020/mcp`).
- **Terminal 2, advisor bridge** (`:8010`): drives the LLM and both MCP servers. The browser reaches it same-origin through `jupyter-server-proxy` (setup recipe in `docs/lessons.md` L27, wiring in `frontend/README.md`).

The front end is a natural-language **chat box** on the Snap4City dashboard (`frontend/mobility_advisor_dashboard.html`, a `widgetExternalContent`) talking to the bridge, with the route drawn on a sibling `widgetMap`.

### The bridge protocol: job + poll

`POST /advise` **starts** the turn and answers `{"job_id": ...}` immediately; `GET /advise/{job_id}` returns `202` while it computes and `200` with the widget JSON once done. A public-transport turn runs ~50–70 s and the reverse proxy chain cuts any single request past ~60 s, so no HTTP request here may span a whole turn (`docs/lessons.md` L47 — heartbeat streaming does *not* work; do not collapse this back into one request). Each `202` also carries the **stage** (`understand` → `geocode` → `routing` / `routing_bus` → `respond`) and its `elapsed_s`, so the chat box says what is running instead of showing a blank "thinking" bubble. Stage and job id live in the transport layer only — they never enter the widget JSON.

Sanity-check without the dashboard:

```bash
curl -s localhost:8010/health
JOB=$(curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"da Piazza del Duomo a Santa Croce a piedi","history":[]}' \
  | python -c 'import sys, json; print(json.load(sys.stdin)["job_id"])')
until curl -s -o /tmp/turn.json -w '%{http_code}' localhost:8010/advise/$JOB | grep -q 200; do sleep 2; done
head -c 300 /tmp/turn.json    # status="success", request_type="route", data.distance_km ≈ 0.68, full data.wkt
# GPS-aware turn: the origin defaults to the position, a category resolves to the nearest service
curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"portami alla farmacia più vicina","history":[],"gps":{"lat":43.7731,"lng":11.2558}}'
```

Each turn overwrites `outputs.txt` with the full output JSON and `debug.log` with the tool-level diagnostics (both gitignored, both in the cwd) — inspect them when a turn draws no route.

### The widget payload

```json
{
  "status": "success",
  "request_type": "route",
  "data": {
    "wkt": "LINESTRING(11.255 43.773, ...)",   // FULL geometry — map widget draws this
    "distance_km": 0.679, "duration": "0:10:00", "mode": "foot"
  },
  "messages": [ ... updated conversation; LAST assistant turn = the reply text ... ]
}
```

The reply text is the **last `assistant` turn in `messages`** (OpenAI-standard) — no custom top-level `answer` field. `data` carries the full `wkt` + `distance_km` + `duration` + `mode`, plus a `routes` list (one per travel mode; a bus route also ships its walk/ride `legs` geometry for the map split). The front-end keeps `messages` and sends them back as `history` next turn, and sends the browser geolocation as `gps: {lat, lng}` (or `null`) with every turn. Out-of-scope questions (including network reference questions like line lists or timetables) return a friendly "unsupported" reply.

**Travel modes.** When the question does not name one (*"da Piazza del Duomo a Santa Croce"*), all three are routed **concurrently** — on foot, by car and by public transport — so the reply compares them and the map draws a line each (`docs/lessons.md` L31). The turn answers once, when all three are in: the wall clock is the slowest of them, today the bus one, because the online whatif-router rebuilds its public-transport graph on every `vehicle=bus` request (~30–45 s) until referente merges `whatif-local/patches/pt-router-singleton.patch`; foot and car are sub-second. Naming a mode (*"a piedi"*) routes that one only, so it never pays the bus latency. A **departure time** the user gives (*"alle 18"*, *"domani alle 9"*) becomes the public-transport timetable window; an *arrival* time is not supported (the What-If servlet has no `arrive_by`).

**Known limitation — the bus line is drawn stop to stop.** A public-transport *ride* leg comes back with one vertex per stop (measured: 8 vertices over 1.78 km, longest hop 476 m), so the drawn line cuts straight across blocks instead of following the roads. Not missing data — both GTFS feeds carry `shapes.txt` — but **GraphHopper's GTFS importer ignores it**, so a ride leg's points are just its stop coordinates. The exact shape is recoverable client-side (`trip_id` → `trips.txt` → `shape_id` → `shapes.txt`, cut between the board and alight stops); see `docs/lessons.md` L44.

### Optional — point `route` at a locally built whatif-router

`route` defaults to the **online** What-If router (`https://www.snap4city.org/whatif-router/route`), which carries the Tuscany GTFS since 2026-07-10 and returns real transit — **no third process needed**. Only to validate the perf patch (or a different GTFS set) do you need a self-built router — see [`whatif-local/README.md`](whatif-local/README.md):

```bash
export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
```

---

## 4. Project layout

```
snap4city-mobility-mcp/
├── pyproject.toml              # uv-managed project file
├── uv.lock                     # exact-version lockfile (committed)
├── api.py                      # FastAPI bridge for the dashboard chat box (job/poll: POST /advise + GET /advise/{job_id})
├── frontend/                   # Snap4City dashboard front-end (widgetExternalContent chat box + widgetMap)
├── whatif-local/               # referente whatif-router perf patch (patches/) + apply/test notes
├── docs/
│   ├── lessons.md              # architectural traps (km4city / runtime)
│   └── snap4city-api-notes.md  # field-by-field observations of the real API
├── tests/                      # local mock unit tests (no LLM / MCP needed)
└── src/
    └── snap4city_mobility_mcp/    # client package + local MCP server — the remote advisor server is referente-managed
        ├── mcp_tools.py           # client MCP layer: Client config, exec_tool, two-pass geocode, result parsers
        ├── mcp_server.py          # our local MCP server: forward geocode (public km4city ServiceMap, L29) + `route` for all modes (What-If GraphHopper, L46)
        ├── orchestrator.py        # deterministic Langgraph graph: understand → execute → respond; run_advisor
        ├── geo.py                 # haversine, shared by the graph and the local server
        ├── llm.py                 # Llama4Client — Snap4City agentic LLM (llama4-agentic-inference, OpenAI-compatible)
        └── token_manager.py       # vendored auth util (OAuth2 token cache/refresh) from referente's reference example
```

---

## 5. Tools consumed — 3 remote + 2 local

The remote `snap4agentic_advisor_native` server (referente-managed) provides **reverse geocoding** (`coordinates_to_address` — labels a GPS-defaulted origin), the **nearest-service search** (`service_search_near_gps_position` — car parks near the destination + "farmacia più vicina" destinations) and **live parking** (`service_info_dev` — free-spaces per car park). We reach it via dashboard auto-discovery (`http://192.168.1.117:8000/apps.json` → `Client(config)`), narrowed to that single server, so FastMCP adds **no** name prefix and tools are called bare (`docs/lessons.md` L6).

**Forward geocoding and routing are served locally** by `mcp_server.py`: `address_search_location` (wrapping the **public** km4city ServiceMap — the remote one is server-side broken, L28) and `route` (`vehicle="foot"|"car"|"bus"` + start/end coordinates + optional `startdatetime`, wrapping the What-If GraphHopper router — the remote `routing` tool is retired, L46). The client connects to it as a **separate** single-server client (`S4C_LOCAL_MCP_URL`); keeping it separate preserves the remote tools' bare names (L29).

The deterministic `execute` node chains them in Python — resolve origin (geocode, or the GPS point) → resolve destination (geocode, or nearest service by category) → route each mode — and `mcp_tools.exec_tool` executes every call. Concrete tool signatures live in [docs/snap4city-api-notes.md](docs/snap4city-api-notes.md) §2; re-probe the live registry from the JupyterHub with:

```powershell
uv run python -c "
import asyncio, json, httpx
from fastmcp import Client
async def main():
    async with httpx.AsyncClient() as h:
        cfg = (await h.get('http://192.168.1.117:8000/apps.json', timeout=10)).json()
    async with Client(cfg) as c:
        for t in await c.list_tools():
            print(t.name, '—', (t.description or '').strip().splitlines()[0][:120])
asyncio.run(main())
"
```

---

## 6. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `uvicorn api:app` → `ModuleNotFoundError: snap4city_mobility_mcp` | The package isn't installed in the active env. Run `pip install -e .` (inside the `s4c` conda env on the JupyterHub) or `uv run uvicorn api:app …` locally. |
| `POST /advise` → `Llama4Error: no user_credentials.json found` | Place `user_credentials.json` (`{"username": ..., "password": ...}`) in the repo root. The LLM only answers from the JupyterHub. |
| `apps.json` 404 / connection refused / timeout | Not running inside the JupyterHub (the intranet IP is reachable only from there), or the dashboard is down. Check that `S4C_DASHBOARD_URL` is not overridden. |
| A `public_transport` request takes ~30–45 s | Known current state, not a client bug: the online whatif-router has not merged the `pt-router-singleton` perf patch, so every PT request rebuilds the PT graph (`docs/lessons.md` L42/L46). `BUS_ROUTE_TIMEOUT_S=120` covers it; sub-second once merged. Foot/car never touch the PT graph (~0.3–0.5 s). |
| Chat shows *"bridge non raggiungibile"* on a long (bus) turn | The widget must be the current one (job + poll). An old single-request widget hangs on the POST and the proxy chain cuts it at ~60 s even though the turn succeeded (L47). Re-paste `frontend/mobility_advisor_dashboard.html`. |
| VS Code: *"Package `fastmcp` is not installed in the selected environment"* | Point the IDE's interpreter at `.venv\Scripts\python.exe` (Command Palette → *Python: Select Interpreter*). |

---

## 7. License

TBD — academic project.
