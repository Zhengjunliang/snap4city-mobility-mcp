"""Console-script entry for snap4city-mobility-cli.

Self-contained CLI wrapper that owns argparse + asyncio + json output. Logic
delegates to orchestrator.run_trip(); orchestrator stays a pure library module
(no argparse / __main__ block).
"""
import argparse
import asyncio
import json

from snap4city_mobility_mcp.orchestrator import run_trip


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="snap4city-mobility-cli")
    p.add_argument("origin")
    p.add_argument("destination")
    p.add_argument(
        "route_type",
        nargs="?",
        default="foot_shortest",
        choices=["public_transport", "foot_shortest", "foot_quiet", "car"],
    )
    return p.parse_args()


async def cli_main(origin: str, destination: str, route_type: str) -> None:
    final = await run_trip(origin, destination, route_type)  # type: ignore[arg-type]
    print(json.dumps(final, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    asyncio.run(cli_main(args.origin, args.destination, args.route_type))


if __name__ == "__main__":
    main()
