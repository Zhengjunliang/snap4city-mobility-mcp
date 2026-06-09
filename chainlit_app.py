"""Chainlit chat UI — multi-turn testing front for the mobility advisor.

Replaces the old terminal REPL: a persistent web chat for testing the deterministic
graph on the Snap4City JupyterHub. Each turn calls `run_advisor(query, history)` and
carries the returned `messages` forward in the Chainlit session, so follow-ups ("那坐
公交呢?") resolve against the conversation.

UI shows ONLY the LLM's own reply (`messages[-1].content`) — the `respond` node already
phrased distance/ETA into natural language (and fell back to its own template into the
same slot on LLM error), so nothing is hardcoded here. The FULL output JSON (the widget
payload the dashboard consumes: ok/intent/data-with-WKT/messages) is appended per turn to
`outputs.txt` for offline inspection of the whole flow.

Run (JupyterHub): `chainlit run chainlit_app.py --host 0.0.0.0 --port 8501`, then open
the JupyterHub URL with `/proxy/8501/`. Needs `user_credentials.json` in the repo root
and the intranet MCP server (192.168.1.117:8000) reachable — same as before.
"""
import json
import pathlib

import chainlit as cl

from snap4city_mobility_mcp.orchestrator import run_advisor

OUTPUTS = pathlib.Path("outputs.txt")  # full-output audit log, written in the cwd


def _reply(final: dict) -> str:
    """The LLM's own assistant turn — no hardcoded route formatting.

    `respond` already phrased distance/ETA into natural language (and fell back to its
    own template into messages[-1] on LLM error). The UI just surfaces that text. The
    `✗` line only appears when no final/answer was produced at all (an infra failure,
    e.g. the MCP server was unreachable), where there is no assistant turn to show.
    """
    if not final.get("ok"):
        return f"✗ {final.get('error', 'request failed')}"
    return next(
        (m["content"] for m in reversed(final.get("messages") or [])
         if m.get("role") == "assistant"
         and isinstance(m.get("content"), str) and m["content"].strip()),
        "(no answer)",
    )


def _log_turn(query: str, final: dict) -> None:
    """Append one turn's full output JSON to outputs.txt (inspectable flow log)."""
    block = json.dumps({"query": query, "final": final}, ensure_ascii=False, indent=2)
    with OUTPUTS.open("a", encoding="utf-8") as f:
        f.write(block + "\n" + "=" * 80 + "\n")


@cl.on_chat_start
async def start() -> None:
    cl.user_session.set("history", [])
    await cl.Message(
        content="Snap4City mobility advisor — ask a trip/transport question (Florence/Tuscany)."
    ).send()


@cl.on_message
async def on_message(msg: cl.Message) -> None:
    history = cl.user_session.get("history") or []
    final = await run_advisor(msg.content, history)  # run_advisor is async — await directly
    _log_turn(msg.content, final)  # backend: full JSON → outputs.txt
    await cl.Message(content=_reply(final)).send()  # UI: only the LLM's own words
    cl.user_session.set("history", final.get("messages", history))  # carry multi-turn state
