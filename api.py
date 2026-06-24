"""FastAPI bridge exposing the mobility advisor to the Snap4City dashboard.

The dashboard chat box (frontend/mobility_advisor_dashboard.html) can't reach the
JupyterHub-only Llama4 + MCP server from the browser, so this thin HTTP layer wraps
run_advisor: POST /advise {query, history} -> the same widget JSON run_advisor returns
(status/request_type/data/messages), passed through verbatim (no extra fields, project
rule 8). The reply is messages[-1].content (OpenAI standard); multi-turn state is the
returned messages, sent back as `history` on the next turn.

Run on the JupyterHub (where Llama4 + the MCP server are reachable), reached from the
browser same-origin through jupyter-server-proxy (see frontend/README.md):
    uvicorn api:app --host 0.0.0.0 --port 8010
Needs user_credentials.json in the repo root and the intranet MCP server reachable.
CORS below is permissive for development and must be tightened before any real exposure.

Diagnostics: each /advise turn OVERWRITES both files so they hold only the latest turn
(easy to inspect "why this query did X"): tool-level DEBUG -> debug.log, full output JSON
-> outputs.txt (both in the cwd). Inspect them on the JupyterHub when a turn draws no route.
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
_PKG_LOGGER = "snap4city_mobility_mcp"


def _reset_debug_log() -> None:
    """Open a FRESH debug.log for the current turn (truncate via mode="w").

    Called at the start of each /advise so debug.log holds only this turn's diagnostics.
    Re-creating the handler (instead of truncating the file underneath an append handler,
    which would leave null bytes at the old offset) is the clean way to truncate. Only this
    package's logger is touched, so httpx etc. stay quiet. Not concurrency-safe (a second
    overlapping request would swap the handler mid-turn); acceptable for single-user testing.
    """
    pkg_logger = logging.getLogger(_PKG_LOGGER)
    for h in list(pkg_logger.handlers):
        pkg_logger.removeHandler(h)
        h.close()
    handler = logging.FileHandler("debug.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    pkg_logger.setLevel(logging.DEBUG)
    pkg_logger.addHandler(handler)
    pkg_logger.propagate = False  # never echo DEBUG payloads to a root/uvicorn handler


def _log_turn(response: dict) -> None:
    """Overwrite outputs.txt with this turn's full output JSON (latest turn only).

    Writes the payload as-is, with no query/response wrapper: the current turn's query
    already lives in response.messages[-2].content (the last user turn)."""
    OUTPUTS.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")


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
    _reset_debug_log()  # fresh debug.log for this turn (before run_advisor emits any DEBUG)
    try:
        response = await run_advisor(req.query, req.history or [])
    except Exception as e:  # noqa: BLE001 - surface infra failure as data, not a 500
        logger.exception("advise failed")
        response = {"status": "error", "error": f"{type(e).__name__}: {e}", "messages": req.history or []}
    _log_turn(response)  # overwrite outputs.txt with this turn's full JSON
    return response
