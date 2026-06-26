"""Direct probe — discover the parking pieces the parking feature needs to calibrate.

Run on JupyterHub (s4c env): python scripts/probe_parking.py
Public km4city backend (auth dropped from schema) → no LLM creds needed.

The "free parking near the destination" feature has four unknowns that only the live
backend can answer (lessons L19/L21/L22: probe the real tables before parsing). This script
dumps each one so the module constants / parser can be calibrated from evidence, not guesses:

  P1  CATEGORY — what is the real service-category name for car parks? get_service_categories
      (detailed) → print every category whose name looks parking/car related. Feeds
      mcp_tools.PARKING_CATEGORY (default guess "Car_park").
  P2  ENVELOPE — call service_search_near_gps_position near a Florence destination with the
      candidate category. Dump the FULL result shape so _extract_parking knows where name /
      serviceUri / coordinates / distance / free-spaces actually live (uris vs features vs
      Service.features), and whether a per-result DISTANCE field exists at all (S1: if not,
      the client computes Haversine from dest + each parking's coords).
  P3  REALTIME — take one returned parking serviceUri and try to read free/occupancy spaces,
      two ways: (a) the search `values=` param (one call, preferred) and (b) service_info /
      service_info_dev. Confirms whether realtime free-spaces is loaded server-side (L21/L22
      warn it is often empty) and the exact value name. Feeds PARKING_FREE_VALUE; if empty
      everywhere, the feature degrades to locations-only (the agreed fallback).

Decision: P1 gives the category; P2 gives the parser shape + whether distance is returned;
P3 says whether free-spaces is real (→ show counts) or absent (→ degrade to nearest lots).
For the map pin (addSelectorPin), capture the working payload from the PA.php dashboard via
DevTools separately — that is a browser step, not callable here.
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import _build_config, _unwrap

# A real Florence destination to search around (Piazza Santa Croce area — central, where
# car parks exist). Probe a couple of candidate categories until one returns parking.
DEST_LAT, DEST_LON = 43.7686, 11.2620
SEARCH_RADIUS_KM = 0.8

# Candidate category names to try (P1 prints the authoritative list; these are the guesses
# to validate). km4city historically used "Car_park"; PA.php may use another label.
CANDIDATE_CATEGORIES = ("Car_park", "Parking", "Parking_area", "CarPark", "Parcheggio")
# Candidate value names that might carry free spaces (P3 confirms the real one).
CANDIDATE_FREE_VALUES = ("freeParking", "free", "available", "freeSpaces", "occupancy", "posti_liberi")

PARKING_HINTS = ("park", "parch", "car_park", "carpark", "parcheggi")


def _full(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _generic_list(obj):
    """Best-effort: pull a list of items from a tool result of unknown shape."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("result", "results", "categories", "features", "items", "data"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
    return []


