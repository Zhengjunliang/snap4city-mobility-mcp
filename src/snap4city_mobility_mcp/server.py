from typing import Annotated, Any, Dict, Optional
from urllib.parse import urlencode

from fastmcp import FastMCP
from pydantic import Field

from snap4city_mobility_mcp._helpers import (
    _describe_payload,
    _safe_get,
    create_error,
    create_success,
)

mcp = FastMCP("snap4city-mobility-mcp")


@mcp.tool(name="locations", tags={"locations"}, meta={"tags": ["locations"]})
async def locations(
    search: Annotated[str, Field(description="Free-text address / municipality / street keywords. Tuscany-only.")],
    max_results: Annotated[int, Field(default=10, ge=1, le=50, description="Cap on returned features.")] = 10,
    authentication: Annotated[Optional[str], Field(default=None, description="Bearer token for authorization if required.")] = None,
) -> Dict[str, Any]:
    """
    Fuzzy-search Snap4City Tuscany location index. Wraps GET /location/?excludePOI=true.

    Returns GeoJSON Feature objects (coordinates as [lng, lat], properties.name as label).
    Region-locked to Tuscany - out-of-region queries silently fall back to the nearest in-region match.
    Pure-noise input may surface as an HTTP 500 reported in the envelope's 'error' field.
    """
    try:
        qs = urlencode({"search": search, "maxResults": max_results, "excludePOI": "true"})
        request_url = f"https://www.snap4city.org/superservicemap/api/v1/location/?{qs}"
        headers = {"Connection": "close"}
        if authentication:
            headers["Authorization"] = f"Bearer {authentication}"
        payload, err, http_meta = await _safe_get(request_url, headers, timeout=60.0)
        if err:
            return create_error(err, meta=http_meta)
        features = payload.get("features", [])
        desc = _describe_payload(payload, hint="locations")
        return create_success(features, total=len(features), meta=desc)
    except Exception as e:
        return create_error(f"Internal Tool Error: {type(e).__name__}: {e}")


def main() -> None:
    """Console-script entry point (configured in pyproject.toml [project.scripts])."""
    mcp.run()


if __name__ == "__main__":
    main()
