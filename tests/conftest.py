"""Shared in-memory fakes for the local mock suite (no network / LLM / MCP)."""
from types import SimpleNamespace

import pytest


class FakeResult:
    """Stand-in for fastmcp Client.call_tool() result (structured_content or content[].text)."""

    def __init__(self, structured=None, text=None):
        self.structured_content = structured
        self.content = [SimpleNamespace(text=text)] if text is not None else []


class FakeTool:
    """Stand-in for an MCP Tool returned by Client.list_tools()."""

    def __init__(self, name, description="", inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema if inputSchema is not None else {"type": "object", "properties": {}}


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
    """Queue-driven Llama4Client double for the understand / agent nodes."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def achat(self, messages, *, tools=None, tool_choice=None, temperature=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return self._responses.pop(0)


@pytest.fixture
def pt_arcs():
    """Mixed walk+bus arcs of a public-transport journey — the ONE shared PT
    fixture exercised through both detection paths (orchestrator._extract_data
    gates on args.routetype, mcp_tools.slim_result_for_llm on the leg count) so
    they cannot silently diverge. Shape is provisional until the first live PT
    run calibrates it (see group_arc_legs docstring)."""
    return [
        {"desc": "Via Panzani", "distance": 0.2, "transport": "foot",
         "transport_provider": "private", "start_datetime": "10:00:00", "end_datetime": "10:03:00"},
        {"desc": "nd", "distance": 0.1, "transport": "foot",
         "transport_provider": "private", "start_datetime": "10:03:00", "end_datetime": "10:05:00"},
        {"desc": "Stazione SMN", "distance": 1.5, "transport": "bus",
         "transport_provider": "Linea 6", "start_datetime": "10:06:00", "end_datetime": "10:16:00"},
        {"desc": "Piazza Dalmazia", "distance": 1.0, "transport": "bus",
         "transport_provider": "Linea 6", "start_datetime": "10:16:00", "end_datetime": "10:22:00"},
        {"desc": "Via Reginaldo Giuliani", "distance": 0.3, "transport": "foot",
         "transport_provider": "private", "start_datetime": "10:22:00", "end_datetime": "10:26:00"},
    ]


@pytest.fixture
def make_result():
    return FakeResult


@pytest.fixture
def make_client():
    return FakeClient


@pytest.fixture
def make_tool():
    return FakeTool


@pytest.fixture
def make_llm():
    return FakeLLM
