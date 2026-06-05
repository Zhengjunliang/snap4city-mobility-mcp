"""Console-script entry for snap4city-mobility-cli.

Drives the agentic advisor. With a query on the command line it runs one shot;
with no arguments it opens an interactive multi-turn REPL that carries the
conversation forward (so follow-ups like "那坐公交呢?" resolve against history).
Logic delegates to orchestrator.run_advisor(); orchestrator stays a pure library
module (no argparse / __main__ block).

Output is a short human summary by default (the LLM answer + the grounded route
numbers). Pass `--json` to dump the full widget payload (incl. `data` WKT and the
multi-turn `messages`) — that raw shape is what the dashboard consumes, not humans.
"""
import asyncio
import json
import sys

from snap4city_mobility_mcp.orchestrator import run_advisor


def _summary(final: dict) -> str:
    """One short human-readable block: the answer plus a grounded route line."""
    if not final.get("ok"):
        return f"✗ {final.get('error', 'request failed')}"
    lines: list[str] = []
    answer = (final.get("answer") or "").strip()
    if answer:
        lines.append(answer)
    data = final.get("data") or {}
    if data.get("distance_km") is not None:  # route: show the numbers the tool returned
        bits = [f"{data['distance_km']} km"]
        if data.get("duration"):
            bits.append(f"~{data['duration']}")
        if data.get("eta"):
            bits.append(f"arrivo {data['eta']}")
        lines.append("📍 " + " · ".join(bits))
    elif data.get("route_error"):
        lines.append(f"⚠ {data['route_error']}")
    return "\n".join(lines) or "(no answer)"


def _show(final: dict, *, as_json: bool) -> None:
    print(json.dumps(final, ensure_ascii=False, indent=2) if as_json else _summary(final))


async def _one_shot(query: str, *, as_json: bool) -> None:
    _show(await run_advisor(query), as_json=as_json)


async def _repl(*, as_json: bool) -> None:
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
        _show(final, as_json=as_json)
        history = final.get("messages", history)  # carry updated multi-turn state


def main() -> None:
    args = sys.argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if args:  # one-shot: remaining args joined as a single NL query
        asyncio.run(_one_shot(" ".join(args), as_json=as_json))
    else:
        asyncio.run(_repl(as_json=as_json))


if __name__ == "__main__":
    main()
