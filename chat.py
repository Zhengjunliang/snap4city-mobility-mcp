"""Terminal chat REPL for the mobility advisor, for multi-turn testing on the JupyterHub.

`python chat.py` opens an interactive chat: type a question, get the advisor's reply,
keep chatting (follow-ups like "那坐公交呢?" resolve against history). Empty line quits.

Shows only the LLM's own reply (messages[-1].content): the respond node already phrased
distance/ETA into natural language (and falls back to its own template into the same slot
on LLM error), so nothing is hardcoded here. The full output JSON the dashboard consumes
(status/request_type/data-with-WKT/messages) is appended per turn to outputs.txt for
offline inspection; both outputs.txt and debug.log are reset at each session start.

A terminal script rather than a web UI because the only runtime is the JupyterHub, where
exposing a web port needs jupyter-server-proxy and is fragile. Needs user_credentials.json
in the repo root and the intranet MCP server (192.168.1.117:8000) reachable.
"""
import asyncio
import json
import logging
import pathlib
import sys

from snap4city_mobility_mcp.orchestrator import run_advisor

OUTPUTS = pathlib.Path("outputs.txt")  # full-output audit log, written in the cwd
DEBUG_LOG = "debug.log"  # tool-level diagnostics (geocode coords, raw routing payloads)


def _setup_debug_log() -> None:
    """Route the package's DEBUG diagnostics to debug.log (file only, so the REPL
    stays clean). Only this package's logger is touched, so httpx etc. stay quiet.
    Idempotent: re-running main() in the same process must not stack handlers."""
    pkg_logger = logging.getLogger("snap4city_mobility_mcp")
    if pkg_logger.handlers:
        return
    # mode="w": each chat session starts a fresh log, no unbounded accumulation.
    handler = logging.FileHandler(DEBUG_LOG, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    pkg_logger.setLevel(logging.DEBUG)
    pkg_logger.addHandler(handler)
    pkg_logger.propagate = False  # never echo DEBUG payloads to a root/notebook handler


def _reply(final: dict) -> str:
    """The LLM's own assistant turn, with no hardcoded route formatting.

    respond already phrased distance/ETA into natural language (and falls back to its
    own template into messages[-1] on LLM error). The "✗" line only appears when no
    final answer was produced at all (an infra failure, e.g. the MCP server was
    unreachable), where there is no assistant turn to show.
    """
    if final.get("status") != "success":
        return f"✗ {final.get('error', 'request failed')}"
    return next(
        (m["content"] for m in reversed(final.get("messages") or [])
         if m.get("role") == "assistant"
         and isinstance(m.get("content"), str) and m["content"].strip()),
        "(no answer)",
    )


def _log_turn(final: dict) -> None:
    """Append one turn's full output JSON to outputs.txt (inspectable flow log).

    Writes the dashboard payload as-is, with no query/final wrapper. The current
    turn's query already lives in `final.messages[-2].content` (the last user turn)."""
    block = json.dumps(final, ensure_ascii=False, indent=2)
    with OUTPUTS.open("a", encoding="utf-8") as f:
        f.write(block + "\n" + "=" * 80 + "\n")


async def main() -> None:
    _setup_debug_log()
    OUTPUTS.write_text("", encoding="utf-8")  # fresh audit log per chat session
    # Terminal streams aren't always clean UTF-8: accented input/paste on the
    # JupyterHub terminal crashed input() with UnicodeDecodeError (stdin, decode),
    # and legacy cp1252 consoles can't encode the emoji prompt (stdout, encode).
    # Replace unmappable characters instead of raising on either side.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    history: list[dict] = []
    print("Snap4City mobility advisor: ask a trip/transport question (empty line to quit).")
    while True:
        try:
            query = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        except UnicodeDecodeError:  # defensive: stdin without reconfigure support
            print("⚠ input encoding error, please retype the question.")
            continue
        if not query:
            break
        try:
            final = await run_advisor(query, history)
        except Exception as e:  # infra failure (MCP/LLM unreachable): keep the REPL alive
            final = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        _log_turn(final)  # backend: full JSON → outputs.txt
        history = final.get("messages", history)  # carry multi-turn state
        print("✦", _reply(final), "\n")  # UI: only the LLM's own words


if __name__ == "__main__":
    asyncio.run(main())
