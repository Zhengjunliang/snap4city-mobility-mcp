# snap4city-mobility-mcp

FastMCP server exposing a **mobility advisor tool** for the Snap4City Agentic LLM. UNIFI — *Sistemi Distribuiti, elaborato Tipo A*.

User asks a trip question → the agent calls this tool → the tool orchestrates Snap4City routing / parking / public-transport microservices → returns multi-modal options to be rendered by a Snap4City dashboard widget.

---

## Status

**Phase 3** — hello-world tools (`greet` + `fake_route`) implemented and verified via MCP Inspector + HTTP smoke client. Real Snap4City API integration is the next phase.

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
PS D:\...\snap4city-mobility-mcp> uv run fastmcp dev src/snap4city_mobility_mcp/server.py:mcp
PS D:\...\snap4city-mobility-mcp> uv run python scripts/smoke_client.py
```

Prefix every command with `uv run`. No `(.venv)` indicator in the prompt.

### Option B — Activate (shorter commands)

```powershell
# PowerShell
PS D:\...\snap4city-mobility-mcp> .venv\Scripts\Activate.ps1
(.venv) PS D:\...\snap4city-mobility-mcp> fastmcp dev src/snap4city_mobility_mcp/server.py:mcp
```

```cmd
:: Windows cmd.exe
D:\...\snap4city-mobility-mcp> .venv\Scripts\activate.bat
(.venv) D:\...\snap4city-mobility-mcp> fastmcp dev src/snap4city_mobility_mcp/server.py:mcp
```

The `(.venv) ` prefix in the prompt means the venv is active. Type `deactivate` to leave.

**bash / zsh**: `source .venv/bin/activate`.

**Don't** `pip install fastmcp` into your global Python and then run `fastmcp` directly — that bypasses the lockfile and pollutes your system Python.

---

## 5. Run modes — three ways

### A. MCP Inspector (recommended for development)

```powershell
uv run fastmcp dev src/snap4city_mobility_mcp/server.py:mcp
```

This launches the official [MCP Inspector](https://github.com/modelcontextprotocol/inspector) — a browser-based debug UI that spawns the server as a subprocess over stdio and lets you call tools interactively.

1. Wait for the browser to open (typically `http://localhost:6274`).
2. STDIO transport is preselected → click **Connect**.
3. Open the **Tools** tab → click **List Tools**.
4. Expect `greet` and `fake_route` to appear.
5. Call `greet` with `{"name": "World"}` → expect `"Ciao World!"`.
6. Call `fake_route` with `{"origin": "A", "destination": "B", "mode": "walk"}` → expect a `RouteOption` JSON with three fields (`mode`, `duration_min`, `summary`).
7. Press `Ctrl+C` in the terminal to stop the Inspector.

> **Path-escape warning**: if you fill the Inspector's **Arguments** field manually, **use forward slashes** (`src/snap4city_mobility_mcp/server.py:mcp`). Backslashes are interpreted as escape characters and silently corrupt the path into `srcsnap4city_mobility_mcpserver.py`, which fails to load.

### B. HTTP transport + smoke client (programmatic verification)

In one terminal, start the server in HTTP mode:

```powershell
uv run fastmcp run src/snap4city_mobility_mcp/server.py:mcp --transport http --port 8000
```

In a second terminal, run the smoke client:

```powershell
uv run python scripts/smoke_client.py
```

Expected output:

```
greet -> Ciao Junliang!
fake_route -> CallToolResult(..., RouteOption(mode='walk', duration_min=42, summary='S.Marta -> Duomo via walk'))
```

### C. Installed console script

```powershell
uv pip install -e .
uv run snap4city-mobility-mcp
```

**What this does**:

