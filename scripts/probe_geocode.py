"""Direct geocode probe — isolate WHY our MCP geocode returns foreign garbage.

Run on JupyterHub (s4c env): python scripts/probe_geocode.py
`address_search_location` / `coordinates_to_address` are public backends (auth dropped) →
no LLM creds needed.

Finding so far: the native What-If autocomplete calls `location/?search=via+zara+3` (bare
query, no extra params) and gets clean Tuscan "VIA ZARA, civic 3" hits (score ~12.6). Our
pipeline calls address_search_location with search="Via Zara 3, Firenze" + excludePOI=true
+ lang=it + logic=or and gets foreign junk (Antwerpen/Valencia, score ~7.7) → the Tuscany
bbox filter then drops everything → "no Tuscany-area match".

This probe A/B's the QUERY FORMAT and PARAMS to find which one poisons the result set:
for a few terms it prints, per variant, the result count, how many fall in the Tuscany
bbox, and the top-5 city(score) — so we can see whether dropping ", Firenze" / excludePOI /
lang / logic is what brings Tuscany back. It also reverse-geocodes a known point as a sanity
check (the near-me foundation).
"""

import asyncio

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import (
    _build_config,
    _in_tuscany,
    _unwrap,
    reverse_geocode,
)

# Terms that FAILED in the pipeline run (all-foreign), to see which format recovers Tuscany.
TERMS = ["via zara 3", "Santa Croce", "stazione di Santa Maria Novella"]

# A known point for the reverse-geocode sanity check: What-If returned "3, VIA ZARA, FIRENZE".
REVERSE_PROBE = (43.781834, 11.25891)


def _variants(term: str):
    """Compare the bare query against bigger maxresults — the last client-side lever: are
    the Tuscan hits just buried below the top-100, or absent from the MCP index entirely?"""
    return [
        ("bare", {"search": term}),
        ("maxresults=1000", {"search": term, "maxresults": 1000}),
        ("maxresults=5000", {"search": term, "maxresults": 5000}),
    ]


def _summarize(payload) -> str:
    """count, #in-Tuscany, the FIRST Tuscan hit's rank/city/score (if any), serviceType, and
    the top-3 foreign city(score) for an address_search_location payload."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return f"NOT a FeatureCollection: {str(payload)[:120]}"
    feats = payload.get("features") or []
    stype = ((feats[0].get("properties") or {}).get("serviceType") if feats else None)
    tusc_i = next((i for i, f in enumerate(feats)
                   if _in_tuscany((f.get("geometry") or {}).get("coordinates"))), None)
    tusc_n = sum(1 for f in feats if _in_tuscany((f.get("geometry") or {}).get("coordinates")))
    top = ", ".join(f"{(f.get('properties') or {}).get('city')}({(f.get('properties') or {}).get('score')})"
                    for f in feats[:3])
    if tusc_i is not None:
        p = feats[tusc_i].get("properties") or {}
        tusc = f"FIRST Tuscan @#{tusc_i}: {p.get('city')} {p.get('address')}({p.get('score')})"
    else:
        tusc = "NO Tuscan hit"
    return (f"count={payload.get('count')} returned={len(feats)} inTuscany={tusc_n} "
            f"serviceType={stype!r}\n      {tusc} | top3: {top}")


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        print("TOOLS:", [t.name for t in await client.list_tools()], flush=True)

        lat, lng = REVERSE_PROBE
        rev = await reverse_geocode(client, lat, lng)
        res = rev.get("result") if isinstance(rev, dict) else None
        first = res[0] if isinstance(res, list) and res else rev
        print(f"\n### reverse_geocode({lat}, {lng}) -> {first}", flush=True)

        for term in TERMS:
            print(f"\n### TERM {term!r} ###", flush=True)
            for label, args in _variants(term):
                try:
                    raw = _unwrap(await asyncio.wait_for(
                        client.call_tool("address_search_location", args), timeout=40))
                    print(f"  [{label}] {args}\n      {_summarize(raw)}", flush=True)
                except Exception as e:  # noqa: BLE001 — diagnostic script, surface anything
                    print(f"  [{label}] [EXC] {type(e).__name__}: {e}", flush=True)
    print("\n>>> DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
