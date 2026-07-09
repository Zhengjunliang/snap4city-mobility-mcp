# snap4city-mobility-mcp

**Langgraph MCP client** for referente's remote Snap4City mobility advisor server. UNIFI — *Sistemi Distribuiti, elaborato Tipo A*.

User asks a trip question → a Langgraph **deterministic** graph (`understand → execute → respond`) resolves it: the Snap4City **Llama4** LLM only extracts the request slots (origin/destination/mode) and phrases the final answer, while Python deterministically drives the remote MCP server's tools (geocoding, routing) — the LLM never free-calls tools. Returns widget JSON to be rendered by a Snap4City dashboard widget. The MCP server itself is referente-managed and deployed on the intranet (reached directly from the Snap4City JupyterHub); this project ships only the **client + Langgraph orchestrator + a FastAPI bridge (`api.py`) and dashboard chat-box front-end (`frontend/`) for testing**.

---

## Status

Working end-to-end on the **Snap4City JupyterHub** (browser login, intranet-direct MCP, no VPN/SSH tunnel). The remote referente MCP server is connected over HTTP Streamable, and the **Llama4 agentic LLM client** (`src/snap4city_mobility_mcp/llm.py`, endpoint `llama4-agentic-inference`) is live there.

The deterministic advisor answers **point-to-point trip questions** — on foot, by car, or by public transport — with GPS-aware endpoint resolution:

- **Named places** geocode worldwide (no region lock); a city the user names always wins, and with the browser GPS available the candidate **nearest to the user** wins among equals.
- **Missing origin** ("portami al Duomo") defaults to the **user's GPS position** (reverse-geocoded once so the reply can say *"dalla tua posizione"*); without GPS the advisor asks for the starting point.
- **Generic-category destinations** ("la farmacia più vicina") resolve via the remote `service_search_near_gps_position` tool — the nearest service of that km4city category around the user (or around the named city without GPS).

Known **server-side** limit (the client is correct — reported to the referente): `public_transport` routing on the remote server never returns transit (`docs/lessons.md` L19) — the local `bus_route` tool covers it via the What-If GraphHopper router.

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

The front end is a natural-language **chat box** on the Snap4City dashboard (`frontend/mobility_advisor_dashboard.html`, a `widgetExternalContent`) talking to the **FastAPI bridge** `api.py`. Internally a Langgraph graph `understand → execute → respond`: Llama4 extracts the slots (`understand`, forced tool call) and phrases the answer (`respond`, no tools), while `execute` deterministically calls the remote `snap4agentic_advisor_native` tools (geocoding + routing) in Python over HTTP Streamable transport. The LLM never free-calls tools (see `docs/lessons.md` L13).

Run **two processes** on the JupyterHub (three with the optional whatif-router; see §11). First the **local MCP server** that serves
forward geocoding (referente's remote `address_search_location` is server-side broken, so we host
our own tool wrapping the public km4city ServiceMap — see `docs/lessons.md` L28/L29). It only needs
outbound HTTP to the public ServiceMap:

```bash
python -m snap4city_mobility_mcp.mcp_server          # serves http://0.0.0.0:8020/mcp/
```

Then the bridge (where Llama4 + the remote MCP server are reachable); the browser reaches it
same-origin through `jupyter-server-proxy` (setup recipe in `docs/lessons.md` L27, wiring in
`frontend/README.md`). It connects to the local geocode server via `S4C_LOCAL_MCP_URL`
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
    "distance_km": 0.679, "eta": "HH:MM:SS", "duration": "00:10:00"
  },
  "messages": [ ... updated conversation; LAST assistant turn = the reply text ... ]
}
```

The reply text is the **last `assistant` turn in `messages`** (OpenAI-standard) — there is no custom top-level `answer` field. `data` carries the route payload: the full `wkt` LINESTRING + `distance_km` + `eta` + `duration` + source/destination node (the `arcs` per-segment detail is currently omitted to slim the payload). `messages` is the conversation history carried forward for multi-turn (the dashboard front-end keeps it and sends it back as `history` each turn). Out-of-scope questions (including transport-network reference questions like line lists or timetables) return a friendly "unsupported" reply.

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
├── whatif-local/               # local whatif-router harness (optional, real PT lines) + the referente perf patch (see its README)
├── docs/
│   ├── lessons.md              # architectural traps (km4city / runtime)
│   └── snap4city-api-notes.md  # field-by-field observations of the real API
├── tests/                      # local mock unit tests (no LLM / MCP needed)
└── src/
    └── snap4city_mobility_mcp/    # client-only package — MCP server itself is referente-managed (remote)
        ├── __init__.py            # package version only
        ├── mcp_tools.py           # client MCP layer: Client config, exec_tool, routing_with_retry, geocode helpers
        ├── mcp_server.py          # our own local MCP server: forward geocode tool wrapping the public km4city ServiceMap (referente's is broken, L29)
        ├── orchestrator.py        # deterministic Langgraph graph: understand → execute → respond; run_advisor
        ├── llm.py                 # Llama4Client — Snap4City agentic LLM (llama4-agentic-inference, OpenAI-compatible tool calling)
        └── token_manager.py       # vendored auth util (OAuth2 token cache/refresh) from referente's reference example
```

