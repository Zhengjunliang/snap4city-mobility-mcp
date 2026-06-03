"""Console-script entry for snap4city-mobility-cli.

Drives the agentic advisor. With a query on the command line it runs one shot;
with no arguments it opens an interactive multi-turn REPL that carries the
conversation forward (so follow-ups like "那坐公交呢?" resolve against history).
Logic delegates to orchestrator.run_advisor(); orchestrator stays a pure library
module (no argparse / __main__ block).
"""
import asyncio
import json
import sys

from snap4city_mobility_mcp.orchestrator import run_advisor


def _show(final: dict) -> None:
    print(json.dumps(final, ensure_ascii=False, indent=2))


async def _one_shot(query: str) -> None:
    _show(await run_advisor(query))


async def _repl() -> None:
    history: list[dict] = []
    print("Snap4City mobility advisor — ask a trip/transport question (empty line or Ctrl-D to quit).")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            break
        final = await run_advisor(line, history)
        _show(final)
        history = final.get("messages", history)  # carry updated multi-turn state


def main() -> None:
    if len(sys.argv) > 1:  # one-shot: remaining args joined as a single NL query
        asyncio.run(_one_shot(" ".join(sys.argv[1:])))
    else:
        asyncio.run(_repl())


if __name__ == "__main__":
    main()
