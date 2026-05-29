# snap4city-mobility-mcp

**Langgraph MCP client** for referente's remote Snap4City mobility advisor server. UNIFI — *Sistemi Distribuiti, elaborato Tipo A*.

User asks a trip question → Langgraph agent calls remote MCP server's tools (geocoding + routing) → returns multi-modal options to be rendered by a Snap4City dashboard widget. The MCP server itself is referente-managed and deployed VPN-only; this project ships only the **client + Langgraph orchestrator + CLI glue**.

---

## Status

**Phase 4 closed (2026-05-25) → Phase 5 §2 切远程 server 进行中**. 本地 stand-in MCP server 已退役, 改用 referente 内网 dashboard at `http://localhost:8000` (VPN+SSH tunnel 前提)。详见 [docs/next-phase.md](docs/next-phase.md)。

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
PS D:\...\snap4city-mobility-mcp> uv run snap4city-mobility-cli "Piazza Duomo Firenze" "Piazza Santa Croce Firenze" foot_shortest
PS D:\...\snap4city-mobility-mcp> uv run ruff check src/
```

Prefix every command with `uv run`. No `(.venv)` indicator in the prompt.

### Option B — Activate (shorter commands)

```powershell
# PowerShell
PS D:\...\snap4city-mobility-mcp> .venv\Scripts\Activate.ps1
(.venv) PS D:\...\snap4city-mobility-mcp> snap4city-mobility-cli "Piazza Duomo Firenze" "Piazza Santa Croce Firenze" foot_shortest
```

```cmd
:: Windows cmd.exe
D:\...\snap4city-mobility-mcp> .venv\Scripts\activate.bat
(.venv) D:\...\snap4city-mobility-mcp> snap4city-mobility-cli "Piazza Duomo Firenze" "Piazza Santa Croce Firenze" foot_shortest
```

The `(.venv) ` prefix in the prompt means the venv is active. Type `deactivate` to leave.

**bash / zsh**: `source .venv/bin/activate`.

**Don't** `pip install fastmcp` into your global Python and then run `fastmcp` directly — that bypasses the lockfile and pollutes your system Python.

---

## 5. Run modes

### 0. Prerequisite — VPN + SSH tunnel

The remote Snap4City MCP server is reachable only through UNIFI's Ateneo VPN plus an SSH jumphost. Bring the tunnel up **before anything else**:

1. Connect FortiClient VPN to UNIFI Ateneo (see `Istruzioni_VPNAteneo_Win_V1.0_2020.pdf`).
2. Open a **separate** PowerShell window and start the tunnel — leave it running for the whole session:
   ```powershell
   ssh -L 8000:192.168.1.117:8000 zheng@150.217.15.125
   ```
3. Sanity check the dashboard is reachable from your local machine:
   ```powershell
   Invoke-RestMethod http://localhost:8000/apps.json | ConvertTo-Json -Depth 8
   ```
   Expected: JSON with `mcpServers` listing `snap4agentic_advisor_native` / `_legacy` / `_experimental`. If this fails, fix tunnel / VPN before continuing.

### Route orchestrator CLI

`snap4city-mobility-cli` is the project's single user-facing entry point — it runs the full Langgraph **trip orchestrator** end-to-end on one command line. Internally it opens a FastMCP `Client` over HTTP Streamable transport to the dashboard at `http://localhost:8000` (tunnel above), then chains a `locations`-style geocode tool → a `shortestpath`-style routing tool exposed by the remote `snap4agentic_advisor_native` server. No Inspector, no manual JSON-RPC — just `origin`, `destination`, `route_type` in / JSON route out.

```powershell
# Happy path (foot, ~0.7 km in central Florence)
uv run snap4city-mobility-cli "Piazza Duomo Firenze" "Piazza Santa Croce Firenze" foot_shortest

# Error short-circuit (unresolvable origin → geocode failure)
uv run snap4city-mobility-cli "asdfqwer乱码xyz" "Piazza Duomo Firenze" foot_shortest

# Same point as origin and destination (no route possible)
uv run snap4city-mobility-cli "Piazza Duomo Firenze" "Piazza Duomo Firenze" foot_shortest
```

`route_type` is one of `foot_shortest` (default), `foot_quiet`, `car`, `public_transport`. The 3rd positional argument is optional.

Output shape (success):

```json
{
  "ok": true,
  "summary": {
    "origin": "...", "destination": "...", "route_type": "foot_shortest",
    "origin_coord": "lat;lng", "destination_coord": "lat;lng",
    "distance_km": 0.679, "eta": "HH:MM:SS",
    "wkt_head": "LINESTRING(... first 80 chars ..."
  },
  "raw_journey": { ... full /shortestpath payload ... }
}
```

Output shape (error short-circuit, any node fails):

```json
{ "ok": false, "error": "geocode failed: HTTP 500 ..." }
```

> **Purpose**: this CLI is for local development / debugging / demo. The referente team integrating Langgraph consumes the remote MCP server directly over HTTP Streamable transport. Conceptually, the CLI bundles a tunnel-aware FastMCP `Client` + the two-tool chain (geocode → routing) into one binary so you can sanity-check the whole stack without a UI.

> **Fallback**: if `snap4city-mobility-cli` is not registered yet (e.g. `[project.scripts]` was just edited and `uv pip install -e .` has not been re-run), use the module form instead — it bypasses the `.exe` stub launcher but still relies on the same editable install from `uv sync`:
> ```powershell
> uv run python -m snap4city_mobility_mcp.cli "Piazza Duomo Firenze" "Piazza Santa Croce Firenze" foot_shortest
> ```
> In practice, `uv run snap4city-mobility-cli` will trigger an automatic `uv sync` + editable-wheel rebuild on first call after a `pyproject.toml` change, so the launcher usually appears without manual reinstall.

