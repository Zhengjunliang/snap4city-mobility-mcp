# snap4city-mobility-mcp

**Langgraph MCP client** for referente's remote Snap4City mobility advisor server. UNIFI — *Sistemi Distribuiti, elaborato Tipo A*.

User asks a trip question → a Langgraph **deterministic** graph (`understand → execute → respond`) resolves it: the Snap4City **Llama4** LLM only extracts the request slots (origin/destination/mode) and phrases the final answer, while Python deterministically drives the remote MCP server's tools (geocoding, routing) — the LLM never free-calls tools. Returns widget JSON to be rendered by a Snap4City dashboard widget. The MCP server itself is referente-managed and deployed on the intranet (reached directly from the Snap4City JupyterHub); this project ships only the **client + Langgraph orchestrator + a terminal chat REPL (`chat.py`) for testing**.

---

## Status

**Phase 5 进行中 (2026-06-03)**. 远程 referente MCP server 已接通 (§2 完成); 运行环境 = **Snap4City JupyterHub** (浏览器登录, 内网直连 MCP, 不用 VPN/SSH tunnel); **Llama4 agentic LLM client** (`src/snap4city_mobility_mcp/llm.py`) 已加, endpoint `llama4-agentic-inference` 在 JupyterHub 实测通。详见 [docs/next-phase.md](docs/next-phase.md)。

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
PS D:\...\snap4city-mobility-mcp> uv run python chat.py
PS D:\...\snap4city-mobility-mcp> uv run ruff check src/
```

Prefix every command with `uv run`. No `(.venv)` indicator in the prompt.

### Option B — Activate (shorter commands)

```powershell
# PowerShell
PS D:\...\snap4city-mobility-mcp> .venv\Scripts\Activate.ps1
(.venv) PS D:\...\snap4city-mobility-mcp> python chat.py
```

```cmd
:: Windows cmd.exe
D:\...\snap4city-mobility-mcp> .venv\Scripts\activate.bat
(.venv) D:\...\snap4city-mobility-mcp> python chat.py
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

### Mobility advisor terminal chat (`chat.py`)

`chat.py` is the project's testing front end — an interactive multi-turn terminal chat over the advisor. Internally a Langgraph graph `understand → execute → respond`: Llama4 extracts the slots (`understand`, forced tool call) and phrases the answer (`respond`, no tools), while `execute` deterministically calls the remote `snap4agentic_advisor_native` tools (geocoding + routing) in Python over HTTP Streamable transport. The LLM never free-calls tools (see `docs/lessons.md` L13).

Run it on the JupyterHub (a plain terminal program — no web server, no proxy; see `docs/lessons.md` L14 for why a terminal REPL over a web UI here):

```bash
python chat.py
```

Then chat — type a question, get the reply, keep going (empty line quits):

```
🧑 > how do I get from Piazza Duomo to Santa Croce on foot?
🤖 ...
🧑 > 那坐公交呢?          # follow-up reuses the previous origin/destination
```

The chat shows **only the LLM's own reply** (nothing hardcoded). Every turn also appends the **full output JSON** to `outputs.txt` (gitignored) so you can inspect the whole flow offline; tool-level diagnostics (geocoded coordinates, raw routing payloads on failure) go to `debug.log` (gitignored). That JSON is the widget payload the dashboard consumes:

```json
{
  "ok": true,
  "intent": "route",
  "data": {
    "wkt": "LINESTRING(11.255 43.773, ...)",   // FULL geometry — map widget draws this
    "distance_km": 0.679, "eta": "HH:MM:SS", "duration": "00:10:00"
  },
  "messages": [ ... updated conversation; LAST assistant turn = the reply text ... ]
}
```

The reply text is the **last `assistant` turn in `messages`** (OpenAI-standard) — there is no custom top-level `answer` field. `data` carries the route payload: the full `wkt` LINESTRING + `distance_km` + `eta` + `duration` + source/destination node (the `arcs` per-segment detail is currently omitted to slim the payload). `messages` is the conversation history carried forward for multi-turn (`chat.py` keeps it in memory across turns). Transport (tpl_*) queries are not wired yet — they return an "unsupported" reply.

> **Note**: the LLM only answers from the JupyterHub (with a `user_credentials.json` present in the repo root).

---

## 6. Project layout

```
snap4city-mobility-mcp/
├── pyproject.toml              # uv-managed project file
├── uv.lock                     # exact-version lockfile (committed)
├── .python-version             # "3.10" (committed)
├── README.md                   # this file
├── chat.py                     # terminal multi-turn chat REPL for testing (writes full JSON to outputs.txt)
├── docs/
│   ├── next-phase.md           # running plan (phase tracking)
│   ├── lessons.md              # architectural traps (km4city / runtime)
│   └── snap4city-api-notes.md  # field-by-field observations of the real API
├── tests/                      # local mock unit tests (no LLM / MCP needed)
└── src/
    └── snap4city_mobility_mcp/    # client-only package — MCP server itself is referente-managed (remote)
        ├── __init__.py            # package version only
        ├── mcp_tools.py           # client MCP layer: Client config, fetch_tool_schemas (from server list_tools), routing_with_retry, exec_tool
        ├── orchestrator.py        # deterministic Langgraph graph: understand → execute → respond; run_advisor
        ├── llm.py                 # Llama4Client — Snap4City agentic LLM (llama4-agentic-inference, OpenAI-compatible tool calling)
        └── token_manager.py       # vendored auth util (OAuth2 token cache/refresh) from referente's reference example
```

