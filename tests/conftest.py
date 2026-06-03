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
