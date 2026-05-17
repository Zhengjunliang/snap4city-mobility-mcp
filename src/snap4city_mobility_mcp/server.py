from typing import Literal

from fastmcp import FastMCP
from pydantic import BaseModel

mcp = FastMCP("snap4city-mobility-mcp")


@mcp.tool
def greet(name: str) -> str:
    """Sanity check tool — returns a greeting."""
    return f"Ciao {name}!"


class RouteOption(BaseModel):
    mode: Literal["walk", "tpl", "car", "bike"]
    duration_min: int
    summary: str


@mcp.tool
def fake_route(
    origin: str,
    destination: str,
    mode: Literal["walk", "tpl", "car"],
) -> RouteOption:
    """Schema-validation tool — proves Literal + Pydantic return type generates correct MCP schema. NOT real routing."""
    return RouteOption(
        mode=mode,
        duration_min=42,
        summary=f"{origin} -> {destination} via {mode}",
    )


def main() -> None:
    """Console-script entry point (configured in pyproject.toml [project.scripts])."""
    mcp.run()


if __name__ == "__main__":
    main()
