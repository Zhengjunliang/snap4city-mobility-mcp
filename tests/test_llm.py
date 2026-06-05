"""Unit tests for the Llama4 client's transient-error retry handling (no network)."""
import httpx
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


def test_is_transient_gateway_wrapped_500():
    """Gateway answers HTTP 200 but the body wraps an upstream vLLM 500 â€” retry it."""
    msg = ("Failed to make POST request to http://192.168.1.13:8080/serve/"
           "llama4-agentic-inference. Error: 500 Server Error: Internal Server Error")
    assert _is_transient(msg, 200)


# --- credential loading (file only, no env) ----------------------------------

def test_load_credentials_reads_file(monkeypatch, tmp_path):
    f = tmp_path / "user_credentials.json"
    f.write_text('{"username": "fileuser", "password": "filepass"}', encoding="utf-8")
    monkeypatch.setenv("S4C_CREDENTIALS_FILE", str(f))
    assert llm_mod._load_credentials() == ("fileuser", "filepass")


def test_load_credentials_missing_raises(monkeypatch):
    monkeypatch.setattr(llm_mod, "_credentials_file", lambda: None)
    with pytest.raises(Llama4Error, match="no user_credentials.json"):
        llm_mod._load_credentials()


def test_load_credentials_incomplete_raises(monkeypatch, tmp_path):
    f = tmp_path / "user_credentials.json"
    f.write_text('{"username": "u"}', encoding="utf-8")  # password absent
    monkeypatch.setenv("S4C_CREDENTIALS_FILE", str(f))
    with pytest.raises(Llama4Error, match="missing 'username' or 'password'"):
        llm_mod._load_credentials()


def test_client_explicit_creds_skip_file(monkeypatch):
    """Explicit username/password bypass the credentials file entirely."""
    def _boom():
        raise AssertionError("file should not be read when creds are explicit")

    monkeypatch.setattr(llm_mod, "_load_credentials", _boom)
    Llama4Client(username="u", password="p")  # must not raise


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
