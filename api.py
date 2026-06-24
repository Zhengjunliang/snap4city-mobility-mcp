"""FastAPI bridge exposing the mobility advisor to the Snap4City dashboard.

The dashboard chat box (frontend/mobility_advisor_dashboard.html) can't reach the
JupyterHub-only Llama4 + MCP server from the browser, so this thin HTTP layer wraps
run_advisor: POST /advise {query, history} -> the same widget JSON run_advisor returns
(status/request_type/data/messages), passed through verbatim (no extra fields, project
rule 8). The reply is messages[-1].content (OpenAI standard); multi-turn state is the
returned messages, sent back as `history` on the next turn.

Run on the JupyterHub (where Llama4/MCP are reachable), reached from the browser through
jupyter-server-proxy (same origin as the dashboard, see frontend/README.md):
    uvicorn api:app --host 0.0.0.0 --port 8010
Needs user_credentials.json in the repo root and the intranet MCP server reachable.
CORS below is permissive for development and must be tightened before any real exposure.

Diagnostics (inspect on the JupyterHub when a turn draws no route): tool-level DEBUG goes
to debug.log and each turn's full output JSON is appended to outputs.txt (both in the cwd,
fresh per bridge start).
"""
import json
import logging
import pathlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from snap4city_mobility_mcp.orchestrator import run_advisor

logger = logging.getLogger(__name__)

OUTPUTS = pathlib.Path("outputs.txt")  # full-output audit log, written in the cwd


def _setup_debug_log() -> None:
    """Route the package's DEBUG diagnostics to debug.log (file only, so stdout stays
    clean). Only this package's logger is touched, so httpx etc. stay quiet. mode="w"
    gives a fresh log per bridge start (no unbounded accumulation across restarts)."""
    pkg_logger = logging.getLogger("snap4city_mobility_mcp")
    if pkg_logger.handlers:
        return
    handler = logging.FileHandler("debug.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    pkg_logger.setLevel(logging.DEBUG)
    pkg_logger.addHandler(handler)
    pkg_logger.propagate = False  # never echo DEBUG payloads to a root/uvicorn handler


def _log_turn(response: dict) -> None:
    """Append one advisor turn's full output JSON to outputs.txt for offline inspection.

    Writes the payload as-is, with no query/response wrapper: the current turn's query
    already lives in response.messages[-2].content (the last user turn)."""
    block = json.dumps(response, ensure_ascii=False, indent=2)
    with OUTPUTS.open("a", encoding="utf-8") as f:
        f.write(block + "\n" + "=" * 80 + "\n")


_setup_debug_log()
OUTPUTS.write_text("", encoding="utf-8")  # fresh audit log per bridge start

app = FastAPI(title="Snap4City Mobility Advisor bridge")

# Dev-permissive CORS so the dashboard widget can call the bridge. With the bridge reached
# same-origin through jupyter-server-proxy there is no preflight, but this stays permissive
# for local dev. TODO: restrict allow_origins to the dashboard origin before real exposure.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class AdviseRequest(BaseModel):
    query: str
    history: list[dict] | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for the proxy / front-end."""
    return {"status": "ok"}


@app.post("/advise")
async def advise(req: AdviseRequest) -> dict:
    """One advisor turn. Returns run_advisor's widget JSON verbatim.

    Never raises to the client: an infra failure (MCP/LLM unreachable) comes back as the
    JSend-style error shape run_advisor itself uses, so the front-end can render it."""
    try:
        response = await run_advisor(req.query, req.history or [])
    except Exception as e:  # noqa: BLE001 - surface infra failure as data, not a 500
        logger.exception("advise failed")
        response = {"status": "error", "error": f"{type(e).__name__}: {e}", "messages": req.history or []}
    _log_turn(response)  # full JSON → outputs.txt for offline inspection
    return response
