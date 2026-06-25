"""Direct geocode probe — isolate WHY our MCP geocode returns foreign garbage.

Run on JupyterHub (s4c env): python scripts/probe_geocode.py
`address_search_location` / `coordinates_to_address` are public backends (auth dropped) →
no LLM creds needed.

Two questions this A/B answers, with the bare `call_tool` (bypassing the orchestrator's
ladder / bbox filter so we see the RAW server result set):

  A. PARAM A/B — does any client lever recover Tuscany? Per term it compares the bare query
     (what the native What-If autocomplete sends) against our pipeline params: + ", Firenze"
     suffix, + excludePOI, + lang=it, + logic=or. If `bare` returns Tuscan hits but our
     pipeline params do not, the params poison the set (client-fixable). If EVERY variant is
     all-foreign, the name is absent from the MCP Tuscan index (server-side, client can't fix).
  B. FLAKINESS — each variant is run REPEATS times; we print the Tuscan hit RATE (e.g. 1/3)
     so a transient zero-region window (L20) is told apart from a stable all-foreign miss.

Per (term, variant, trial) we print result count, # in Tuscany bbox, the first Tuscan hit's
rank/city/score, and the top-3 city(score) so the foreign junk is visible. It also
reverse-geocodes a known point (the near-me foundation: reverse is reliable where forward is not).
"""

import asyncio

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import (
    GEOCODE_LANG,
    GEOCODE_LOGIC,
    _build_config,
    _in_tuscany,
    _unwrap,
    reverse_geocode,
)

# How many times to repeat each variant (flakiness / hit-rate; L20 transient zero-region).
REPEATS = 3

# Terms that FAILED in the live chat (all-foreign), plus one known-good control (Duomo).
# Each is probed both bare and with the ", Firenze" suffix the orchestrator's `understand`
# appends, so we see whether the suffix itself poisons the set.
TERMS = [
    "Piazza del Duomo",  # control: a landmark that usually hits
    "oratorio di san francesco poverino",
    "chiesa dei sette santi fondatori",
    "stazione di Santa Maria Novella",
    "Santa Croce",
]

# A known point for the reverse-geocode sanity check: What-If returned "3, VIA ZARA, FIRENZE".
REVERSE_PROBE = (43.781834, 11.25891)


def _variants(term: str):
    """The client levers, isolated. `pipeline-addr`/`pipeline-poi` are exactly what
    `_geocode_address_first` sends (pass 1 / pass 2); the rest strip one lever at a time so we
    see which (if any) brings Tuscany back. `+Firenze` appends the orchestrator's city suffix."""
    fi = f"{term}, Firenze"
    return [
        ("bare", {"search": term}),
        ("bare+Firenze", {"search": fi}),
        ("excludePOI+Firenze", {"search": fi, "excludePOI": True}),
        ("lang+logic+Firenze", {"search": fi, "lang": GEOCODE_LANG, "logic": GEOCODE_LOGIC}),
        ("pipeline-addr", {"search": fi, "excludePOI": True, "lang": GEOCODE_LANG, "logic": GEOCODE_LOGIC}),
        ("pipeline-poi", {"search": fi, "excludePOI": False, "lang": GEOCODE_LANG, "logic": GEOCODE_LOGIC}),
    ]


def _tuscan_first(feats):
    """(rank, feature) of the first in-bbox hit, or (None, None)."""
    for i, f in enumerate(feats):
        if _in_tuscany((f.get("geometry") or {}).get("coordinates")):
            return i, f
    return None, None


def _summarize(payload) -> str:
    """count, #in-Tuscany, the FIRST Tuscan hit's rank/city/score (if any), and the top-3
    foreign city(score) for an address_search_location payload."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return f"NOT a FeatureCollection: {str(payload)[:120]}"
    feats = payload.get("features") or []
    tusc_i, tf = _tuscan_first(feats)
    tusc_n = sum(1 for f in feats if _in_tuscany((f.get("geometry") or {}).get("coordinates")))
    top = ", ".join(f"{(f.get('properties') or {}).get('city')}({(f.get('properties') or {}).get('score')})"
                    for f in feats[:3])
    if tf is not None:
        p = tf.get("properties") or {}
        tusc = f"FIRST Tuscan @#{tusc_i}: {p.get('city')} {p.get('address')}({p.get('score')})"
    else:
        tusc = "NO Tuscan hit"
    return f"returned={len(feats)} inTuscany={tusc_n} | {tusc} | top3: {top}"


async def _has_tuscan(client, args) -> bool | None:
    """One call: True if any in-bbox hit, False if none, None on exception (printed inline)."""
    try:
        raw = _unwrap(await asyncio.wait_for(
            client.call_tool("address_search_location", args), timeout=40))
    except Exception as e:  # noqa: BLE001 — diagnostic script, surface anything
        print(f"      [EXC] {type(e).__name__}: {e}", flush=True)
        return None
    print(f"      {_summarize(raw)}", flush=True)
    feats = raw.get("features") if isinstance(raw, dict) else None
    return _tuscan_first(feats or [])[1] is not None


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
                hits = 0
                for trial in range(1, REPEATS + 1):
                    print(f"  [{label}] try {trial}/{REPEATS} {args}", flush=True)
                    ok = await _has_tuscan(client, args)
                    if ok:
                        hits += 1
                print(f"  => [{label}] Tuscan hit rate: {hits}/{REPEATS}", flush=True)
    print("\n>>> DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
