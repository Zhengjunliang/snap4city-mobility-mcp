"""Direct geocode probe — bypass the orchestrator, A/B the lang/logic knobs.

Run on JupyterHub (s4c env): python scripts/probe_geocode.py
`address_search_location` / `coordinates_to_address` are public backends (auth dropped) →
no LLM creds needed.

Why this exists: chat addresses land in the wrong place (e.g. "Giardino Niccolò Galli"
geocoded to the airport edge, off the walkable graph → route draw crashed). The native
Snap4City What-If widget is accurate because it starts from a precise clicked coordinate or
a street+civic address, not a fuzzy name. This probe isolates the forward-geocode quality:
for a batch of Florence queries it prints the coordinate _pick_coord would choose under each
(lang, logic) combo, then REVERSE-geocodes that coordinate back to a street so you can see
whether the pick is sane. Compare lang="it" vs the old default "en" to justify the change.
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import (
    _build_config,
    _filter_geocode_to_tuscany,
    _unwrap,
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


async def _pick(client: Client, search: str, lang: str, logic: str) -> list[float] | None:
    """address pass (excludePOI=true) under the given knobs → bbox filter → _pick_coord."""
    raw = _unwrap(await client.call_tool(
        "address_search_location",
        {"search": search, "excludePOI": True, "lang": lang, "logic": logic},
    ))
    return _pick_coord(_filter_geocode_to_tuscany(raw, search), search)


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        print("TOOLS:", [t.name for t in await client.list_tools()], flush=True)

        # Reverse-geocode sanity check (the near-me foundation + coordinate→address validator).
        lat, lng = REVERSE_PROBE
        rev = await reverse_geocode(client, lat, lng)
        print(f"\n### reverse_geocode({lat}, {lng}) ###\n{json.dumps(rev, ensure_ascii=False)}", flush=True)

        for q in QUERIES:
            print(f"\n### {q!r} ###", flush=True)
            for lang, logic in COMBOS:
                try:
                    coord = await asyncio.wait_for(_pick(client, q, lang, logic), timeout=30)
                except Exception as e:  # noqa: BLE001 — diagnostic script, surface anything
                    print(f"  lang={lang} logic={logic}: [EXC] {type(e).__name__}: {e}", flush=True)
                    continue
                if not coord:
                    print(f"  lang={lang} logic={logic}: no pick", flush=True)
                    continue
                # coord is [lng, lat]; reverse it to show what street the pick actually lands on.
                back = await reverse_geocode(client, coord[1], coord[0])
                addr = back.get("address") if isinstance(back, dict) else None
                muni = back.get("municipality") if isinstance(back, dict) else None
                print(f"  lang={lang} logic={logic}: {coord} -> {addr}, {muni}", flush=True)
    print("\n>>> DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
