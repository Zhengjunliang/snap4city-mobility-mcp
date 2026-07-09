"""Unit tests for the FastAPI bridge (api.py): gps sanitization + verbatim passthrough.

run_advisor is monkeypatched (no LLM/MCP): the tests assert what the bridge forwards
(query, history, sanitized gps) and that the widget JSON comes back untouched (rule 8).
api.py lives at the repo root (not in the src package), so the root goes on sys.path.
"""
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

    async def fake_run_advisor(query, history=None, gps=None):
        captured.update({"query": query, "history": history, "gps": gps})
        return response if response is not None else {"status": "success", "messages": []}

    monkeypatch.setattr(api, "run_advisor", fake_run_advisor)
    monkeypatch.setattr(api, "_reset_debug_log", lambda: None)  # no debug.log churn in tests
    monkeypatch.setattr(api, "_log_turn", lambda r: None)       # no outputs.txt churn in tests
    return TestClient(api.app), captured


def test_advise_forwards_sanitized_gps(monkeypatch):
    client, captured = _client_with_captured_run(monkeypatch)
    r = client.post("/advise", json={"query": "portami al Duomo", "history": [],
                                     "gps": {"lat": 43.7731, "lng": 11.2558}})
    assert r.status_code == 200
    assert captured["gps"] == {"lat": 43.7731, "lng": 11.2558}
    assert captured["query"] == "portami al Duomo"


def test_advise_invalid_gps_degrades_to_none_not_422(monkeypatch):
    client, captured = _client_with_captured_run(monkeypatch)
    r = client.post("/advise", json={"query": "q", "history": [], "gps": {"lat": 0, "lng": 0}})
    assert r.status_code == 200
    assert captured["gps"] is None
    r = client.post("/advise", json={"query": "q", "history": []})  # gps absent
    assert r.status_code == 200
    assert captured["gps"] is None


def test_advise_returns_widget_json_verbatim(monkeypatch):
    payload = {"status": "success", "request_type": "route",
               "data": {"wkt": "LINESTRING(1 2,3 4)"}, "messages": [{"role": "assistant", "content": "ok"}]}
    client, _ = _client_with_captured_run(monkeypatch, response=payload)
    r = client.post("/advise", json={"query": "q", "history": []})
    assert r.json() == payload  # passthrough, no extra fields (rule 8)
