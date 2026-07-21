# snap4city-mobility-mcp

**Langgraph MCP client** for referente's remote Snap4City mobility advisor server. UNIFI — *Sistemi Distribuiti, elaborato Tipo A*.

User asks a trip question → a Langgraph **deterministic** graph (`understand → execute → respond`) resolves it: the Snap4City **Llama4** LLM only extracts the request slots (origin/destination/mode), while Python deterministically drives the MCP tools — geocoding and routing (all modes) on a **local MCP server** (`mcp_server.py`, wrapping the public km4city ServiceMap and the Snap4City What-If GraphHopper router), reverse geocoding / nearest-service search / live parking on the remote server — and composes the reply. The LLM never free-calls tools. Returns widget JSON to be rendered by a Snap4City dashboard widget. The remote MCP server is referente-managed and deployed on the intranet (reached directly from the Snap4City JupyterHub); this project ships the **client + Langgraph orchestrator + a FastAPI bridge (`api.py`) + dashboard chat-box front-end (`frontend/`) + the local MCP server**.

The written report for the elaborato is in [relazione/](relazione/); architecture diagrams in [docs/diagrams/](docs/diagrams/), dashboard screenshots in [screenshots/](screenshots/), and real end-to-end outputs in [examples/](examples/).

---

## 1. Status

Working end-to-end on the **Snap4City JupyterHub** (browser login, intranet-direct MCP, no VPN/SSH tunnel). The remote referente MCP server is connected over HTTP Streamable, and the **Llama4 agentic LLM client** (`src/snap4city_mobility_mcp/llm.py`, endpoint `llama4-agentic-inference`) is live there.

The deterministic advisor answers **point-to-point trip questions** — on foot, by car, or by public transport — with GPS-aware endpoint resolution:

- **Named places** geocode worldwide (no region lock); a city the user names always wins, and with the browser GPS available the candidate **nearest to the user** wins among equals. Usable km4city data is Tuscany-only, so test with Tuscan places.
- **Missing origin** ("portami al Duomo") defaults to the **user's GPS position** (reverse-geocoded once so the reply can say *"dalla tua posizione"*); without GPS the advisor asks for the starting point.
- **Generic-category destinations** ("la farmacia più vicina") resolve via the remote `service_search_near_gps_position` tool — the nearest service of that km4city category around the user (or around the named city without GPS).
- **Services along the route** ("con le farmacie lungo il percorso") sample anchor points along the computed geometry and run a nearest-search around each, attaching the results per travel mode.

The remote `routing` tool is retired on the client side: **all routing** (foot / car / public transport) goes through the local `route` tool, which wraps the Snap4City What-If GraphHopper router. The remote tool never returned real transit for `public_transport`, and its km4city backend needed a whole retry ladder for transient empty responses; the What-If router is both correct and — for foot and car — faster. The remote server still provides reverse geocoding, the nearest-service search and live parking data.

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

On the **JupyterHub** (the only place the advisor really runs, see §3) `uv` is usually absent: create a conda **Python 3.11** env (`s4c`) — the default kernel 3.9 is too old for fastmcp — and `pip install -e .`. **Never `pip install` into the JupyterHub base env**: pulling a package that upgrades the base `jupyter-server` breaks the singleuser server on its next restart, and the container overlay is reused across restarts so it cannot self-heal.

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

