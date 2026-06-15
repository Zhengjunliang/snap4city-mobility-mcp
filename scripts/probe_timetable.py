"""Direct tpl_stop_timeline probe — find out WHY the stop timetable comes back empty.

Run on JupyterHub (s4c env): python scripts/probe_timetable.py
Public km4city backend (auth dropped from schema) → no LLM creds needed.

Why this exists (lesson L21): `tpl_stop_timeline` returns `{stop, lines}` but its
`timetable`/`realtime` keys were EMPTY in earlier probes. Two competing causes to separate
BEFORE touching client code (don't presume — dump the real payload first):
  (a) the tool now takes a date/time param we are NOT passing → empty because no departure
      window was given → FIXABLE client-side (pass a startdatetime, like routing).
  (b) the server simply has no schedule loaded for that stop → empty no matter what →
      server-side, stays a referente item.

This probe: prints the LIVE tpl_stop_timeline inputSchema (does it have a datetime param
now? the server has been silently adding params/tools — L22), resolves a REAL stop URI on
line 6 (Florence urban), then calls tpl_stop_timeline (1) plain and (2) with a weekday
datetime under every plausible param name, dumping each raw payload so the timetable/realtime
keys can be compared. The schema print is authoritative; the datetime calls are best-effort
(wrapped — an unknown param just 400s, harmless).

Decision after running:
  - timetable/realtime POPULATED only in the datetime call → fix = pass that param from
    orchestrator/tpl (add to the tpl_stop_timeline call in run_tpl_flow + extend the slot).
  - empty in ALL calls → server-side, no client fix; keep the honest "times unavailable".
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import _build_config, _unwrap
from snap4city_mobility_mcp.tpl import (
    _agency_entries,
    _resolve_agency,
    _route_uris,
    _stop_entries,
)

LINE = "6"  # proven live: line "6" + Florence urban agency 888-48 → routes/stops (L21)

# Weekday daytime when buses actually run — edit if the default window has no service.
# Tried under several param names since the live schema may name it differently.
WEEKDAY_DATETIME = "16/06/2026, 09:00"  # Tue morning
DATETIME_PARAM_CANDIDATES = ["startdatetime", "datetime", "date", "fromTime", "time"]

# Stop name to target on the line (token-loose). Falls back to a mid-list stop, then first.
PREFERRED_STOP_HINT = "marco"  # "Museo Di San Marco" was a live line-6 stop


async def _dump(client: Client, label: str, args: dict) -> None:
    print(f"\n### {label} ###  args={args}", flush=True)
    try:
        res = await asyncio.wait_for(client.call_tool("tpl_stop_timeline", args), timeout=30)
        print(json.dumps(_unwrap(res), ensure_ascii=False, default=str)[:3000], flush=True)
    except asyncio.TimeoutError:
        print("[TIMEOUT >30s]", flush=True)
    except Exception as e:  # noqa: BLE001 — diagnostic script, surface anything
        print(f"[EXC] {type(e).__name__}: {e}", flush=True)


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        tools = {t.name: t for t in await client.list_tools()}
        print("TOOLS:", list(tools), flush=True)
        # Any schedule-ish tool the server may have added (L22: tools appear silently).
        hits = [n for n in tools if any(k in n.lower()
                for k in ("time", "schedule", "realtime", "hour", "depart", "orari"))]
        print("schedule-ish tools:", hits, flush=True)
        if "tpl_stop_timeline" in tools:
            print("tpl_stop_timeline schema:",
                  json.dumps(tools["tpl_stop_timeline"].inputSchema, ensure_ascii=False),
                  flush=True)

        # --- resolve a real stop URI on line 6 (Florence urban default agency) ---
        print("\n>>> resolving a real line-%s stop URI ..." % LINE, flush=True)
        agencies = _agency_entries(_unwrap(await client.call_tool("tpl_agencies", {})))
        agency_uri = _resolve_agency(agencies, "")  # "" → Florence urban default
        print("agency_uri:", agency_uri, flush=True)
        if not agency_uri:
            print("!! no agency resolved — cannot continue", flush=True)
            return
        routes_payload = _unwrap(
            await client.call_tool("tpl_routes_by_line", {"line": LINE, "agency": agency_uri})
        )
        route_uris = _route_uris(routes_payload)
        print("route_uris (first 3):", route_uris[:3], flush=True)
        if not route_uris:
            print("!! no routes for line — cannot continue", flush=True)
            return
        stops_payload = _unwrap(await client.call_tool("tpl_stops_by_route", {"route": route_uris[0]}))
        stops = _stop_entries(stops_payload)
        print("stops found: %d" % len(stops), flush=True)
        if not stops:
            print("!! no stops parsed — cannot continue", flush=True)
            return
        pick = next(
            (s for s in stops if PREFERRED_STOP_HINT in str(s.get("name") or "").lower()),
            stops[len(stops) // 2],
        )
        print("picked stop:", pick.get("name"), "->", pick["uri"], flush=True)

        # --- (1) plain call, then (2) one call per candidate datetime param ---
        await _dump(client, "tpl_stop_timeline PLAIN (no datetime)", {"stop": pick["uri"]})
        for param in DATETIME_PARAM_CANDIDATES:
            await _dump(
                client,
                f"tpl_stop_timeline + {param}={WEEKDAY_DATETIME!r}",
                {"stop": pick["uri"], param: WEEKDAY_DATETIME},
            )
    print("\n>>> DONE — compare timetable/realtime keys across the calls above.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
