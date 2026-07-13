"""FastAPI bridge exposing the mobility advisor to the Snap4City dashboard.

The dashboard chat box (frontend/mobility_advisor_dashboard.html) can't reach the
JupyterHub-only Llama4 + MCP server from the browser, so this thin HTTP layer wraps
run_advisor. It is a JOB + POLL protocol (never one long request, see below):
    POST /advise {query, history, gps} -> {"job_id": ...}   (returns at once)
    GET  /advise/{job_id}              -> 202 while running, then 200 with the widget JSON
The 200 body is the same widget JSON run_advisor returns (status/request_type/data/
messages), passed through verbatim (no extra fields, project rule 8): the job id lives
in the transport layer only. The reply is messages[-1].content (OpenAI standard);
multi-turn state is the returned messages, sent back as `history` on the next turn.
`gps` is the browser geolocation {lat, lng} or null; it is sanitized here (never
rejected) so a buggy widget degrades to the no-GPS flow instead of failing the turn.

WHY JOB + POLL (L47): a public-transport turn takes ~50-70 s (the online whatif-router
rebuilds its PT graph on every bus request until the perf patch lands) and the reverse
proxy chain in front cut any request past ~60 s — the browser showed "bridge non
raggiungibile" for a turn that had in fact completed. Feeding the proxy heartbeat bytes
does NOT work: jupyter-server-proxy buffers the whole response body for any request that
isn't Accept: text/event-stream, so no byte leaves this hop before the turn ends. Polling
takes the turn OUT of the request lifetime instead — every HTTP call here is sub-second,
so no proxy (this one, or whatever fronts the referente deployment later) can time it out.
Do not "optimize" this back into a single long request.

Run on the JupyterHub (where Llama4 + the MCP server are reachable), reached from the
browser same-origin through jupyter-server-proxy (see frontend/README.md):
    uvicorn api:app --host 0.0.0.0 --port 8010
Needs user_credentials.json in the repo root and the intranet MCP server reachable.
CORS below is permissive for development and must be tightened before any real exposure.

Diagnostics: each /advise turn OVERWRITES both files so they hold only the latest turn
(easy to inspect "why this query did X"): tool-level DEBUG -> debug.log, full output JSON
-> outputs.txt (both in the cwd). Inspect them on the JupyterHub when a turn draws no route.
"""
import asyncio
import json
import logging
import math
import pathlib
import time
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from snap4city_mobility_mcp.orchestrator import run_advisor

# Named under the package tree ON PURPOSE: _reset_debug_log attaches the per-turn
# debug.log handler to the "snap4city_mobility_mcp" logger, so bridge-level lines
# (received/sanitized gps) land in debug.log too. A bare __name__ ("api") would not.
logger = logging.getLogger("snap4city_mobility_mcp.api")

OUTPUTS = pathlib.Path("outputs.txt")  # full-output audit log, written in the cwd
_PKG_LOGGER = "snap4city_mobility_mcp"

# How long a job stays in the table after it was created. Covers the slowest turn (a bus
# route on the unpatched router, ~70 s) with room to spare; anything older is a job whose
# client went away (tab closed mid-turn), so it is dropped on the next POST.
JOB_TTL_S = 300.0

# job_id -> (created_at, task). In-memory on purpose: the bridge is a single-worker,
# single-user test rig (like _reset_debug_log's per-turn truncation). Running it with
# several uvicorn workers would need sticky routing or a shared store.
_jobs: dict[str, tuple[float, asyncio.Task]] = {}


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
    gps: dict | None = None  # browser geolocation {lat, lng}, null when unavailable/denied


def _sanitize_gps(raw: dict | None) -> dict[str, float] | None:
    """Validated {lat, lng} from the request's gps field, or None.

    Anything not a finite in-range coordinate pair — wrong types, |lat| > 90,
    |lng| > 180, or exactly (0, 0) (null island, the classic uninitialized value) —
    becomes None: the turn must degrade to the no-GPS flow, never 422."""
    if not isinstance(raw, dict):
        return None
    try:
        lat, lng = float(raw.get("lat")), float(raw.get("lng"))
    except (TypeError, ValueError):
        logger.debug("gps ignored (non-numeric): %r", raw)
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        logger.debug("gps ignored (non-finite): %r", raw)
        return None
    if abs(lat) > 90 or abs(lng) > 180 or (lat == 0.0 and lng == 0.0):
        logger.debug("gps ignored (out of range / null island): %r", raw)
        return None
    return {"lat": lat, "lng": lng}