---

## 6. Project layout

```
snap4city-mobility-mcp/
├── pyproject.toml              # uv-managed project file
├── uv.lock                     # exact-version lockfile (committed)
├── .python-version             # "3.10" (committed)
├── README.md                   # this file
├── docs/
│   ├── next-phase.md           # running plan (phase tracking)
│   └── snap4city-api-notes.md  # field-by-field observations of the real API
└── src/
    └── snap4city_mobility_mcp/    # client-only package — MCP server itself is referente-managed (remote)
        ├── __init__.py            # package version only
        ├── orchestrator.py        # Langgraph 4-node StateGraph: resolve_origin → resolve_destination → compute_route → format_output
        └── cli.py                 # console-script entry for snap4city-mobility-cli (thin wrapper around orchestrator)
```

---

## 7. Tools consumed (remote)

This project does **not** expose any MCP tools — it consumes them. The remote `snap4agentic_advisor_native` server (referente-managed) is the source of truth; we connect to it via dashboard auto-discovery (`http://localhost:8000/apps.json` → `Client(config)`). FastMCP merges multi-server tool names with the server id as a prefix, so the names seen by the orchestrator look like `snap4agentic_advisor_native_<toolname>`.

Live registry (run after VPN + SSH tunnel are up):

```powershell
uv run python -c "
import asyncio, json, httpx
from fastmcp import Client
async def main():
    async with httpx.AsyncClient() as h:
        cfg = (await h.get('http://localhost:8000/apps.json', timeout=10)).json()
    async with Client(cfg) as c:
        for t in await c.list_tools():
            print(t.name, '—', (t.description or '').strip().splitlines()[0][:120])
asyncio.run(main())
"
```

Concrete tool signatures (names + inputSchema + envelope shape) live in [docs/snap4city-api-notes.md §3](docs/snap4city-api-notes.md). Backend reference (km4city `/location/` and `/shortestpath` field-by-field notes — the underlying endpoints the remote tools wrap) is in §1 / §2 of the same file. The two-tool chain the orchestrator drives is **a geocode tool** (input free text, output lat/lng) → **a routing tool** (input two coordinates + mode, output WKT linestring + distance + ETA).

---

## 8. Verification checklist

- [ ] `uv sync` completes without error
- [ ] VPN + SSH tunnel up; dashboard reachable:
  ```powershell
  Invoke-RestMethod http://localhost:8000/apps.json | ConvertTo-Json -Depth 8
  ```
  Expected: JSON with `mcpServers` listing `snap4agentic_advisor_native` / `_legacy` / `_experimental`.
- [ ] End-to-end orchestrator check via CLI (Langgraph chain hits the remote MCP server over HTTP):
  ```powershell
  uv run snap4city-mobility-cli "Piazza Duomo Firenze" "Piazza Santa Croce Firenze" foot_shortest
  ```
  Expected: `ok=true`, `summary.distance_km ≈ 0.68`. If `ok=false` with `"no route found (empty routes list)"` on the very first call, wait ≥ 5 s and retry — known transient km4city behavior (`docs/lessons.md` L3).

---

## 9. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `fastmcp: command not found` | `uv sync` was not run, or your shell is not pointing at `.venv`. Use `uv run fastmcp …` to bypass activation. |
| `'snap4city-mobility-cli' is not recognized` after editing `pyproject.toml [project.scripts]` | The launcher binary in `.venv\Scripts\` was not regenerated. Run `uv pip install -e .` once to re-create it (or just `uv run snap4city-mobility-cli ...` — `uv run` auto-syncs and rebuilds the editable wheel on first call after a `pyproject.toml` change). Pure source-code edits don't need a re-install; only `[project.scripts]` additions/changes do. |
| `apps.json` 404 / connection refused / timeout from `http://localhost:8000` | VPN 没连或 SSH tunnel 挂了。先确认 FortiClient 在线, 再确认那条 `ssh -L 8000:192.168.1.117:8000 zheng@150.217.15.125` 窗口没断 (会话超时 / 网络抖动都会让它沉默死亡)。 |
| `routing failed: empty body (L3 stale didn't clear after retry)` 在 `car` 路径 (尤其中心步行街 Duomo→Santa Croce) | **referente 的 routing wrapper 已知 bug** (lesson L8): km4city 内部对 ZTL 区 car 返 `-2` 没被 wrapper 透传, 反吃成空 body。重试也不愈 (区别 transient L3)。换 `foot_shortest` / `foot_quiet` 走人行可绕过, 或等 referente 修。 |
| `routing failed: successful (code=0)` | 历史 bug (Phase 5 §2 R4 retest 时踩过), 现已修复; 见 lesson L7。如果仍出现说明代码回退了。 |
| Port 8000 already in use 起 SSH tunnel 报错 | 本机已经有别的进程占了 8000。`netstat -ano \| findstr 8000` 找 PID, `taskkill /PID <pid> /F` 释放; 或换 tunnel 本地端口 (`-L 8765:192.168.1.117:8000`), 然后 orchestrator 的 `MCP_URL` 也要改成 `http://localhost:8765/...`。 |
| VS Code shows *"Package `fastmcp` is not installed in the selected environment"* | The IDE's Python interpreter is not pointing at `.venv\Scripts\python.exe`. Open the Command Palette → *Python: Select Interpreter* → pick the one inside `.venv`. |

---

## 10. License

TBD — academic project.
