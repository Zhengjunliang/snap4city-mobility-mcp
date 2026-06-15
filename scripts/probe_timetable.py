"""Direct probe — is the empty bus timetable REALLY server-side, or did we test too narrowly?

Run on JupyterHub (s4c env): python scripts/probe_timetable.py
Public km4city backend (auth dropped from schema) → no LLM creds needed.

An earlier probe found ONE Florence (AT) stop with empty timetable/realtime and called it
server-side (lesson L21). Before trusting that, this version attacks the three ways that
conclusion could be wrong:

  P1  TOOLS — list every tool, flag the schedule-ish ones (name OR description). A schedule
      capability could hide in a tool we never tried.
  P2  OTHER STOPS / AGENCIES — does the timetable come back empty everywhere, or only for AT
      Firenze? Resolve a stop on several agencies (Florence urban, Roma ATAC, Athens OASA) and
      dump tpl_stop_timeline timetable/realtime for each. If some agency DOES carry times, the
      gap is data-coverage, not "no tool can ever do it".
  P3  CONTROL — does service_info_dev EVER return time-varying/realtime data? Pull real
      services near Florence (and a sensor-ish category) and call service_info_dev with a time
      window. If a sensor returns time-varying data, the TOOL + PARAMS are correct → the empty
      bus result is genuinely "this stop has no data", not "we called it wrong". If NOTHING
      ever returns realtime, suspect the tool/params instead.
  P4  PARAM FORMAT — the bus stop 400'd ("failed access") only with fromtime/totime set. Retry
      with ISO datetime and 'n-hour'/'n-day' to rule out the time FORMAT causing the 400.

Decision: empty in P2 across agencies AND P3 proves the tool works on sensors AND P4 shows the
400 is data-access (not format) ⇒ genuinely server-side (GTFS stop_times + realtime not loaded
for AT). Otherwise we have a client-side path to wire in.
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import _build_config, _unwrap
from snap4city_mobility_mcp.tpl import (
    _agency_entries,
    _generic_list,
    _resolve_agency,
    _route_uris,
    _stop_entries,
    _unwrap_tpl,
)

# Agencies to compare in P2. Florence urban resolves via "" default; the others by name token
# (km4city carries Roma ATAC + Athens OASA — proven in the tpl_agencies dump).
AGENCY_PROBES = ("", "ATAC", "Athens")

# Florence center — search here for real (likely realtime-capable) services in P3.
FLO_LAT, FLO_LON = 43.7765, 11.2486

SCHEDULE_HINTS = (
    "time", "schedule", "timetable", "orari", "orario", "realtime", "real-time",
    "hour", "depart", "arriv", "passage", "passaggi", "avl", "gtfs", "frequency",
    "next bus", "waiting", "attesa", "corse", "corsa",
)


def _flag(text: str) -> list[str]:
    low = (text or "").lower()
    return [h for h in SCHEDULE_HINTS if h in low]


def _full(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _scan_service_uris(obj, out: list[str]) -> None:
    """Recursively collect km4city service URIs from any payload shape."""
    if isinstance(obj, str):
        if obj.startswith("http") and "km4city/resource" in obj and obj not in out:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _scan_service_uris(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _scan_service_uris(v, out)


def _timetable_signature(tl) -> str:
    """One-line verdict on a tpl_stop_timeline / service_info payload."""
    tl = _unwrap_tpl(tl)
    if not isinstance(tl, dict):
        return f"(not a dict: {type(tl).__name__})"
    if "error" in tl:
        return f"ERROR {_full(tl)[:200]}"
    tt, rt = tl.get("timetable"), tl.get("realtime")
    return (
        f"timetable={type(tt).__name__}(len={len(tt) if isinstance(tt, (list, dict)) else '?'}) "
        f"realtime={type(rt).__name__}(len={len(rt) if isinstance(rt, (list, dict)) else '?'}) "
        f"keys={list(tl)}"
    )


async def _call(client: Client, name: str, args: dict):
    try:
        return _unwrap(await asyncio.wait_for(client.call_tool(name, args), timeout=30))
    except asyncio.TimeoutError:
        return {"error": "TIMEOUT >30s"}
    except Exception as e:  # noqa: BLE001 — diagnostic, surface anything
        return {"error": f"{type(e).__name__}: {e}"}


async def _resolve_stop(client: Client, agencies, agency_text: str):
    """First (line→route→stop) chain for an agency. Returns (agency_uri, stop_uri, stop_name)."""
    agency_uri = _resolve_agency(agencies, agency_text)
    if not agency_uri:
        # name-token fallback for the non-Florence probes
        for a in agencies:
            if agency_text and agency_text.lower() in str(a.get("name") or "").lower():
                agency_uri = a["uri"]
                break
    if not agency_uri:
        return None, None, None
    lines = _generic_list(await _call(client, "tpl_lines", {"agency": agency_uri}))
    line_ref = None
    for it in lines:
        if isinstance(it, dict):
            for k in ("uri", "lineUri", "line", "shortName", "name"):
                if isinstance(it.get(k), str) and it[k].strip():
                    line_ref = it[k]
                    break
        if line_ref:
            break
    if not line_ref:
        return agency_uri, None, None
    routes = await _call(client, "tpl_routes_by_line", {"line": line_ref, "agency": agency_uri})
    route_uris = _route_uris(routes)
    if not route_uris:
        return agency_uri, None, None
    stops = _stop_entries(await _call(client, "tpl_stops_by_route", {"route": route_uris[0]}))
    if not stops:
        return agency_uri, None, None
    return agency_uri, stops[0]["uri"], stops[0].get("name")


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        # --- P1: tool inventory (compact) ---
        tools = {t.name: t for t in await client.list_tools()}
        flagged = [n for n, t in tools.items()
                   if _flag(n) or _flag(getattr(t, "description", "") or "")]
        print(f"\n=== P1: {len(tools)} tools; schedule-ish = {flagged} ===", flush=True)

        agencies = _agency_entries(_unwrap(await client.call_tool("tpl_agencies", {})))
        print(f"agencies available: {len(agencies)}", flush=True)

        # --- P2: timetable across several agencies (not just AT Firenze) ---
        print("\n=== P2: tpl_stop_timeline across agencies ===", flush=True)
        flo_stop = None
        for atext in AGENCY_PROBES:
            agency_uri, stop_uri, stop_name = await _resolve_stop(client, agencies, atext)
            label = atext or "(Florence urban default)"
            if not stop_uri:
                print(f"- {label}: could not resolve a stop (agency_uri={agency_uri})", flush=True)
                continue
            tl = await _call(client, "tpl_stop_timeline", {"stop": stop_uri})
            print(f"- {label}: stop={stop_name!r}\n    {_timetable_signature(tl)}", flush=True)
            if atext == "":
                flo_stop = stop_uri

        # --- P3: CONTROL — can service_info_dev return time-varying data on a real sensor? ---
        print("\n=== P3: control — service_info_dev on real services near Florence ===", flush=True)
        cats = await _call(client, "get_service_categories", {"mode": "detailed"})
        print("service categories (head):", _full(cats)[:400], flush=True)
        near = await _call(
            client, "service_search_near_gps_position",
            {"latitude": FLO_LAT, "longitude": FLO_LON, "maxdistance": 1.0},
        )
        uris: list[str] = []
        _scan_service_uris(near, uris)
        # drop the bus-stop/route URIs — we want OTHER services (sensors/parking/etc.)
        sensor_uris = [u for u in uris if "gtfs_Stop" not in u and "gtfs_Route" not in u][:6]
        print(f"non-TPL service URIs found near Florence: {len(sensor_uris)}", flush=True)
        for u in sensor_uris:
            res = await _call(client, "service_info_dev",
                              {"serviceuri": u, "fromtime": "1-day", "totime": "0-minute"})
            blob = _full(res)
            has_rt = any(k in blob for k in ('"realtime"', "valueDate", "measuredTime",
                                             "time_series", "values", '"date"'))
            print(f"- {u}\n    time-varying? {has_rt}  head={blob[:300]}", flush=True)

        # --- P4: is the bus-stop 400 a FORMAT problem or a data-access problem? ---
        print("\n=== P4: fromtime/totime FORMAT variants on the AT Firenze stop ===", flush=True)
        if flo_stop:
            for ft, tt in (("1-hour", "0-minute"), ("1-day", "0-minute"),
                           ("2026-06-16T08:00:00", "2026-06-16T10:00:00")):
                res = await _call(client, "service_info_dev",
                                  {"serviceuri": flo_stop, "fromtime": ft, "totime": tt})
                print(f"- fromtime={ft!r} totime={tt!r} -> {_full(res)[:300]}", flush=True)
        else:
            print("(no Florence stop resolved — skipped)", flush=True)

    print("\n>>> DONE. Read: P2 (any agency with non-empty timetable?), P3 (does service_info_dev "
          "EVER return time-varying data ⇒ tool/params OK), P4 (is the 400 format vs data-access). "
          "Empty in P2 + P3 works on sensors + P4 is data-access ⇒ truly server-side for AT.",
          flush=True)


if __name__ == "__main__":
    asyncio.run(main())