---

## 7. Tools consumed (remote)

This project does **not** expose any MCP tools — it consumes them. The remote `snap4agentic_advisor_native` server (referente-managed) is the source of truth; we connect to it via dashboard auto-discovery (`http://192.168.1.117:8000/apps.json` → `Client(config)`). We narrow the config to that single server, so FastMCP adds **no** name prefix and tools are called bare (`routing`, not `snap4agentic_advisor_native_routing`) — see `docs/lessons.md` L6.

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

Concrete tool signatures (names + inputSchema + envelope shape) live in [docs/snap4city-api-notes.md §3](docs/snap4city-api-notes.md). Backend reference (km4city `/location/` and `/shortestpath` field-by-field notes — the underlying endpoints the remote tools wrap) is in §1 / §2 of the same file. The client fetches schemas for **7 of these tools** (`address_search_location`, `routing`, `tpl_agencies`, `tpl_lines`, `tpl_routes_by_line`, `tpl_stops_by_route`, `tpl_stop_timeline`); the deterministic `execute` node chains them in Python (geocode → geocode → routing for a trip — the LLM does not pick tools), and `mcp_tools.exec_tool` executes each call. The `tpl_*` discovery chain is not wired into `execute` yet.

---

## 8. Verification checklist

- [ ] `uv sync` completes without error
- [ ] Local mock tests green (no LLM / MCP needed — runs anywhere): `uv run pytest -q`
- [ ] On the JupyterHub, dashboard reachable:
  ```bash
  curl -s http://192.168.1.117:8000/apps.json | python -m json.tool | head
  ```
  Expected: JSON with `mcpServers` listing `snap4agentic_advisor_native` / `_legacy` / `_experimental`.
- [ ] End-to-end advisor check via the chat UI (JupyterHub — drives the LLM + remote MCP server):
  ```bash
  python chat.py
  # then ask "from Piazza Duomo to Santa Croce on foot"
  ```
  Expected (see `outputs.txt`): `ok=true`, `intent="route"`, `data.distance_km ≈ 0.68`, full `data.wkt`. If `ok=true` but `data.route_error` mentions `"no route found (empty routes list)"` on the very first call, retry after ≥ 5 s — known transient km4city behavior (`docs/lessons.md` L3).

---

## 9. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `fastmcp: command not found` | `uv sync` was not run, or your shell is not pointing at `.venv`. Use `uv run fastmcp …` to bypass activation. |
| `python chat.py` → `ModuleNotFoundError: snap4city_mobility_mcp` | The package isn't installed in the active env. Run `pip install -e .` (inside the `s4c` conda env on the JupyterHub) or `uv run python chat.py` locally. |
| `python chat.py` → `Llama4Error: no user_credentials.json found` | Place `user_credentials.json` (`{"username": ..., "password": ...}`) in the repo root. The LLM only answers from the JupyterHub. |
| `apps.json` 404 / connection refused / timeout from `http://192.168.1.117:8000` | 不在 JupyterHub 内网跑 (内网 IP 只能从 JupyterHub 直连), 或 dashboard 那头挂了。确认在 JupyterHub terminal/notebook 里跑, 且 `S4C_DASHBOARD_URL` 没被设成别的地址。 |
| `routing failed: empty response from routing service (3 attempts) — …` | 3 次 (间隔 6s) 仍空 ≈ **server-side 稳定 bug** (lesson L8 类, 经典案例: ZTL 区 car, km4city `-2` 没被 wrapper 透传反吃成空 body; foot 模式也实测出现过)。transient L3 在 3 次内会自愈, 走到这条说明重试不愈。步行档已自动互换 profile 重试 (`foot_shortest` ⇄ `foot_quiet`); car 可换步行/公交绕过。`chat.py` 会把每次的原始 payload 记进 `debug.log` — 拿它找 referente 报障。 |
| `routing failed: successful (code=0)` | 历史 bug (Phase 5 §2 R4 retest 时踩过), 现已修复; 见 lesson L7。如果仍出现说明代码回退了。 |
| VS Code shows *"Package `fastmcp` is not installed in the selected environment"* | The IDE's Python interpreter is not pointing at `.venv\Scripts\python.exe`. Open the Command Palette → *Python: Select Interpreter* → pick the one inside `.venv`. |

---

## 10. License

TBD — academic project.
