"""Geodesic + WKT helpers shared by the client graph and the local MCP server.

Both sides measure real-world distances: the orchestrator ranks geocode candidates and
car parks by how far they are from an anchor, and the local server sums a route WKT's
vertex-to-vertex hops. The WKT LINESTRING parse/format pair lives here too because both
the route tool (leg slicing) and the GTFS shape enhancer (gtfs_shapes) read and write
the same geometry strings. One implementation so the sides never drift apart.
"""
import math


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng points."""
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def wkt_points(wkt: str) -> list[tuple[float, float]] | None:
    """(lng, lat) vertex list of a 'LINESTRING (lng lat, lng lat, ...)', or None.

    Vertex indices match the What-If router instructions' `interval` fields
    (live-verified: the last instruction's interval names the last vertex, and a ride
    interval's endpoints land on the board/alight stops) — which is what lets
    mcp_server._leg_slices cut the geometry. Also parses the km4city tpl API's
    space-free 'LINESTRING(...)' variant.
    """
    lo, hi = wkt.find("("), wkt.rfind(")")
    if lo < 0 or hi <= lo:
        return None
    pts: list[tuple[float, float]] = []
    for pair in wkt[lo + 1 : hi].split(","):
        xy = pair.split()
        if len(xy) >= 2:
            try:
                pts.append((float(xy[0]), float(xy[1])))
            except ValueError:
                continue
    return pts or None


def wkt_length_km(wkt: str) -> float | None:
    """Total geodesic length (km) of a 'LINESTRING (lng lat, lng lat, ...)'.

    The What-If router's paths[0].distance counts only the walking-access metres — a GTFS
    transit leg's in-vehicle ride contributes 0 to it — so the real door-to-door distance
    is recovered by measuring the full drawn geometry instead (L35). None when the WKT
    can't be parsed.
    """
    pts = wkt_points(wkt)
    if pts is None or len(pts) < 2:
        return None
    total = sum(
        haversine_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:])
    )
    return round(total, 3)


def fmt_linestring(pts: list[tuple[float, float]]) -> str:
    """'LINESTRING (lng lat, ...)' from a (lng, lat) vertex list (router WKT shape)."""
    return "LINESTRING (" + ", ".join(f"{lng} {lat}" for lng, lat in pts) + ")"