---

## 7. Tools consumed (remote) + one served locally

The remote `snap4agentic_advisor_native` server (referente-managed) is the source of truth for routing, reverse geocoding, and the nearest-service search; we connect to it via dashboard auto-discovery (`http://192.168.1.117:8000/apps.json` → `Client(config)`). We narrow the config to that single server, so FastMCP adds **no** name prefix and tools are called bare (`routing`, not `snap4agentic_advisor_native_routing`) — see `docs/lessons.md` L6.

**Exception — forward geocoding is served locally.** The remote `address_search_location` is server-side broken (returns foreign hits / zero Tuscan for valid Florence queries, `docs/lessons.md` L28), so we host our own MCP server (`mcp_server.py`) wrapping the **public** km4city ServiceMap and connect to it as a **separate** single-server client (`S4C_LOCAL_MCP_URL`). Keeping it a separate client — rather than merging both into one config — preserves the remote tools' bare names (no prefix migration, L29). The geocode tool returns the same GeoJSON shape, so the client's geocode pipeline is unchanged.

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

Concrete tool signatures (names + inputSchema + envelope shape) live in [docs/snap4city-api-notes.md §3](docs/snap4city-api-notes.md). Backend reference (km4city `/location/` and `/shortestpath` field-by-field notes — the underlying endpoints the remote tools wrap) is in §1 / §2 of the same file. The advisor drives **6 tools**: `routing`, `coordinates_to_address` (labels a GPS-defaulted origin), `service_search_near_gps_position` (car parks near the destination + nearest-category destinations) and `service_info_dev` (live parking free-spaces) from the remote server, plus `address_search_location` (forward geocode) and `bus_route` (public-transport routing via the What-If router) from the **local** server (`mcp_server.py`). The deterministic `execute` node chains them in Python — resolve origin (geocode, or the GPS point) → resolve destination (geocode, or nearest service by category) → routing per mode (the LLM never picks tools). `mcp_tools.exec_tool` executes each call.

---

## 8. Verification checklist

- [ ] `uv sync` completes without error
- [ ] Local mock tests green (no LLM / MCP needed — runs anywhere): `uv run pytest -q`
- [ ] On the JupyterHub, dashboard reachable:
  ```bash
  curl -s http://192.168.1.117:8000/apps.json | python -m json.tool | head
  ```
  Expected: JSON with `mcpServers` listing `snap4agentic_advisor_native` / `_legacy` / `_experimental`.
- [ ] End-to-end advisor check via the bridge (JupyterHub — drives the LLM + remote MCP server):
  ```bash
  uvicorn api:app --host 0.0.0.0 --port 8010      # in one terminal
  curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
    -d '{"query":"da Piazza del Duomo a Santa Croce a piedi","history":[]}'
  ```
  Expected (also appended to `outputs.txt`): `status="success"`, `request_type="route"`, `data.distance_km ≈ 0.68`, full `data.wkt`. If `data` comes back empty / `route_error` mentions `"no route found (empty routes list)"` on the very first call, retry after ≥ 5 s — known transient km4city behavior (`docs/lessons.md` L3).

---