def _scan_service_uris(obj, out: list[str]) -> None:
    """Recursively collect km4city service URIs from any payload shape."""
    if isinstance(obj, str):
        if obj.startswith("http") and "/resource/" in obj and obj not in out:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _scan_service_uris(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _scan_service_uris(v, out)


async def _call(client: Client, name: str, args: dict):
    try:
        return _unwrap(await asyncio.wait_for(client.call_tool(name, args), timeout=40))
    except asyncio.TimeoutError:
        return {"error": "TIMEOUT >40s"}
    except Exception as e:  # noqa: BLE001 — diagnostic, surface anything
        return {"error": f"{type(e).__name__}: {e}"}


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        # --- P1: parking category name -------------------------------------------------
        print("\n=== P1: get_service_categories (detailed) — parking-ish names ===", flush=True)
        cats = await _call(client, "get_service_categories", {"mode": "detailed"})
        names: list[str] = []
        for c in _generic_list(cats):
            if isinstance(c, dict):
                for v in c.values():
                    if isinstance(v, str):
                        names.append(v)
            elif isinstance(c, str):
                names.append(c)
        parking_cats = sorted({n for n in names if any(h in n.lower() for h in PARKING_HINTS)})
        print(f"parking-ish categories: {parking_cats}", flush=True)
        print(f"(total categories seen: {len(set(names))})", flush=True)

        # The category to search with: first real parking-ish hit, else the first guess.
        search_cat = parking_cats[0] if parking_cats else CANDIDATE_CATEGORIES[0]

        # --- P2: search envelope near the destination ----------------------------------
        print(f"\n=== P2: service_search_near_gps_position near ({DEST_LAT},{DEST_LON}) "
              f"cat={search_cat!r} r={SEARCH_RADIUS_KM}km ===", flush=True)
        # Also request candidate free-space values so we can see if they come back inline.
        near = await _call(client, "service_search_near_gps_position", {
            "latitude": DEST_LAT, "longitude": DEST_LON,
            "maxdistance": SEARCH_RADIUS_KM, "categories": search_cat,
            "maxresults": 10, "values": ";".join(CANDIDATE_FREE_VALUES),
        })
        print("top-level keys:", list(near) if isinstance(near, dict) else type(near).__name__, flush=True)
        print("FULL envelope (first 4000 chars):", _full(near)[:4000], flush=True)
        print("\n>>> NOTE where name / serviceUri / coordinates / DISTANCE / free-spaces live "
              "above → calibrate _extract_parking. If NO per-result distance field, the client "
              "computes Haversine from the destination.", flush=True)

        parking_uris: list[str] = []
        _scan_service_uris(near, parking_uris)
        print(f"\nparking serviceUris found: {len(parking_uris)} -> {parking_uris[:5]}", flush=True)

        # If the first category returned nothing, retry the other guesses so the run is useful.
        if not parking_uris:
            for cat in CANDIDATE_CATEGORIES:
                if cat == search_cat:
                    continue
                retry = await _call(client, "service_search_near_gps_position", {
                    "latitude": DEST_LAT, "longitude": DEST_LON,
                    "maxdistance": SEARCH_RADIUS_KM, "categories": cat, "maxresults": 10,
                })
                _scan_service_uris(retry, parking_uris)
                print(f"- retry cat={cat!r}: {len(parking_uris)} uris so far", flush=True)
                if parking_uris:
                    print("  head:", _full(retry)[:1500], flush=True)
                    break

        # --- P3: realtime free-spaces -------------------------------------------------
        # The plain Car_park POIs (e.g. GARAGE VERDI) carry NO realtime; the live free-space
        # data lives on the IoT sensor entities (iot/orionUNIFI/... — the PA.php dashboard
        # shows CarParkParterre/Beccaria/S.Ambrogio with live SingleContent + TimeTrend
        # widgets). So probe the IoT/orion URIs FIRST, then fall back to the rest.
        print("\n=== P3: realtime free-spaces (IoT/orion parkings first) ===", flush=True)
        ordered = ([u for u in parking_uris if ("iot" in u.lower() or "orion" in u.lower())]
                   + [u for u in parking_uris if not ("iot" in u.lower() or "orion" in u.lower())])
        any_free = False
        for uri in ordered[:6]:
            info = await _call(client, "service_info", {"serviceuri": uri})
            blob = _full(info)
            # realtimeAttributes / realtime carry the live values when loaded.
            has_rt = ('"realtimeattributes": {}' not in blob.lower()
                      and ("realtimeattribute" in blob.lower() or '"realtime": {"' in blob.lower()))
            seen = [v for v in CANDIDATE_FREE_VALUES if v.lower() in blob.lower()]
            print(f"- {uri}\n    realtime? {has_rt}  free-values seen: {seen or 'NONE'}", flush=True)
            if has_rt or seen:
                any_free = True
                print("    service_info FULL (first 2500):", blob[:2500], flush=True)
        if not parking_uris:
            print("(no parking serviceUri resolved — check P1 category name / P2 radius)", flush=True)
        print(f"\n>>> realtime free-spaces available on SOME parking? {any_free}. If yes, note the "
              f"value name + which URI kind (iot/orion vs POI) → wire per-spot service_info for "
              f"those; if no, degrade to locations-only (agreed fallback).", flush=True)

    print("\n>>> DONE. Calibrate from this run: PARKING_CATEGORY (P1), _extract_parking shape + "
          "distance presence (P2), PARKING_FREE_VALUE / realtime availability (P3). Capture the "
          "addSelectorPin pin payload from the PA.php dashboard via browser DevTools (L30).",
          flush=True)


if __name__ == "__main__":
    asyncio.run(main())
