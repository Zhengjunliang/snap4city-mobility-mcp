"""Direct probe — WHY the bus timetable comes back empty, and whether ANY tool can yield
real departure times at all.

Run on JupyterHub (s4c env): python scripts/probe_timetable.py
Public km4city backend (auth dropped from schema) → no LLM creds needed.

Background (lesson L21): `tpl_stop_timeline` returns `{stop, lines}` but its `timetable` /
`realtime` keys came back EMPTY. An earlier, narrower probe established that the tool's schema
has ONLY a `stop` param (5 datetime param names all rejected) → empty is NOT a missing-param
problem. This deeper version answers the two questions still open:

  Q1  What EXACTLY is in timetable/realtime? (full payload, never truncated — earlier probe
      cut at 3000 chars so the keys' real value/type/length were never seen.)
  Q2  Is there ANY OTHER tool that can return departure times? The earlier probe only
      keyword-matched tool NAMES; the server has been silently adding tools (L22), and a
      schedule capability could hide in a tool DESCRIPTION or a sibling line-level endpoint.
      So: dump every tool's full description + inputSchema, flag the schedule-ish ones, and
      generically try each flagged tool against the resolved stop/line URIs.

Decision after running:
  - timetable/realtime POPULATED in tpl_stop_timeline → client just needs to surface them.
  - some OTHER flagged tool returns real times → wire THAT into the tpl_timeline flow.
  - empty everywhere → server-side, no client fix; keep the honest "times unavailable" and
    keep it a referente item. (Re-confirms L21 with full evidence.)
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
    _unwrap_tpl,
)

LINE = "6"  # proven live: line "6" + Florence urban agency 888-48 → routes/stops (L21)
PREFERRED_STOP_HINT = "marco"  # "Museo Di San Marco" was a live line-6 stop

# A tool is "schedule-ish" if any of these appear in its name OR description.
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


def _describe_value(label: str, val) -> None:
    """Print a key's exact emptiness signature — this is the Q1 evidence."""
    kind = type(val).__name__
    size = len(val) if isinstance(val, (list, dict, str)) else "n/a"
    print(f"  {label}: type={kind} len={size} value={_full(val)[:600]}", flush=True)


async def _try_tool(client: Client, name: str, args: dict) -> None:
    print(f"\n### TRY {name}  args={args}", flush=True)
    try:
        res = await asyncio.wait_for(client.call_tool(name, args), timeout=30)
        print(_full(_unwrap(res))[:2500], flush=True)
    except asyncio.TimeoutError:
        print("[TIMEOUT >30s]", flush=True)
    except Exception as e:  # noqa: BLE001 — diagnostic, surface anything
        print(f"[EXC] {type(e).__name__}: {e}", flush=True)


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        tools = {t.name: t for t in await client.list_tools()}
        print(f"\n=== {len(tools)} TOOLS — full description + schema ===", flush=True)
        flagged: list[str] = []
        for name, t in tools.items():
            desc = getattr(t, "description", "") or ""
            hits = sorted(set(_flag(name) + _flag(desc)))
            mark = f"  <<< SCHEDULE-ISH: {hits}" if hits else ""
            if hits:
                flagged.append(name)
            print(f"\n- {name}{mark}", flush=True)
            print(f"    desc: {desc[:300]}", flush=True)
            print(f"    schema: {_full(t.inputSchema)[:500]}", flush=True)
        print(f"\n=== FLAGGED (schedule-ish) tools: {flagged} ===", flush=True)

        # --- resolve a real line-6 stop URI + capture its serving line URIs ---
        print(f"\n>>> resolving a real line-{LINE} stop URI ...", flush=True)
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
        stop_uri = pick["uri"]
        print("picked stop:", pick.get("name"), "->", stop_uri, flush=True)

        # --- Q1: full tpl_stop_timeline payload + EXACT timetable/realtime signature ---
        print("\n=== Q1: tpl_stop_timeline FULL payload ===", flush=True)
        tl = _unwrap_tpl(_unwrap(await client.call_tool("tpl_stop_timeline", {"stop": stop_uri})))
        print("top-level keys:", list(tl) if isinstance(tl, dict) else f"(not a dict: {type(tl).__name__})",
              flush=True)
        print("full payload:", _full(tl)[:6000], flush=True)
        line_uris: list[str] = []
        if isinstance(tl, dict):
            for key in ("timetable", "realtime", "departures", "passages", "orari"):
                if key in tl:
                    _describe_value(key, tl[key])
            # collect serving-line Route URIs — service_info(_dev) may carry their realtime data
            bindings = (((tl.get("busLines") or {}).get("results") or {}).get("bindings")) or []
            line_uris = [
                (b.get("lineUri") or {}).get("value")
                for b in bindings if isinstance(b, dict) and (b.get("lineUri") or {}).get("value")
            ]
        print("serving line URIs:", line_uris[:5], flush=True)

        # --- Q2: the realtime/time-varying tools, called CORRECTLY ---
        # service_info / service_info_dev take `serviceuri` (lowercase) — earlier probe guessed
        # wrong names. The stop URI IS a serviceUri; line Route URIs are services too. dev variant
        # adds fromtime/totime ('n-hour'/'n-minute'/ISO) for the time-varying window. If real
        # departures exist anywhere, they surface here, NOT in tpl_stop_timeline's empty keys.
        print("\n=== Q2: service_info / service_info_dev on the stop + serving lines ===", flush=True)
        targets = [("stop", stop_uri)] + [("line", u) for u in line_uris[:2]]
        for label, uri in targets:
            await _try_tool(client, "service_info", {"serviceuri": uri})
            await _try_tool(client, "service_info_dev", {"serviceuri": uri})
            await _try_tool(
                client, "service_info_dev",
                {"serviceuri": uri, "fromtime": "0-minute", "totime": "120-minute"},
            )
            print(f"    ^ ({label}) {uri}", flush=True)

    print("\n>>> DONE — Q1: are tpl_stop_timeline timetable/realtime empty dicts? "
          "Q2: does service_info(_dev) expose real departure/realtime data the timeline tool "
          "omits? If service_info_dev returns time-varying data ⇒ wire it in; if empty too "
          "⇒ server-side, confirms L21.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
