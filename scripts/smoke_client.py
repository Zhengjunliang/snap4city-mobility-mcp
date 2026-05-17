"""Smoke test: connect to the local FastMCP HTTP server and call both tools."""

import asyncio

from fastmcp import Client

# Note: FastMCP HTTP transport default path is /mcp/ (trailing slash).
# /mcp (no slash) also works via redirect, but using /mcp/ avoids the redirect hop.
SERVER_URL = "http://localhost:8000/mcp/"


async def main() -> None:
    async with Client(SERVER_URL) as c:
        greet_result = await c.call_tool("greet", {"name": "Junliang"})
        print("greet ->", greet_result)

        route_result = await c.call_tool(
            "fake_route",
            {"origin": "S.Marta", "destination": "Duomo", "mode": "walk"},
        )
        print("fake_route ->", route_result)


if __name__ == "__main__":
    asyncio.run(main())
