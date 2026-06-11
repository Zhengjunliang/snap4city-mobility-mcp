"""Direct geocode probe — see what address_search_location returns for concrete address
strings, raw backend vs the client's 2-pass (address-first + Tuscany bbox + city ladder).

Run on JupyterHub (s4c env): python scripts/probe_geocode.py

Purpose: the understand "institution → street" rule assumes a STREET name ("Viale Morgagni,
Firenze") geocodes to a Tuscan point while the institution string ("Università di Firenze,
Viale Morgagni") returns foreign junk. This probe confirms that per-string before trusting
the LLM to do the rewrite. address_search_location is a public backend → no LLM creds needed.
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import _build_config, _geocode_address_first, _unwrap

# Edit this list to test whatever concrete addresses you want.
QUERIES = [
    "Università di Firenze, Viale Giovan Battista Morgagni, Firenze",  # turn5: institution+street -> failed
    "Viale Morgagni, Firenze",                                         # target street -> should hit
    "Viale Giovan Battista Morgagni, Firenze",                         # full street name
    "Dipartimento di Morgagni, Firenze",                              # turn6: bare institution -> wrong centro POI
    "Stazione di Firenze Rifredi",                                     # origin -> worked
    "Piazza del Duomo, Firenze",                                       # known-good baseline
]


def _top(payload, n=3):
    """Compact view: an error string, or count + top-n {city, address, coords[lng,lat]}."""
    if not isinstance(payload, dict):
        return repr(payload)[:200]
    if "error" in payload:
        return {"error": payload["error"]}
    feats = payload.get("features") or []
    top = [
        {
            "city": (f.get("properties") or {}).get("city"),
            "address": (f.get("properties") or {}).get("address"),
            "coords": (f.get("geometry") or {}).get("coordinates"),
        }
        for f in feats[:n]
    ]
    return {"count": payload.get("count"), "top": top}


async def main() -> None:
    print(">>> connecting ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        for q in QUERIES:
            print(f"\n=== {q!r} ===", flush=True)
            # 1. raw backend, address pass (excludePOI=true) — UNFILTERED, shows foreign junk
            try:
                raw = _unwrap(await client.call_tool(
                    "address_search_location", {"search": q, "excludePOI": True}))
                print("  raw address-pass:", json.dumps(_top(raw), ensure_ascii=False), flush=True)
            except Exception as e:  # noqa: BLE001 — diagnostic, surface anything
                print(f"  raw address-pass: [EXC] {type(e).__name__}: {e}", flush=True)
            # 2. client 2-pass (address-first + Tuscany bbox + city ladder) — what the orchestrator uses
            try:
                final = await _geocode_address_first(client, {"search": q})
                print("  client 2-pass:   ", json.dumps(_top(final), ensure_ascii=False), flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  client 2-pass:    [EXC] {type(e).__name__}: {e}", flush=True)
    print("\n>>> DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
