"""Shared in-memory fakes for the local mock suite (no network / LLM / MCP)."""
from types import SimpleNamespace

import pytest

from snap4city_mobility_mcp.gtfs_shapes import reset_caches as gtfs_shapes_reset
from snap4city_mobility_mcp.mcp_tools import geocode_cache_clear


@pytest.fixture(autouse=True)
def _clean_geocode_cache():
    """Empty the process-wide caches (geocode + tpl shapes) before every test.

    Mandatory, not hygiene: the caches outlive a test, while the fakes are single FIFO
    queues of responses. A cached search would consume no queued response, so every later
    pop in that test would shift by one — a silent, confusing failure (a routing call would
    receive a FeatureCollection). Several tests already geocode the same place text
    ("Duomo, Firenze" appears in five); the gtfs_shapes lines index has the same trap.
    Before-only: the next test's own clear covers whatever this one leaves behind."""
    geocode_cache_clear()
    gtfs_shapes_reset()


class FakeResult:
    """Stand-in for fastmcp Client.call_tool() result (structured_content or content[].text)."""

    def __init__(self, structured=None, text=None):
        self.structured_content = structured
        self.content = [SimpleNamespace(text=text)] if text is not None else []


class FakeClient:
    """Queue-driven fastmcp Client double; records calls, pops responses in order."""

    def __init__(self, responses=(), tools=()):
        self._responses = list(responses)
        self._tools = list(tools)
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if not self._responses:
            raise AssertionError(f"unexpected call_tool({name!r})")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def list_tools(self):
        return list(self._tools)


class FakeLLM:
    """Queue-driven Llama4Client double for the understand / respond nodes."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def achat(self, messages, *, tools=None, tool_choice=None, temperature=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return self._responses.pop(0)


@pytest.fixture
def make_result():
    return FakeResult


@pytest.fixture
def make_client():
    """FakeClient factory that asserts every queued response was consumed at teardown.

    Mandatory guard, not hygiene: exec_tool converts ANY failure — including FakeClient's
    own empty-queue AssertionError — into an {"error": ...} payload, so a queue/call
    misalignment does not fail the test by itself; it silently degrades the flow under
    test (a geocode 'succeeds' with an error dict). Leftover responses are the visible
    symptom, so they fail the test here."""
    created = []

    def factory(responses=(), tools=()):
        c = FakeClient(responses, tools)
        created.append(c)
        return c

    yield factory
    leftover = [c._responses for c in created if c._responses]
    assert not leftover, f"unconsumed FakeClient responses: {leftover}"


@pytest.fixture
def make_llm():
    return FakeLLM
