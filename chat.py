"""Terminal chat REPL for the mobility advisor — multi-turn testing on the JupyterHub.

`python chat.py` opens an interactive chat: type a question, get the advisor's reply,
keep chatting (follow-ups like "那坐公交呢?" resolve against history). Empty line quits.

Shows ONLY the LLM's own reply (`messages[-1].content`) — the `respond` node already
phrased distance/ETA into natural language (and fell back to its own template into the
same slot on LLM error), so nothing is hardcoded here. The FULL output JSON the dashboard
consumes (ok/intent/data-with-WKT/messages) is appended per turn to `outputs.txt` for
offline inspection of the whole flow.

Why a terminal script (not a web UI): the only runtime is the Snap4City JupyterHub, where
exposing a web port needs jupyter-server-proxy and is fragile (see docs/lessons.md L14) — a
terminal REPL just works. Needs `user_credentials.json` in the repo root and the intranet
MCP server (192.168.1.117:8000) reachable.
"""
import asyncio
import json
import pathlib

from snap4city_mobility_mcp.orchestrator import run_advisor

OUTPUTS = pathlib.Path("outputs.txt")  # full-output audit log, written in the cwd


def _reply(final: dict) -> str:
    """The LLM's own assistant turn — no hardcoded route formatting.

    `respond` already phrased distance/ETA into natural language (and fell back to its
    own template into messages[-1] on LLM error). The `✗` line only appears when no
    final/answer was produced at all (an infra failure, e.g. the MCP server was
    unreachable), where there is no assistant turn to show.
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


async def main() -> None:
    history: list[dict] = []
    print("Snap4City mobility advisor — ask a trip/transport question (empty line to quit).")
    while True:
        try:
            query = input("🧑 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query:
            break
        final = await run_advisor(query, history)
        _log_turn(query, final)  # backend: full JSON → outputs.txt
        history = final.get("messages", history)  # carry multi-turn state
        print("🤖", _reply(final), "\n")  # UI: only the LLM's own words


if __name__ == "__main__":
    asyncio.run(main())