- `uv pip install -e .` — installs the package in **editable mode** (`-e`) from the current directory (`.`). No files are copied; a link pointing at `src/` is registered inside `.venv/`. Edits to `server.py` take effect immediately, no reinstall needed. As a side effect, a small launcher named `snap4city-mobility-mcp.exe` is generated in `.venv\Scripts\` (`.venv/bin/` on Unix).
- `uv run snap4city-mobility-mcp` — runs that launcher (equivalent to `python -m snap4city_mobility_mcp.server`, just shorter). The server starts on **stdio** and blocks waiting for JSON-RPC frames — this is normal. Press `Ctrl+C` to exit.

> If you activated the venv (§4 Option B), you can drop the `uv run` prefix and call `snap4city-mobility-mcp` directly. Without activation, `uv run` is required because `.venv\Scripts\` is not on your shell's `PATH`. If you see *`'snap4city-mobility-mcp' is not recognized as an internal or external command`* (or the Italian / localized equivalent), this is the cause.

The console script exists because MCP host applications (any program that spawns an MCP server as a subprocess) prefer a single binary name over `python -m package.module`. Even if you don't use such a host yourself, this is the entry point that downstream integrators will reference.

---

## 6. Project layout

```
snap4city-mobility-mcp/
├── pyproject.toml              # uv-managed project file, fastmcp<3 dep
├── uv.lock                     # exact-version lockfile (committed)
├── .python-version             # "3.10" (committed)
├── README.md                   # this file
├── src/
│   └── snap4city_mobility_mcp/
│       ├── __init__.py         # package version only
│       └── server.py           # FastMCP instance + tools + main()
└── scripts/
    └── smoke_client.py         # HTTP-transport smoke test
```

---

## 7. Tools currently exposed

| Tool | Signature | Returns | Purpose |
|---|---|---|---|
| `greet` | `(name: str)` | `str` | Sanity check — proves transport + tool registration + round-trip |
| `fake_route` | `(origin: str, destination: str, mode: Literal["walk","tpl","car"])` | `RouteOption` | Schema validation — proves `Literal` + `pydantic.BaseModel` correctly generate input/output schema. **Not real routing.** |

`RouteOption` shape:

```python
class RouteOption(BaseModel):
    mode: Literal["walk", "tpl", "car", "bike"]
    duration_min: int
    summary: str
```

---

## 8. Verification checklist

- [ ] `uv sync` completes without error
- [ ] Tool registration check:
  ```powershell
  uv run python -c "from snap4city_mobility_mcp.server import mcp; import asyncio; print(list(asyncio.run(mcp.get_tools()).keys()))"
  ```
  Expected: `['greet', 'fake_route']`
- [ ] Inspector (§5A) connects; both tools visible; both tools return expected values
- [ ] HTTP smoke client (§5B) prints the expected output

---

## 9. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `fastmcp: command not found` | `uv sync` was not run, or your shell is not pointing at `.venv`. Use `uv run fastmcp …` to bypass activation. |
| Inspector floods with `notifications/message` errors and the path looks like `srcsnap4city_mobility_mcpserver.py` | Backslash escape issue — re-read the path-escape warning in §5A and use forward slashes. |
| `AttributeError: module 'snap4city_mobility_mcp' has no attribute 'main'` when running `snap4city-mobility-mcp` | Your `pyproject.toml` is outdated. The `[project.scripts]` entry must point to `snap4city_mobility_mcp.server:main`. Pull the latest code. |
| `'snap4city-mobility-mcp' is not recognized as an internal or external command` (or the localized equivalent — Italian: *"non è riconosciuto come comando interno o esterno"*) | The venv is not activated, so `.venv\Scripts\` is not on `PATH`. Either prefix with `uv run` (`uv run snap4city-mobility-mcp`) or activate the venv first (see §4 Option B). |
| stdio server appears to hang after startup | Normal — stdio servers block on stdin waiting for JSON-RPC frames. They are designed to be spawned by MCP host applications, not run interactively. |
| Port 8000 already in use | `netstat -ano \| findstr 8000` to find the PID, then `taskkill /PID <pid> /F` to free it. Or change `--port` to e.g. 8765. |
| VS Code shows *"Package `fastmcp` is not installed in the selected environment"* | The IDE's Python interpreter is not pointing at `.venv\Scripts\python.exe`. Open the Command Palette → *Python: Select Interpreter* → pick the one inside `.venv`. |

---

## 10. License

TBD — academic project.
