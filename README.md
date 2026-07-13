# snap4city-mobility-mcp

**Langgraph MCP client** for referente's remote Snap4City mobility advisor server. UNIFI — *Sistemi Distribuiti, elaborato Tipo A*.

User asks a trip question → a Langgraph **deterministic** graph (`understand → execute → respond`) resolves it: the Snap4City **Llama4** LLM only extracts the request slots (origin/destination/mode) and phrases the final answer, while Python deterministically drives the MCP tools — geocoding and routing (all modes) on a **local MCP server** (`mcp_server.py`, wrapping the public km4city ServiceMap and the Snap4City What-If GraphHopper router), reverse geocoding / nearest-service search / live parking on the remote server — the LLM never free-calls tools. Returns widget JSON to be rendered by a Snap4City dashboard widget. The remote MCP server is referente-managed and deployed on the intranet (reached directly from the Snap4City JupyterHub); this project ships the **client + Langgraph orchestrator + a FastAPI bridge (`api.py`) + dashboard chat-box front-end (`frontend/`) + the local MCP server**.

---

## Status

Working end-to-end on the **Snap4City JupyterHub** (browser login, intranet-direct MCP, no VPN/SSH tunnel). The remote referente MCP server is connected over HTTP Streamable, and the **Llama4 agentic LLM client** (`src/snap4city_mobility_mcp/llm.py`, endpoint `llama4-agentic-inference`) is live there.

The deterministic advisor answers **point-to-point trip questions** — on foot, by car, or by public transport — with GPS-aware endpoint resolution:

- **Named places** geocode worldwide (no region lock); a city the user names always wins, and with the browser GPS available the candidate **nearest to the user** wins among equals.
- **Missing origin** ("portami al Duomo") defaults to the **user's GPS position** (reverse-geocoded once so the reply can say *"dalla tua posizione"*); without GPS the advisor asks for the starting point.
- **Generic-category destinations** ("la farmacia più vicina") resolve via the remote `service_search_near_gps_position` tool — the nearest service of that km4city category around the user (or around the named city without GPS).

