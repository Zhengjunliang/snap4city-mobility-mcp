"""Geodesic helpers shared by the client graph and the local MCP server.

Both sides measure real-world distances: the orchestrator ranks geocode candidates and
car parks by how far they are from an anchor, and the local server sums a route WKT's
vertex-to-vertex hops. One implementation lives here so the two never drift apart.
"""
import math


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng points."""
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