## 9. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `fastmcp: command not found` | `uv sync` was not run, or your shell is not pointing at `.venv`. Use `uv run fastmcp …` to bypass activation. |
| `uvicorn api:app` → `ModuleNotFoundError: snap4city_mobility_mcp` | The package isn't installed in the active env. Run `pip install -e .` (inside the `s4c` conda env on the JupyterHub) or `uv run uvicorn api:app …` locally. |
| `POST /advise` → `Llama4Error: no user_credentials.json found` | Place `user_credentials.json` (`{"username": ..., "password": ...}`) in the repo root. The LLM only answers from the JupyterHub. |
| `apps.json` 404 / connection refused / timeout from `http://192.168.1.117:8000` | Not running inside the JupyterHub (the intranet IP is reachable only from there), or the dashboard is down. Run from a JupyterHub terminal/notebook and make sure `S4C_DASHBOARD_URL` is not overridden. |
| `routing failed: empty response from routing service (3 attempts) — …` | Still empty after 3 attempts (6 s apart) ≈ a **stable server-side bug** (lesson L8 class — e.g. car into a ZTL, where km4city's `-2` is swallowed into an empty body; also seen on foot, and on every `car` / `public_transport` request, see L19). A transient (L3) self-heals within 3 tries, so reaching this means retries did not clear it. Walking modes already auto-swap profile (`foot_shortest` ⇄ `foot_quiet`); for car, try foot or public transport. The bridge logs each raw payload to `debug.log` — use it to report the issue to the referente. |
| `routing failed: successful (code=0)` | A historical bug (hit during an earlier retest), now fixed; see lesson L7. If it reappears, the code has regressed. |
| VS Code shows *"Package `fastmcp` is not installed in the selected environment"* | The IDE's Python interpreter is not pointing at `.venv\Scripts\python.exe`. Open the Command Palette → *Python: Select Interpreter* → pick the one inside `.venv`. |

---

## 10. License

TBD — academic project.

---

## 11. JupyterHub quick start (run order)

Run from a **JupyterHub terminal** inside the `s4c` conda env (Python 3.11). Two processes are always needed — the local geocode MCP server (`:8020`) and the advisor bridge (`:8010`) — plus an optional local whatif-router (`:8080`) for real public-transport lines.

Start each process in its **own terminal** (in the `s4c` conda env), the local MCP server first. **Always stop a process with `Ctrl-C`, never by just closing the tab** — a closed tab leaves Tomcat running as a zombie holding `:8080`, and the next boot then fails with `Address already in use` / `read lock ... failed`. If zombies pile up, sweep them and confirm the ports are free before restarting:

```bash
whatif-local/apache-tomcat-9.0.119/bin/catalina.sh stop 20 -force 2>/dev/null
pkill -9 -f 'apache-tomcat-9.0.119'
ss -ltn | grep -E ':8080|:8020|:8010' || echo "all ports free"
```

**Terminal 1 — local geocode MCP server** (forward geocode, wraps public km4city ServiceMap, `docs/lessons.md` L29). Must be up before the bridge:

```bash
python -m snap4city_mobility_mcp.mcp_server
```

Listens on `:8020` (client connects via `S4C_LOCAL_MCP_URL`, default `http://127.0.0.1:8020/mcp`). Routing / reverse geocode / nearest-service search still go to the referente remote server.

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

### Optional — local whatif-router (real public-transport lines)

`bus_route` calls the Snap4City What-If GraphHopper router, defaulting to a **local** instance at `http://localhost:8080/whatif-router/route` (`mcp_server.py`'s `WHATIF_ROUTER_URL`) because the online `https://www.snap4city.org/whatif-router/route` has **no Tuscany GTFS loaded** and returns a degraded walking line (`docs/lessons.md` L31/L34). So for real public transport, self-host a whatif-router loaded with Toscana GTFS **on the same JupyterHub** — the router and `mcp_server` share the JupyterHub, so `bus_route` reaches it over `localhost` (no tunnel). Full recipe + the referente perf patch: [`whatif-local/README.md`](whatif-local/README.md). Start it FIRST (the graph build takes minutes), after uploading the prebuilt war to `whatif-local/whatif-router.war`:

```bash
bash whatif-local/run-on-jupyterhub.sh   # installs Java8 + Tomcat9, fetches OSM+GTFS, deploys war, starts Tomcat as a detached daemon
```

First boot builds the graph-cache (minutes); the script waits for `PtWarmupListener: PT router ready.`. Tomcat runs detached (setsid): closing the terminal does not stop it — manage it with the `status` / `logs` / `stop` subcommands (`stop` shuts down gracefully and preserves the graph-cache; a hard kill corrupts it). In the `mcp_server` terminal, point `bus_route` at it **before** starting the server:

```bash
export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
```

Once referente loads the GTFS + merges the perf patch on the online instance, revert `mcp_server.py`'s `WHATIF_ROUTER_URL` default to the online URL (or set `S4C_WHATIF_ROUTER_URL` to it) and stop Tomcat.