The remote `routing` tool is retired on the client side: **all routing** (foot / car / public transport) goes through the local `route` tool, which wraps the Snap4City What-If GraphHopper router (`docs/lessons.md` L46 — the remote tool's `public_transport` never returned transit, L19, and its km4city backend needed a whole retry ladder, L3/L8). The remote server still provides reverse geocoding, the nearest-service search and live parking data.

---

## Prerequisites

- Python **≥ 3.10** (project pinned to 3.10 via `.python-version`)
- [`uv`](https://github.com/astral-sh/uv) — modern Python project + venv manager

Commands below are written for **PowerShell** on Windows; bash equivalents are noted in parentheses when they differ.

---

## 1. Install Python 3.10

- **Windows**: `winget install Python.Python.3.10` *(or download from [python.org](https://www.python.org/downloads/); avoid the Microsoft Store build — known PATH issues)*
- **macOS**: `brew install python@3.10`
- **Linux**: use your distribution's package manager

Verify:

```powershell
python --version
# Python 3.10.x
```

---

## 2. Install `uv`

Simplest (uses whichever Python is on PATH):

```powershell
pip install uv
```

Or via the official installer:

- **Windows**: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
- **macOS / Linux**: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Verify:

```powershell
uv --version
# uv 0.11.x or newer
```

---

## 3. Clone and sync dependencies

```powershell
git clone <repo-url>
cd snap4city-mobility-mcp
uv sync
```

`uv sync` reads `pyproject.toml` + `uv.lock`, creates `.venv/` inside the project, and installs `fastmcp<3` plus all transitive dependencies at the exact versions pinned in the lockfile.

---

## 4. Do I need to activate the venv? — Short answer: **No**

`uv run <cmd>` automatically uses the project's `.venv/`. You can work two ways — **pick one and stick with it**.

### Option A — Don't activate (recommended for newcomers to `uv`)

```powershell
PS D:\...\snap4city-mobility-mcp> uv run pytest -q
PS D:\...\snap4city-mobility-mcp> uv run ruff check src/
```

Prefix every command with `uv run`. No `(.venv)` indicator in the prompt.

### Option B — Activate (shorter commands)

```powershell
# PowerShell
PS D:\...\snap4city-mobility-mcp> .venv\Scripts\Activate.ps1
(.venv) PS D:\...\snap4city-mobility-mcp> pytest -q
```

```cmd
:: Windows cmd.exe
D:\...\snap4city-mobility-mcp> .venv\Scripts\activate.bat
(.venv) D:\...\snap4city-mobility-mcp> pytest -q
```

The `(.venv) ` prefix in the prompt means the venv is active. Type `deactivate` to leave.

**bash / zsh**: `source .venv/bin/activate`.

**Don't** `pip install fastmcp` into your global Python and then run `fastmcp` directly — that bypasses the lockfile and pollutes your system Python.

---

## 5. Run modes

The project runs on the **Snap4City JupyterHub** (referente requires Python dev to run on the dedicated Jupyter; browser login, intranet-direct, no VPN/SSH tunnel). The orchestrator reaches the MCP dashboard at the intranet IP `192.168.1.117:8000` by default; override with the `S4C_DASHBOARD_URL` env var if needed.

The **Llama4 LLM** (`src/snap4city_mobility_mcp/llm.py`) is reachable **only from the JupyterHub**; provide the function-account creds there via a `user_credentials.json` file (`{"username": "...", "password": "..."}`; it is `.gitignore`d, so upload it manually to the repo root). The client searches `S4C_CREDENTIALS_FILE` → working dir → repo root. JupyterHub bootstrap (conda Python 3.11 env — the default kernel 3.9 is too old for fastmcp) is in `CLAUDE.md` §5.1 and `docs/lessons.md` L9.

### 0. Prerequisite — JupyterHub environment

The remote Snap4City MCP server lives on the intranet and is reached directly from the JupyterHub (no VPN/SSH tunnel). Set up and sanity-check there:

1. Log in: snap4city.org → *Strumenti di sviluppo* → *Jupyter Hub - Python* (function account; creds in private memory, not in the repo).
2. Create a conda Python 3.11 env (kernel `s4c`) and `pip install -e .` (see `CLAUDE.md` §5.1 / `docs/lessons.md` L9). The default kernel 3.9 is too old for fastmcp.
3. Sanity check the dashboard is reachable:
   ```bash
   curl -s http://192.168.1.117:8000/apps.json | python -m json.tool | head
   ```
   Expected: JSON with `mcpServers` listing `snap4agentic_advisor_native` / `_legacy` / `_experimental`.

### Mobility advisor (dashboard chat box + bridge `api.py`)

The front end is a natural-language **chat box** on the Snap4City dashboard (`frontend/mobility_advisor_dashboard.html`, a `widgetExternalContent`) talking to the **FastAPI bridge** `api.py`. Internally a Langgraph graph `understand → execute → respond`: Llama4 extracts the slots (`understand`, forced tool call) and phrases the answer (`respond`, no tools), while `execute` deterministically chains the MCP tools in Python — geocoding + routing (`route`, all modes) on the local server, reverse geocode / nearest-service search / live parking on the remote `snap4agentic_advisor_native` server over HTTP Streamable transport. The LLM never free-calls tools (see `docs/lessons.md` L13).

Run **two processes** on the JupyterHub (see §11; routing uses the online whatif-router, no third process needed). First the **local MCP server**, which serves
forward geocoding (referente's remote `address_search_location` is server-side broken, so we host
our own tool wrapping the public km4city ServiceMap — see `docs/lessons.md` L28/L29) and the
`route` tool (foot / car / bus, wrapping the Snap4City What-If GraphHopper router —
`docs/lessons.md` L46). It only needs outbound HTTP to the public ServiceMap and the online
whatif-router:

```bash
python -m snap4city_mobility_mcp.mcp_server          # serves http://0.0.0.0:8020/mcp/
```

Then the bridge (where Llama4 + the remote MCP server are reachable); the browser reaches it
same-origin through `jupyter-server-proxy` (setup recipe in `docs/lessons.md` L27, wiring in
`frontend/README.md`). It connects to the local MCP server via `S4C_LOCAL_MCP_URL`
(default `http://127.0.0.1:8020/mcp`):

```bash
uvicorn api:app --host 0.0.0.0 --port 8010
```

Sanity-check the bridge without the dashboard:

```bash
curl -s localhost:8010/health
curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"da Piazza del Duomo a Santa Croce a piedi","history":[]}'
```

The chat bubble shows **only the LLM's own reply** (`messages[-1].content`, nothing hardcoded); the route (`data.wkt`) is drawn on a sibling `widgetMap`. Follow-ups reuse history (e.g. *"e per una passeggiata tranquilla?"*). The dashboard front-end also sends the browser geolocation as `gps: {lat, lng}` (or `null`) with every turn — origin defaulting and nearest-candidate picking key off it. Every turn also appends the **full output JSON** to `outputs.txt` (gitignored) so you can inspect the whole flow offline; tool-level diagnostics (geocoded coordinates, extracted slots, raw routing payloads on failure) go to `debug.log` (gitignored). Both files are reset at every bridge start — they hold only the current session. That JSON is the widget payload the dashboard consumes:

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

The reply text is the **last `assistant` turn in `messages`** (OpenAI-standard) — there is no custom top-level `answer` field. `data` carries the route payload: the full `wkt` LINESTRING + `distance_km` + `duration` + `mode`, plus a `routes` list (one per travel mode; a bus route also ships its walk/ride `legs` geometry for the map split — the `arcs` per-segment detail is currently omitted to slim the payload). `messages` is the conversation history carried forward for multi-turn (the dashboard front-end keeps it and sends it back as `history` each turn). Out-of-scope questions (including transport-network reference questions like line lists or timetables) return a friendly "unsupported" reply.

> **Note**: the LLM only answers from the JupyterHub (with a `user_credentials.json` present in the repo root).

---

## 6. Project layout

```
snap4city-mobility-mcp/
├── pyproject.toml              # uv-managed project file
├── uv.lock                     # exact-version lockfile (committed)
├── .python-version             # "3.10" (committed)
├── README.md                   # this file
├── api.py                      # FastAPI bridge for the dashboard chat box (POST /advise; writes full JSON to outputs.txt)
├── frontend/                   # Snap4City dashboard front-end (widgetExternalContent chat box + widgetMap)
├── whatif-local/               # referente whatif-router perf patch (patches/) + apply/test notes (see its README)
├── docs/
│   ├── lessons.md              # architectural traps (km4city / runtime)
│   └── snap4city-api-notes.md  # field-by-field observations of the real API
├── tests/                      # local mock unit tests (no LLM / MCP needed)
└── src/
    └── snap4city_mobility_mcp/    # client package + local MCP server — the remote advisor server is referente-managed
        ├── __init__.py            # package version only
        ├── mcp_tools.py           # client MCP layer: Client config, exec_tool, two-pass geocode helpers, result parsers
        ├── mcp_server.py          # our own local MCP server: forward geocode (wraps the public km4city ServiceMap, L29) + `route` for all modes (wraps the What-If GraphHopper router, L46)
        ├── orchestrator.py        # deterministic Langgraph graph: understand → execute → respond; run_advisor
        ├── llm.py                 # Llama4Client — Snap4City agentic LLM (llama4-agentic-inference, OpenAI-compatible tool calling)
        └── token_manager.py       # vendored auth util (OAuth2 token cache/refresh) from referente's reference example
```

---

## 7. Tools consumed — 3 remote + 2 local

The remote `snap4agentic_advisor_native` server (referente-managed) provides reverse geocoding, the nearest-service search and live parking data; we connect to it via dashboard auto-discovery (`http://192.168.1.117:8000/apps.json` → `Client(config)`). We narrow the config to that single server, so FastMCP adds **no** name prefix and tools are called bare (`coordinates_to_address`, not `snap4agentic_advisor_native_coordinates_to_address`) — see `docs/lessons.md` L6.

**Forward geocoding and routing are served locally.** The remote `address_search_location` is server-side broken (returns foreign hits / zero Tuscan for valid Florence queries, `docs/lessons.md` L28), and the remote `routing` tool is retired (`public_transport` never returned transit, L19, and the km4city backend needed a whole retry ladder, L3/L8 — see L46). Our own MCP server (`mcp_server.py`) hosts `address_search_location` (wrapping the **public** km4city ServiceMap) and `route` (`vehicle="foot"|"car"|"bus"` + start/end coordinates + optional `startdatetime`, wrapping the Snap4City What-If GraphHopper router). The client connects to it as a **separate** single-server client (`S4C_LOCAL_MCP_URL`); keeping it separate — rather than merging both into one config — preserves the remote tools' bare names (no prefix migration, L29).

Live registry (run from the JupyterHub):

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

Concrete tool signatures (names + inputSchema + envelope shape) live in [docs/snap4city-api-notes.md §3](docs/snap4city-api-notes.md). Backend reference (km4city `/location/` field-by-field notes — the endpoint the geocode tool wraps) is in §1 of the same file; §2 documents the retired remote `routing` tool and is kept as evidence for the referente. The advisor drives **5 tools**: `coordinates_to_address` (labels a GPS-defaulted origin), `service_search_near_gps_position` (car parks near the destination + nearest-category destinations) and `service_info_dev` (live parking free-spaces) from the remote server, plus `address_search_location` (forward geocode) and `route` (foot / car / bus routing via the What-If router) from the **local** server (`mcp_server.py`). The deterministic `execute` node chains them in Python — resolve origin (geocode, or the GPS point) → resolve destination (geocode, or nearest service by category) → routing per mode (the LLM never picks tools). `mcp_tools.exec_tool` executes each call.

---

## 8. Verification checklist

- [ ] `uv sync` completes without error
- [ ] Local mock tests green (no LLM / MCP needed — runs anywhere): `uv run pytest -q`
- [ ] On the JupyterHub, dashboard reachable:
  ```bash
  curl -s http://192.168.1.117:8000/apps.json | python -m json.tool | head
  ```
  Expected: JSON with `mcpServers` listing `snap4agentic_advisor_native` / `_legacy` / `_experimental`.
- [ ] End-to-end advisor check via the bridge (JupyterHub — drives the LLM + both MCP servers):
  ```bash
  uvicorn api:app --host 0.0.0.0 --port 8010      # in one terminal
  curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
    -d '{"query":"da Piazza del Duomo a Santa Croce a piedi","history":[]}'
  ```
  Expected (also appended to `outputs.txt`): `status="success"`, `request_type="route"`, `data.distance_km ≈ 0.68`, full `data.wkt`.

---

## 9. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `fastmcp: command not found` | `uv sync` was not run, or your shell is not pointing at `.venv`. Use `uv run fastmcp …` to bypass activation. |
| `uvicorn api:app` → `ModuleNotFoundError: snap4city_mobility_mcp` | The package isn't installed in the active env. Run `pip install -e .` (inside the `s4c` conda env on the JupyterHub) or `uv run uvicorn api:app …` locally. |
| `POST /advise` → `Llama4Error: no user_credentials.json found` | Place `user_credentials.json` (`{"username": ..., "password": ...}`) in the repo root. The LLM only answers from the JupyterHub. |
| `apps.json` 404 / connection refused / timeout from `http://192.168.1.117:8000` | Not running inside the JupyterHub (the intranet IP is reachable only from there), or the dashboard is down. Run from a JupyterHub terminal/notebook and make sure `S4C_DASHBOARD_URL` is not overridden. |
| A `public_transport` request takes ~30–45 s | Known current state, not a client bug: the online whatif-router has not merged the `pt-router-singleton` perf patch yet, so every PT request rebuilds the PT graph (`docs/lessons.md` L42/L46). `BUS_ROUTE_TIMEOUT_S=120` covers it; sub-second once the patch is merged. Foot/car never touch the PT graph (~0.3–0.5 s measured). |
| VS Code shows *"Package `fastmcp` is not installed in the selected environment"* | The IDE's Python interpreter is not pointing at `.venv\Scripts\python.exe`. Open the Command Palette → *Python: Select Interpreter* → pick the one inside `.venv`. |

---

## 10. License

TBD — academic project.

---

## 11. JupyterHub quick start (run order)

Run from a **JupyterHub terminal** inside the `s4c` conda env (Python 3.11). Two processes are needed — the local MCP server (`:8020`) and the advisor bridge (`:8010`). Routing (all modes) goes to the **online** whatif-router (Tuscany GTFS deployed 2026-07-10); a local whatif-router (`:8080`) is only a dev harness, see §"Optional".

Start each process in its **own terminal** (in the `s4c` conda env), the local MCP server first. **Always stop a process with `Ctrl-C`, never by just closing the tab** — a closed tab can leave the process holding its port so the next boot fails with `Address already in use`. If a port is stuck, confirm it is free before restarting:

```bash
ss -ltn | grep -E ':8020|:8010' || echo "all ports free"
```

**Terminal 1 — local MCP server** (forward geocode wrapping the public km4city ServiceMap, `docs/lessons.md` L29, + `route` for all modes wrapping the What-If GraphHopper router, L46). Must be up before the bridge:

```bash
python -m snap4city_mobility_mcp.mcp_server
```

Listens on `:8020` (client connects via `S4C_LOCAL_MCP_URL`, default `http://127.0.0.1:8020/mcp`). Reverse geocode / nearest-service search / live parking still go to the referente remote server.

**Terminal 2 — advisor bridge (FastAPI)** (drives the LLM + remote MCP server, dashboard 联动):

```bash
uvicorn api:app --host 0.0.0.0 --port 8010
```

Self-check (separate terminal):

```bash
curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"da Piazza del Duomo a Santa Croce a piedi","history":[]}'
# GPS-aware turns: origin defaults to the position, categories resolve to the nearest service
curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"portami alla farmacia più vicina","history":[],"gps":{"lat":43.7731,"lng":11.2558}}'
```

Each call appends the full JSON to `outputs.txt`; diagnostics go to `debug.log`. Browser reaches the bridge same-origin via jupyter-server-proxy (`docs/lessons.md` L27).

### Optional — point `route` at a local whatif-router

`route` (all modes: foot / car / bus) calls the Snap4City What-If GraphHopper router; the default (`mcp_server.py`'s `WHATIF_ROUTER_URL`) is the **online** instance `https://www.snap4city.org/whatif-router/route`, which carries the Tuscany GTFS since 2026-07-10 and returns real transit. **No extra process is needed.** Foot/car never touch the PT graph and answer sub-second (0.3–0.5 s measured); the online instance does not yet run the `pt-router-singleton` perf patch, so each PT (bus) request takes ~30-45 s (`BUS_ROUTE_TIMEOUT_S` covers it; tighten it back once the patch is merged).

Only to validate that perf patch (or a different GTFS set) do you need a self-built router — apply the patch and point `route` at your build with an env override, see [`whatif-local/README.md`](whatif-local/README.md) and `whatif-local/patches/`:

```bash
export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
```
