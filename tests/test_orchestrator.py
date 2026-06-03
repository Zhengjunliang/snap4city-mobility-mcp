"""Unit tests for the agentic graph nodes (orchestrator.py)."""
import json

from snap4city_mobility_mcp.orchestrator import (
    AGENT_SYSTEM,
    _extract_data,
    _last_assistant_text,
    _route_after_agent,
    format_widget,
    understand,
)
from snap4city_mobility_mcp.orchestrator import _build_graph
from snap4city_mobility_mcp.orchestrator import tools as tools_node


def _slots_response(arguments: str) -> dict:
    """A canned OpenAI response whose assistant turn is a forced extract_slots call."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "extract_slots", "arguments": arguments},
                        }
                    ],
                }
            }
        ]
    }


# --- understand --------------------------------------------------------------

async def test_understand_parses_slots(make_llm):
    llm = make_llm([_slots_response(
        '{"intent":"route","origin_text":"Duomo","destination_text":"Santa Croce","mode":"foot_shortest"}'
    )])
    out = await understand(
        {"messages": [{"role": "user", "content": "from Duomo to Santa Croce on foot"}]}, llm=llm
    )
    assert out["intent"] == "route"
    assert out["slots"]["origin_text"] == "Duomo"
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"] == AGENT_SYSTEM


async def test_understand_invalid_args_falls_back(make_llm):
    llm = make_llm([_slots_response("NOT JSON")])
    out = await understand({"messages": [{"role": "user", "content": "hi"}]}, llm=llm)
    assert out["intent"] == "other"


async def test_understand_keeps_existing_system(make_llm):
    llm = make_llm([_slots_response('{"intent":"other"}')])
    msgs = [{"role": "system", "content": "PRE"}, {"role": "user", "content": "hi"}]
    out = await understand({"messages": msgs}, llm=llm)
    assert out["messages"][0]["content"] == "PRE"  # not double-prepended


# --- tools node --------------------------------------------------------------

async def test_tools_node_executes_and_appends(make_client, make_result):
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "tpl_agencies", "arguments": "{}"}}
        ],
    }
    client = make_client([make_result(structured={"agencies": []})])
    out = await tools_node({"messages": [assistant], "tool_results": []}, client=client)
    last = out["messages"][-1]
    assert last["role"] == "tool"
    assert last["tool_call_id"] == "a"
    assert json.loads(last["content"]) == {"agencies": []}
    assert out["tool_results"][0]["name"] == "tpl_agencies"


async def test_tools_node_malformed_arguments(make_client):
    assistant = {
        "role": "assistant",
        "tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "tpl_agencies", "arguments": "{bad"}}
        ],
    }
    client = make_client([])  # must never reach the client
    out = await tools_node({"messages": [assistant], "tool_results": []}, client=client)
    assert "error" in json.loads(out["messages"][-1]["content"])
    assert client.calls == []


# --- _extract_data / format_widget -------------------------------------------

def test_extract_data_route_full_wkt():
    results = [
        {"name": "address_search_location", "result": {"features": []}},
        {"name": "routing", "result": {"journey": {"routes": [
            {"wkt": "LINESTRING(1 2,3 4)", "distance": 0.68, "eta": "10:00:00", "time": "00:10:00"}
        ]}}},
    ]
    data = _extract_data(results)
    assert data["wkt"] == "LINESTRING(1 2,3 4)"
    assert data["distance_km"] == 0.68
    assert data["duration"] == "00:10:00"


def test_extract_data_route_error():
    results = [{"name": "routing", "result": {"error": "no route found (empty routes list)"}}]
    assert _extract_data(results) == {"route_error": "no route found (empty routes list)"}


def test_extract_data_tpl_lines():
    assert _extract_data([{"name": "tpl_lines", "result": [{"uri": "L1"}]}]) == {"lines": [{"uri": "L1"}]}


def test_extract_data_last_success_wins():
    results = [
        {"name": "tpl_agencies", "result": [{"uri": "a"}]},
        {"name": "tpl_lines", "result": [{"uri": "L"}]},
    ]
    assert _extract_data(results) == {"lines": [{"uri": "L"}]}


def test_extract_data_none():
    assert _extract_data([]) == {}


def test_format_widget_preserves_full_wkt():
    long_wkt = "LINESTRING(" + "9 4," * 50 + "1 1)"
    state = {
        "intent": "route",
        "tool_results": [{"name": "routing", "result": {"journey": {"routes": [
            {"wkt": long_wkt, "distance": 1.0}
        ]}}}],
        "messages": [{"role": "assistant", "content": "Done, 1.0 km."}],
    }
    final = format_widget(state)["final"]
    assert final["ok"] is True
    assert final["answer"] == "Done, 1.0 km."
    assert final["data"]["wkt"] == long_wkt  # not truncated to 80 chars
    assert len(final["data"]["wkt"]) > 80


# --- _route_after_agent ------------------------------------------------------

def test_route_after_agent_continues_to_tools():
    state = {"messages": [{"role": "assistant", "tool_calls": [{"id": "x"}]}], "steps": 1}
    assert _route_after_agent(state) == "tools"


def test_route_after_agent_formats_on_final_answer():
    state = {"messages": [{"role": "assistant", "content": "here you go"}], "steps": 1}
    assert _route_after_agent(state) == "format"


def test_route_after_agent_ceiling_overrides():
    state = {"messages": [{"role": "assistant", "tool_calls": [{"id": "x"}]}], "steps": 99}
    assert _route_after_agent(state) == "format"


# --- _last_assistant_text ----------------------------------------------------

def test_last_assistant_text_skips_blank_and_nonassistant():
    msgs = [
        {"role": "assistant", "content": "first"},
        {"role": "tool", "content": "x"},
        {"role": "assistant", "content": "   "},
        {"role": "user", "content": "q"},
    ]
    assert _last_assistant_text(msgs) == "first"


# --- graph wiring ------------------------------------------------------------

def test_graph_compiles(make_client, make_llm):
    """StateGraph.compile() validates node/edge wiring — catches typos statically."""
    graph = _build_graph(make_client([]), make_llm([]), [])
    assert graph is not None
