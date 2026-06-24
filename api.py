"""FastAPI bridge exposing the mobility advisor to the Snap4City dashboard.

The dashboard chat box (frontend/mobility_advisor_dashboard.html) can't reach the
JupyterHub-only Llama4 + MCP server from the browser, so this thin HTTP layer wraps
run_advisor: POST /advise {query, history} -> the same widget JSON run_advisor returns
(status/request_type/data/messages), passed through verbatim (no extra fields, project
rule 8). The reply is messages[-1].content (OpenAI standard); multi-turn state is the
returned messages, sent back as `history` on the next turn (same contract as chat.py).

Run on the JupyterHub (where Llama4/MCP are reachable):
    uvicorn api:app --host 0.0.0.0 --port 8010
Needs user_credentials.json in the repo root and the intranet MCP server reachable.
Browser reachability (jupyter-server-proxy / same-origin hosting) is a separate concern
decided per deployment; CORS below is permissive for development and must be tightened
before any real exposure.
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from snap4city_mobility_mcp.orchestrator import run_advisor

logger = logging.getLogger(__name__)

app = FastAPI(title="Snap4City Mobility Advisor bridge")

# Dev-permissive CORS so the dashboard widget (different origin) can call the bridge.
# TODO: restrict allow_origins to the dashboard origin before any non-local exposure.
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
    JSend-style error shape run_advisor itself uses, so the front-end can render it the
    same way the CLI REPL does."""
    try:
        return await run_advisor(req.query, req.history or [])
    except Exception as e:  # noqa: BLE001 - surface infra failure as data, not a 500
        logger.exception("advise failed")
        return {"status": "error", "error": f"{type(e).__name__}: {e}", "messages": req.history or []}
