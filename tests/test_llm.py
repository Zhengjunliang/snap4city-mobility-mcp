"""Unit tests for the Llama4 client's transient-error retry handling (no network).

Lean core suite: the gateway-wrapped 500 (L12 — HTTP 200 body hiding an upstream
vLLM error must be retried), the credentials-file contract, and chat() retry vs
hard-error (L10 — "Rule not found" is auth-level, never retried).
"""
import pytest

from snap4city_mobility_mcp import llm as llm_mod
from snap4city_mobility_mcp.llm import (
    Llama4Client,
    Llama4Error,
    _is_transient,
)


class _Resp:
    """Minimal httpx.Response stand-in: .json() + .status_code + .text."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never actually back off during tests."""
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def client(monkeypatch):
    """A Llama4Client whose token fetch is stubbed (no auth round-trip)."""
    c = Llama4Client(username="u", password="p")
    monkeypatch.setattr(c._tm, "get_token", lambda: "tok")
    return c


def _queue_posts(monkeypatch, client, responses):
    """Patch the client's HTTP post to pop canned responses/exceptions in order."""
    calls = []

    def fake_post(url, **kw):
        calls.append(kw)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(client._client, "post", fake_post)
    return calls


# --- _is_transient (L12) -----------------------------------------------------

def test_is_transient_gateway_wrapped_500():
    """Gateway answers HTTP 200 but the body wraps an upstream vLLM 500 -> retry it."""
    msg = ("Failed to make POST request to http://192.168.1.13:8080/serve/"
           "llama4-agentic-inference. Error: 500 Server Error: Internal Server Error")
    assert _is_transient(msg, 200)


# --- credential loading (file only, no env) ----------------------------------

def test_load_credentials_missing_raises(monkeypatch):
    monkeypatch.setattr(llm_mod, "_credentials_file", lambda: None)
    with pytest.raises(Llama4Error, match="no user_credentials.json"):
        llm_mod._load_credentials()


# --- chat() retry behavior ---------------------------------------------------

def test_chat_retries_then_succeeds(client, monkeypatch):
    ok = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    calls = _queue_posts(monkeypatch, client, [
        _Resp({"message": "The upstream server is timing out"}),
        _Resp(ok),
    ])
    out = client.chat([{"role": "user", "content": "x"}])
    assert out == ok
    assert len(calls) == 2  # retried once, then succeeded


def test_chat_hard_error_not_retried(client, monkeypatch):
    # L10: "Rule not found" is an auth/authorization failure, not transient — no retry.
    calls = _queue_posts(monkeypatch, client, [
        _Resp({"message": "Rule not found for this user/path"}),
    ])
    with pytest.raises(Llama4Error, match="Rule not found"):
        client.chat([{"role": "user", "content": "x"}])
    assert len(calls) == 1  # hard error: no retry