async def _turn(query: str, history: list[dict], gps: dict[str, float] | None) -> dict:
    """One advisor turn, run detached from the HTTP request that started it.

    Never raises: an infra failure (MCP/LLM unreachable) comes back as the JSend-style
    error shape run_advisor itself uses, so the front-end renders it. outputs.txt is
    written HERE, not in the GET handler, so the turn's full JSON is on disk even when
    nobody collects it (tab closed mid-turn)."""
    try:
        response = await run_advisor(query, history, gps=gps)
    except Exception as e:  # noqa: BLE001 - surface infra failure as data, not a 500
        logger.exception("advise failed")
        response = {"status": "error", "error": f"{type(e).__name__}: {e}", "messages": history}
    try:
        _log_turn(response)  # overwrite outputs.txt with this turn's full JSON
    except Exception:  # noqa: BLE001 - a diagnostics write must never sink a turn that worked
        logger.exception("outputs.txt write failed")
    return response


def _prune_jobs() -> None:
    """Drop jobs older than JOB_TTL_S (their client never came back for the result)."""
    now = time.monotonic()
    for job_id in [j for j, (t0, _) in _jobs.items() if now - t0 > JOB_TTL_S]:
        _, task = _jobs.pop(job_id)
        task.cancel()  # a still-running abandoned turn has no reader left


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for the proxy / front-end."""
    return {"status": "ok"}


@app.post("/advise")
async def advise(req: AdviseRequest) -> dict[str, str]:
    """Start one advisor turn and return its job id AT ONCE (the turn keeps running).

    The client then polls GET /advise/{job_id} until it answers 200. See the module
    docstring for why the turn must not be awaited inside this request (L47)."""
    _reset_debug_log()  # fresh debug.log for this turn (before run_advisor emits any DEBUG)
    gps = _sanitize_gps(req.gps)
    # First line of every turn's debug.log: what the widget sent vs what survived
    # sanitization — the one fact needed to split a "GPS didn't work" report into
    # front-end (raw null/garbage) vs back-end (sanitized away / ignored downstream).
    logger.debug("advise turn: gps raw=%r sanitized=%r", req.gps, gps)
    _prune_jobs()
    job_id = uuid.uuid4().hex
    _jobs[job_id] = (time.monotonic(), asyncio.create_task(_turn(req.query, req.history or [], gps)))
    return {"job_id": job_id}


@app.get("/advise/{job_id}")
async def advise_result(job_id: str) -> JSONResponse:
    """Collect a turn started by POST /advise.

    202 = still computing (poll again). 200 = the widget JSON, passed through verbatim
    (rule 8: the job id never enters the payload) and the job is dropped from the table.
    404 = unknown/expired job id, in the same JSend-style error shape the front-end
    already renders."""
    entry = _jobs.get(job_id)
    if entry is None:
        return JSONResponse(status_code=404, content={"status": "error", "error": f"unknown job {job_id}"})
    _, task = entry
    if not task.done():
        return JSONResponse(status_code=202, content={"status": "pending"})
    _jobs.pop(job_id, None)
    # A turn that blew up in the bridge itself (run_advisor failures are already data by now,
    # see _turn) must still come back as the error shape the widget renders — a 500 here would
    # print "bridge non raggiungibile" for a turn that may well have produced a route.
    if task.cancelled():
        return JSONResponse(status_code=404, content={"status": "error", "error": f"job {job_id} was dropped"})
    exc = task.exception()
    if exc is not None:
        logger.error("job %s crashed", job_id, exc_info=exc)
        return JSONResponse(content={"status": "error", "error": f"{type(exc).__name__}: {exc}", "messages": []})
    return JSONResponse(content=task.result())
