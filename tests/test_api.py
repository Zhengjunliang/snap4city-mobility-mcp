"""Unit tests for the FastAPI bridge (api.py): gps sanitization + the job/poll protocol.

run_advisor is monkeypatched (no LLM/MCP): the tests assert what the bridge forwards
(query, history, sanitized gps) and that the widget JSON comes back untouched (rule 8).
api.py lives at the repo root (not in the src package), so the root goes on sys.path.

Every test drives the TestClient AS A CONTEXT MANAGER: entered, it keeps one event loop
across requests, so the detached turn started by POST /advise survives until the poll
collects it (a bare TestClient spins a fresh loop per request and would cancel the task).
Under uvicorn the loop is the process's, so this is a test-harness detail only.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import api
from api import _sanitize_gps

from fastapi.testclient import TestClient


# --- _sanitize_gps ------------------------------------------------------------

def test_sanitize_gps_valid_passes():
    assert _sanitize_gps({"lat": 43.7731, "lng": 11.2558}) == {"lat": 43.7731, "lng": 11.2558}
    assert _sanitize_gps({"lat": "43.7731", "lng": "11.2558"}) == {"lat": 43.7731, "lng": 11.2558}


def test_sanitize_gps_invalid_becomes_none():
    assert _sanitize_gps(None) is None
    assert _sanitize_gps("43.77;11.25") is None          # wrong type
    assert _sanitize_gps({}) is None                      # missing keys
    assert _sanitize_gps({"lat": "x", "lng": 11.0}) is None  # non-numeric
    assert _sanitize_gps({"lat": float("nan"), "lng": 11.0}) is None  # non-finite
    assert _sanitize_gps({"lat": 999, "lng": 11.0}) is None   # out of range
    assert _sanitize_gps({"lat": 43.0, "lng": 200.0}) is None
    assert _sanitize_gps({"lat": 0, "lng": 0}) is None    # null island = uninitialized value


# --- POST /advise -------------------------------------------------------------

def _client_with_captured_run(monkeypatch, response=None):
    """TestClient whose run_advisor is a capturing fake; returns (client, captured)."""
    captured = {}

    async def fake_run_advisor(query, history=None, gps=None, on_stage=None):
        captured.update({"query": query, "history": history, "gps": gps, "on_stage": on_stage})
        return response if response is not None else {"status": "success", "messages": []}

    monkeypatch.setattr(api, "run_advisor", fake_run_advisor)
    monkeypatch.setattr(api, "_reset_debug_log", lambda: None)  # no debug.log churn in tests
    monkeypatch.setattr(api, "_log_turn", lambda r: None)       # no outputs.txt churn in tests
    return TestClient(api.app), captured


def _collect(client, job_id, tries=50):
    """Poll GET /advise/{job_id} the way the widget does; returns the final response."""
    for _ in range(tries):
        r = client.get(f"/advise/{job_id}")
        if r.status_code != 202:
            return r
    raise AssertionError("job never finished")


def test_advise_forwards_sanitized_gps(monkeypatch):
    client, captured = _client_with_captured_run(monkeypatch)
    with client:
        r = client.post("/advise", json={"query": "portami al Duomo", "history": [],
                                         "gps": {"lat": 43.7731, "lng": 11.2558}})
        assert r.status_code == 200 and r.json()["job_id"]  # the turn runs behind a job id
        _collect(client, r.json()["job_id"])
    assert captured["gps"] == {"lat": 43.7731, "lng": 11.2558}
    assert captured["query"] == "portami al Duomo"


def test_advise_invalid_gps_degrades_to_none_not_422(monkeypatch):
    client, captured = _client_with_captured_run(monkeypatch)
    with client:
        r = client.post("/advise", json={"query": "q", "history": [], "gps": {"lat": 0, "lng": 0}})
        assert r.status_code == 200
        _collect(client, r.json()["job_id"])
        assert captured["gps"] is None
        r = client.post("/advise", json={"query": "q", "history": []})  # gps absent
        assert r.status_code == 200
        _collect(client, r.json()["job_id"])
    assert captured["gps"] is None


def test_advise_returns_widget_json_verbatim(monkeypatch):
    payload = {"status": "success", "request_type": "route",
               "data": {"wkt": "LINESTRING(1 2,3 4)"}, "messages": [{"role": "assistant", "content": "ok"}]}
    client, _ = _client_with_captured_run(monkeypatch, response=payload)
    with client:
        job_id = client.post("/advise", json={"query": "q", "history": []}).json()["job_id"]
        r = _collect(client, job_id)
    assert r.status_code == 200
    assert r.json() == payload  # passthrough, no extra fields — the job id stays in the transport (rule 8)


def test_advise_polls_202_while_the_turn_runs(monkeypatch):
    """The POST must NOT wait for the turn (L47): it returns a job id at once and the result is
    polled out. A turn still running answers 202 with the stage it is in, so no HTTP request
    ever spans the whole turn (the ~60 s proxy cut killed the old single-request design) and
    the chat box can say what is running during the ~30-45 s a bus route costs."""
    payload = {"status": "success", "messages": []}
    release = {"go": False}  # flipped from the test thread; the turn's loop polls it

    async def slow_run_advisor(query, history=None, gps=None, on_stage=None):
        on_stage("routing_bus")  # the graph reports where it is; the poll must echo it back
        while not release["go"]:
            await asyncio.sleep(0.01)
        return payload

    monkeypatch.setattr(api, "run_advisor", slow_run_advisor)
    monkeypatch.setattr(api, "_reset_debug_log", lambda: None)
    monkeypatch.setattr(api, "_log_turn", lambda r: None)
    with TestClient(api.app) as client:
        job_id = client.post("/advise", json={"query": "q", "history": []}).json()["job_id"]

        pending = client.get(f"/advise/{job_id}")
        assert pending.status_code == 202
        body = pending.json()
        assert body["status"] == "pending"
        assert body["stage"] == "routing_bus"  # relayed from the running turn
        assert isinstance(body["elapsed_s"], int)

        release["go"] = True
        done = _collect(client, job_id)
        assert done.status_code == 200 and done.json() == payload  # 200 = widget JSON, verbatim
        # The job is handed over exactly once: a second collect finds nothing.
        assert client.get(f"/advise/{job_id}").status_code == 404


def test_advise_unknown_job_is_404_error_shape(monkeypatch):
    client, _ = _client_with_captured_run(monkeypatch)
    with client:
        r = client.get("/advise/deadbeef")
    assert r.status_code == 404
    assert r.json()["status"] == "error"  # same shape the front-end already renders


def test_advise_prunes_jobs_nobody_collected(monkeypatch):
    """A job whose client went away (tab closed mid-turn) is evicted on the next POST, so the
    table can't grow without bound."""
    client, _ = _client_with_captured_run(monkeypatch)
    # Negative, not 0.0: two back-to-back requests can read the SAME time.monotonic() tick on
    # Windows (15.6 ms resolution), so an age of exactly 0.0 would not be "> TTL".
    monkeypatch.setattr(api, "JOB_TTL_S", -1.0)  # every existing job counts as stale
    with client:
        abandoned = client.post("/advise", json={"query": "q", "history": []}).json()["job_id"]
        client.post("/advise", json={"query": "q2", "history": []})  # this POST prunes it
        assert client.get(f"/advise/{abandoned}").status_code == 404
    assert abandoned not in api._jobs


def test_advise_survives_a_failing_outputs_write(monkeypatch):
    """outputs.txt is a diagnostics file: a failed write must not turn a good turn into a 500
    (the front-end would print "bridge non raggiungibile" for a turn that produced a route)."""
    payload = {"status": "success", "request_type": "route", "data": {}, "messages": []}
    client, _ = _client_with_captured_run(monkeypatch, response=payload)

    def boom(_response):
        raise OSError("disk full")

    monkeypatch.setattr(api, "_log_turn", boom)
    with client:
        job_id = client.post("/advise", json={"query": "q", "history": []}).json()["job_id"]
        r = _collect(client, job_id)
    assert r.status_code == 200 and r.json() == payload
