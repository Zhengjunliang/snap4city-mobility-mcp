"""Direct geocode probe — bypass the orchestrator graph, A/B the lang/logic knobs.

Run on JupyterHub (s4c env): python scripts/probe_geocode.py
`address_search_location` / `coordinates_to_address` are public backends (auth dropped) →
no LLM creds needed.

Why this exists: chat addresses land in the wrong place (e.g. "Giardino Niccolò Galli"
geocoded to the airport edge, off the walkable graph → route draw crashed). The native
Snap4City What-If widget is accurate because it starts from a precise clicked coordinate or
a street+civic picked from a dropdown, not a fuzzy auto-resolved name. This probe runs the
REAL geocode pipeline (geocode_with_retry: two-pass + bbox + _narrow_by_city + retry) under
each (lang, logic) combo, prints the coordinate _pick_coord chooses, then REVERSE-geocodes
that point back to a street so you can see whether the pick is sane. It also dumps the raw
server shape for one query (forward returns ALL Tuscany ranked by score — the named city is
NOT first, so _narrow_by_city is what must pull Florence to the front).
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp import mcp_tools as m
from snap4city_mobility_mcp.mcp_tools import (
    _build_config,
    _unwrap,
    geocode_with_retry,
    reverse_geocode,
)
from snap4city_mobility_mcp.orchestrator import _pick_coord

# Mix of the chat-failing POIs, known-good central landmarks, and a What-If-style
# street+civic (the input shape that geocodes precisely).
QUERIES = [
    "Piazza del Duomo, Firenze",
    "Santa Croce, Firenze",
    "Giardino Niccolò Galli, Firenze",
    "scuola primaria Giosuè Carducci, Firenze",
    "Via Zara 3, Firenze",
    "stazione di Santa Maria Novella, Firenze",
]
# (lang, logic) matrix: the old default vs the new bias, plus the stricter logic to A/B.
COMBOS = [("en", "or"), ("it", "or"), ("it", "and")]

# A known point for the reverse-geocode sanity check: What-If returned "3, VIA ZARA, FIRENZE".
REVERSE_PROBE = (43.781834, 11.25891)


def _rev_addr(payload) -> str:
    """First street/address out of coordinates_to_address's {"result": [...]} shape."""
    if isinstance(payload, dict):
        res = payload.get("result")
        if isinstance(res, list) and res and isinstance(res[0], dict):
            r = res[0]
            return f"{r.get('address')}, {r.get('municipality')}"
    return str(payload)[:120]


async def _pick(client: Client, search: str, lang: str, logic: str):
    """Drive the REAL pipeline under the given knobs (module globals are read at call time),
    returning (coord, status) where status explains a None pick."""
    m.GEOCODE_LANG, m.GEOCODE_LOGIC = lang, logic
    res = await geocode_with_retry(client, {"search": search})
    if isinstance(res, dict) and "error" in res:
        return None, f"error: {res['error'][:80]}"
    coord = _pick_coord(res, search)
    if coord is None:
        n = len(res.get("features", [])) if isinstance(res, dict) else "?"
        return None, f"no pick (type={res.get('type') if isinstance(res, dict) else type(res)}, features={n})"
    return coord, "ok"


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        print("TOOLS:", [t.name for t in await client.list_tools()], flush=True)

        # Reverse-geocode sanity check (the near-me foundation + coordinate→address validator).
        lat, lng = REVERSE_PROBE
        rev = await reverse_geocode(client, lat, lng)
        print(f"\n### reverse_geocode({lat}, {lng}) ### -> {_rev_addr(rev)}", flush=True)

        # Raw forward shape for one query: confirms the property shape our parser expects and
        # shows the all-Tuscany score ranking (why _narrow_by_city is needed).
        raw = _unwrap(await client.call_tool(
            "address_search_location",
            {"search": "Via Zara 3, Firenze", "excludePOI": True, "lang": "it", "logic": "or"},
        ))
        print("\n### RAW address_search_location('Via Zara 3, Firenze', excludePOI=true) ###", flush=True)
        if isinstance(raw, dict):
            print("keys:", list(raw), "count:", raw.get("count"), flush=True)
            for f in (raw.get("features") or [])[:5]:
                p = f.get("properties", {})
                print(f"  {f.get('geometry', {}).get('coordinates')}  "
                      f"addr={p.get('address')!r} civic={p.get('civic')!r} city={p.get('city')!r} "
                      f"score={p.get('score')!r}", flush=True)
        else:
            print(json.dumps(raw, ensure_ascii=False)[:800], flush=True)

        for q in QUERIES:
            print(f"\n### {q!r} ###", flush=True)
            for lang, logic in COMBOS:
                try:
                    coord, status = await asyncio.wait_for(_pick(client, q, lang, logic), timeout=40)
                except Exception as e:  # noqa: BLE001 — diagnostic script, surface anything
                    print(f"  lang={lang} logic={logic}: [EXC] {type(e).__name__}: {e}", flush=True)
                    continue
                if not coord:
                    print(f"  lang={lang} logic={logic}: {status}", flush=True)
                    continue
                # coord is [lng, lat]; reverse it to show what street the pick lands on.
                back = await reverse_geocode(client, coord[1], coord[0])
                print(f"  lang={lang} logic={logic}: {coord} -> {_rev_addr(back)}", flush=True)
    print("\n>>> DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