- **Terminal 1, local MCP server** (`:8020`): forward geocoding (wrapping the **public** km4city ServiceMap — referente's remote `address_search_location` is server-side broken, evidence in [docs/snap4city-api-notes.md](docs/snap4city-api-notes.md) §3) and the `route` tool for all modes (wrapping the What-If GraphHopper router). It only needs outbound HTTP. The client reaches it via `S4C_LOCAL_MCP_URL` (default `http://127.0.0.1:8020/mcp`).
- **Terminal 2, advisor bridge** (`:8010`): drives the LLM and both MCP servers. The browser reaches it same-origin through `jupyter-server-proxy` (install recipe and wiring in [frontend/README.md](frontend/README.md)).

**Both processes must be restarted after a code change.** Restarting only the bridge leaves the old local MCP server running, which produces baffling symptoms (endpoints drifting, `civic` fields coming back empty) that look like logic bugs but are just a stale process.

The front end is a natural-language **chat box** on the Snap4City dashboard (`frontend/mobility_advisor_dashboard.html`, a `widgetExternalContent`) talking to the bridge, with the route drawn on a sibling `widgetMap`.

### The bridge protocol: job + poll

`POST /advise` **starts** the turn and answers `{"job_id": ...}` immediately; `GET /advise/{job_id}` returns `202` while it computes and `200` with the widget JSON once done. A public-transport turn runs ~50–70 s and the reverse proxy chain cuts any single request past ~60 s, so no HTTP request here may span a whole turn.

**Do not collapse this back into one request, and do not try to fix it with heartbeat streaming**: `jupyter-server-proxy` only streams responses whose `Accept` is `text/event-stream` and buffers every other response in full, so heartbeat bytes never reach the upstream proxy. With job + poll every HTTP request is sub-second, so no proxy's buffering or timeout policy can cut it.

Each `202` also carries the **stage** (`understand` → `geocode` → `routing` / `routing_bus` → `respond`) and its `elapsed_s`, so the chat box says what is running instead of showing a blank "thinking" bubble. Stage and job id live in the transport layer only — they never enter the widget JSON.

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

Each turn overwrites `outputs.txt` with the full output JSON and `debug.log` with the tool-level diagnostics (both gitignored, both in the cwd) — inspect them when a turn draws no route. Captured examples of that output are committed in [examples/](examples/).

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

The reply text is the **last `assistant` turn in `messages`** (OpenAI-standard) — no custom top-level `answer` field. `data` carries the full `wkt` + `distance_km` + `duration` + `mode`, plus a `routes` list (one per travel mode; a bus route also ships its walk/ride `legs` geometry for the map split, and each route carries a pre-rendered `detail` string and its own `services`). The front-end keeps `messages` and sends them back as `history` next turn, and sends the browser geolocation as `gps: {lat, lng}` (or `null`) with every turn. Out-of-scope questions (including network reference questions like line lists or timetables) return a friendly "unsupported" reply.

**Travel modes.** When the question does not name one (*"da Piazza del Duomo a Santa Croce"*), all three are routed **concurrently** — on foot, by car and by public transport — so the reply compares them and the map draws a line each. The turn answers once, when all three are in: the wall clock is the slowest of them, today the bus one, because the online whatif-router rebuilds its public-transport graph on every `vehicle=bus` request (~30–45 s, an accepted latency — the stage-aware thinking bubble keeps the wait visible); foot and car are sub-second. Naming a mode (*"a piedi"*) routes that one only, so it never pays the bus latency. A **departure time** the user gives (*"alle 18"*, *"domani alle 9"*) becomes the public-transport timetable window; an *arrival* time is not supported (the What-If servlet has no `arrive_by`).

**Known limitation — the bus line is drawn stop to stop where no GTFS shape matches.** A public-transport *ride* leg comes back from the router with one vertex per stop (measured: 8 vertices over 1.78 km, longest hop 476 m), so the raw line cuts straight across blocks instead of following the roads. Not missing data — both GTFS feeds carry `shapes.txt` — but **GraphHopper's GTFS importer ignores it**, so a ride leg's points are just its stop coordinates. Not a request parameter either: the servlet takes none for geometry, and GraphHopper has no shapes switch (its open PR #3127 is unmerged; even master draws stop-to-stop). The client works around this at runtime (`gtfs_shapes.py`) by matching the line against the public km4city *tpl* API and swapping in the real shape, cut between the boarding and alighting stops; when no variant matches within tolerance the leg keeps the straight chord. The clean fix is server-side and small — gtfs-lib already loads `shapes.txt` into memory and exposes `GTFSFeed.getTripGeometry(trip_id)`, and the servlet already holds `ptLeg.trip_id` where it serializes the leg, so a `shape_wkt` field there would give every client the real bus path (feature request for the referente).

### Optional — point `route` at a different whatif-router

`route` defaults to the **online** What-If router (`https://www.snap4city.org/whatif-router/route`), which carries the Tuscany GTFS since 2026-07-10 and returns real transit — **no third process needed**. To test against a self-built router (e.g. a different GTFS set), override the endpoint:

```bash
export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
```

---

## 4. Project layout

```
snap4city-mobility-mcp/
├── LICENSE                     # MIT
├── pyproject.toml              # uv-managed project file
├── uv.lock                     # exact-version lockfile (committed)
├── api.py                      # FastAPI bridge for the dashboard chat box (job/poll: POST /advise + GET /advise/{job_id})
├── frontend/                   # Snap4City dashboard front-end (widgetExternalContent chat box + widgetMap)
├── relazione/                  # elaborato report — LaTeX source + PDF
├── docs/
│   ├── diagrams/               # UML: PlantUML sources (.puml) + rendered .png
│   └── snap4city-api-notes.md  # field-by-field observations of the real API
├── screenshots/                # dashboard screenshots of the working advisor
├── examples/                   # real widget-JSON outputs captured from live turns
├── scripts/                    # delivery packaging
├── tests/                      # local mock unit tests (no LLM / MCP needed)
└── src/
    └── snap4city_mobility_mcp/    # client package + local MCP server — the remote advisor server is referente-managed
        ├── mcp_tools.py           # client MCP layer: Client config, exec_tool, two-pass geocode, result parsers
        ├── mcp_server.py          # our local MCP server: forward geocode (public km4city ServiceMap) + `route` for all modes (What-If GraphHopper)
        ├── orchestrator.py        # deterministic Langgraph graph: understand → execute → respond; run_advisor
        ├── gtfs_shapes.py         # swaps bus ride-leg chords for real km4city GTFS shapes
        ├── geo.py                 # haversine + WKT helpers, shared by the graph and the local server
        ├── llm.py                 # Llama4Client — Snap4City agentic LLM (llama4-agentic-inference, OpenAI-compatible)
        └── token_manager.py       # vendored auth util (OAuth2 token cache/refresh) from referente's reference example
```

---

## 5. Tools consumed — 3 remote + 2 local

The remote `snap4agentic_advisor_native` server (referente-managed) provides **reverse geocoding** (`coordinates_to_address` — labels a GPS-defaulted origin), the **nearest-service search** (`service_search_near_gps_position` — car parks near the destination, "farmacia più vicina" destinations and services along the route) and **live parking** (`service_info_dev` — free-spaces per car park). We reach it via dashboard auto-discovery (`http://192.168.1.117:8000/apps.json` → `Client(config)`), narrowed to that single server: FastMCP only prefixes tool names when it merges **several** servers into one config, so with a single-server config the tools are called by their bare names.

**Forward geocoding and routing are served locally** by `mcp_server.py`: `address_search_location` (wrapping the **public** km4city ServiceMap, because the remote one is server-side broken) and `route` (`vehicle="foot"|"car"|"bus"` + start/end coordinates + optional `startdatetime`, wrapping the What-If GraphHopper router). The client connects to it as a **separate** single-server client (`S4C_LOCAL_MCP_URL`); keeping it separate is what preserves the remote tools' bare names.

The deterministic `execute` node chains them in Python — resolve origin (geocode, or the GPS point) → resolve destination (geocode, or nearest service by category) → route each mode → optionally sample services along the geometry — and `mcp_tools.exec_tool` executes every call. Concrete tool signatures live in [docs/snap4city-api-notes.md](docs/snap4city-api-notes.md) §2; re-probe the live registry from the JupyterHub with:

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
| A `public_transport` request takes ~30–45 s | Known and accepted, not a client bug: the online whatif-router rebuilds the PT graph on every PT request. `BUS_ROUTE_TIMEOUT_S=120` covers it. Foot/car never touch the PT graph (~0.3–0.5 s). |
| Endpoints drift, `civic` comes back empty, or a fix seems to have no effect | The local MCP server (`:8020`) was not restarted after the code change. Restart **both** processes. |
| Chat shows *"bridge non raggiungibile"* on a long (bus) turn | The widget must be the current one (job + poll). An old single-request widget hangs on the POST and the proxy chain cuts it at ~60 s even though the turn succeeded. Re-paste `frontend/mobility_advisor_dashboard.html`. |
| VS Code: *"Package `fastmcp` is not installed in the selected environment"* | Point the IDE's interpreter at `.venv\Scripts\python.exe` (Command Palette → *Python: Select Interpreter*). |

---

## 7. License

**MIT** — see [LICENSE](LICENSE).

`src/snap4city_mobility_mcp/token_manager.py` (OAuth2 token cache/refresh) is adapted from referente's Snap4City reference notebook and redistributed here for this academic elaborato; all other code is original.
