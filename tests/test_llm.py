"""Unit tests for the Llama4 client's transient-error retry handling (no network)."""
import httpx
import pytest

from snap4city_mobility_mcp import llm as llm_mod
from snap4city_mobility_mcp.llm import Llama4Client, Llama4Error, _is_transient


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


def _queue_posts(monkeypatch, responses):
    """Patch httpx.post to pop canned responses/exceptions in order; record each call."""
    calls = []

    def fake_post(url, **kw):
        calls.append(kw)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    return calls


# --- _is_transient -----------------------------------------------------------

def test_is_transient_status_codes():
    assert _is_transient(None, 502)
    assert _is_transient(None, 503)
    assert _is_transient(None, 429)
    assert not _is_transient(None, 200)
    assert not _is_transient(None, 404)


def test_is_transient_message_hints():
    assert _is_transient("The upstream server is timing out", 200)
    assert _is_transient("Service temporarily unavailable", 200)
    assert _is_transient("Bad gateway", 200)
    assert not _is_transient("Rule not found for this user/path", 200)
    assert not _is_transient(None, 200)


# --- chat() retry behavior ---------------------------------------------------

def test_chat_retries_then_succeeds(client, monkeypatch):
    ok = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    calls = _queue_posts(monkeypatch, [
        _Resp({"message": "The upstream server is timing out"}),
        _Resp(ok),
    ])
    out = client.chat([{"role": "user", "content": "x"}])
    assert out == ok
    assert len(calls) == 2  # retried once, then succeeded


def test_chat_hard_error_not_retried(client, monkeypatch):
    calls = _queue_posts(monkeypatch, [
        _Resp({"message": "Rule not found for this user/path"}),
    ])
    with pytest.raises(Llama4Error, match="Rule not found"):
        client.chat([{"role": "user", "content": "x"}])
    assert len(calls) == 1  # hard error: no retry


def test_chat_exhausts_retries_on_transient(client, monkeypatch):
    calls = _queue_posts(monkeypatch, [
        _Resp({"message": "upstream timing out"}),
        _Resp({"message": "upstream timing out"}),
        _Resp({"message": "upstream timing out"}),
    ])
    with pytest.raises(Llama4Error, match="timing out"):
        client.chat([{"role": "user", "content": "x"}])
    assert len(calls) == 3  # 1 initial + LLM_RETRIES (2)


def test_chat_retries_network_error(client, monkeypatch):
    ok = {"choices": [{"message": {"content": "ok"}}]}
    calls = _queue_posts(monkeypatch, [
        httpx.ConnectError("connection reset"),
        _Resp(ok),
    ])
    out = client.chat([{"role": "user", "content": "x"}])
    assert out == ok
    assert len(calls) == 2
