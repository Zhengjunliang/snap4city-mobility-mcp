"""Direct TPL probe — bypass the orchestrator, see RAW per-step server returns and
localize the tpl/agency fault (client logic vs server) before touching tpl.py.

Run on JupyterHub (s4c env): python scripts/probe_tpl.py
The tpl_* tools are a public backend (auth dropped from the schema) → no LLM creds needed,
and run_tpl_flow is pure Python + client.call_tool, so the whole chat understand/respond
LLM path is skipped here.

Why this exists: chat tests 7/8/9 (tpl_lines / tpl_routes / tpl_timeline) all returned
empty data because _resolve_agency picks the WRONG agency — the brand "Autolinee Toscane"
has no single entry (only `Autolinee Toscane - <network>` supersets, no `/AtF`), and the
empty-text default lands on the FIRST "autolinee" hit = ExtraUrbano Arezzo, while Florence
line 6 lives under Urbano Area Metropolitana Fiorentina (888-48). This walks the chain with
raw dumps at each step so the agency bug and the never-observed tpl payload shapes are both
on the table.

Reuses the production helpers (same private-symbol style as probe_routing/probe_geocode):
  mcp_tools._build_config / _unwrap / _label_tokens
  tpl._agency_entries / _resolve_agency / _route_uris / _stop_entries / _match_stop / run_tpl_flow

Read-out guide: STEP 2 shows the wrong pick; STEP 5 shows tpl_routes_by_line(6) going
non-empty once the agency is the Florence urban one → confirms the client agency bug.
STEP 6/7 raw dumps record the real stops/timeline field names for the next calibration pass.
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import _build_config, _label_tokens, _unwrap
from snap4city_mobility_mcp.tpl import (
    _agency_entries,
    _match_stop,
    _resolve_agency,
    _route_uris,
    _stop_entries,
    run_tpl_flow,
)

# Florence urban network detector: "Autolinee Toscane - Urbano Area Metropolitana
# Fiorentina" (888-48) carries Florence city lines (incl. line 6). Token-based so it
# survives minor name variants.
_FI_URBAN_TOKENS = frozenset({"firenze", "fiorentina", "metropolitana"})

# Reproduce the three failing chat turns end-to-end through run_tpl_flow (no LLM).
PROBE_LINE = "6"
PROBE_STOP = "San Marco"


def _is_fi_urban(name: str | None) -> bool:
    toks = _label_tokens(str(name or ""))
    return "urbano" in toks and bool(toks & _FI_URBAN_TOKENS)


def _dump(payload: object, limit: int = 2000) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str)[:limit], flush=True)


def _summ(result: object) -> str:
    """One-line summary of a tool result for the run_tpl_flow audit view."""
    if isinstance(result, dict):
        if "error" in result:
            return f"ERROR: {result['error']!r}"
        return f"dict keys={list(result)[:6]}"
    if isinstance(result, list):
        return f"list[{len(result)}]"
    return repr(result)[:120]


async def _raw(client: Client, name: str, args: dict, *, dump: bool = True) -> object:
    """One raw tpl tool call → unwrapped payload (never raises; errors become {'error': ...})."""
    try:
        res = await asyncio.wait_for(client.call_tool(name, args), timeout=30)
        payload = _unwrap(res)
    except asyncio.TimeoutError:
        payload = {"error": "TIMEOUT >30s"}
    except Exception as e:  # noqa: BLE001 — diagnostic script, surface anything
        payload = {"error": f"{type(e).__name__}: {e}"}
    if dump:
        _dump(payload)
    return payload


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    async with Client(cfg) as client:
        tools = {t.name: t for t in await client.list_tools()}

        # STEP 0 — tpl tool schemas (real required/optional param names)
        print("\n===== STEP 0: tpl tool schemas =====", flush=True)
        for n in ("tpl_agencies", "tpl_lines", "tpl_routes_by_line",
                  "tpl_stops_by_route", "tpl_stop_timeline"):
            if n in tools:
                print(f"\n{n} schema:", flush=True)
                _dump(tools[n].inputSchema, limit=1200)
            else:
                print(f"\n{n}: NOT EXPOSED by server", flush=True)

        # STEP 1 — tpl_agencies, FULL list (name -> uri), no truncation of the count
        print("\n===== STEP 1: tpl_agencies (full) =====", flush=True)
        agencies_raw = await _raw(client, "tpl_agencies", {}, dump=False)
        entries = _agency_entries(agencies_raw)
        print(f"{len(entries)} agencies parsed:", flush=True)
        for e in entries:
            print(f"  {e['name']!r} -> {e['uri']}", flush=True)
        by_uri = {e["uri"]: e.get("name") for e in entries}

        # STEP 2 — current _resolve_agency behavior (shows the wrong pick)
        print("\n===== STEP 2: _resolve_agency(text) — current logic =====", flush=True)
        for txt in ["", "Autolinee Toscane", "ATAF", "Autolinee Toscane Firenze", "Trenitalia"]:
            uri = _resolve_agency(entries, txt)
            print(f"  {txt!r:32} -> {uri}  ({by_uri.get(uri)!r})", flush=True)
        empty_pick = _resolve_agency(entries, "")  # what an agency-less request resolves to today

        # STEP 3 — auto-detect the Florence urban candidate(s) (expected: 888-48)
        print("\n===== STEP 3: Florence-urban candidates =====", flush=True)
        fi_candidates = [e for e in entries if _is_fi_urban(e.get("name"))]
        for e in fi_candidates:
            print(f"  CANDIDATE {e['name']!r} -> {e['uri']}", flush=True)
        if not fi_candidates:
            print("  (none matched 'urbano' + Firenze tokens — inspect STEP 1 names)", flush=True)

        # Ordered unique agency probe set: current empty-text pick + Florence candidates.
        probe_agencies: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label, uri in [("resolver(empty-text)", empty_pick),
                           *[("fi-urban", e["uri"]) for e in fi_candidates]]:
            if uri and uri not in seen:
                seen.add(uri)
                probe_agencies.append((label, uri))

        # STEP 4 — tpl_lines raw per candidate agency (does line 6 show up?)
        print("\n===== STEP 4: tpl_lines per agency =====", flush=True)
        for label, uri in probe_agencies:
            print(f"\n--- tpl_lines | {label} | {by_uri.get(uri)!r} ---", flush=True)
            await _raw(client, "tpl_lines", {"agency": uri})

        # STEP 5 — tpl_routes_by_line(line=6) per candidate agency (wrong vs right agency)
        print(f"\n===== STEP 5: tpl_routes_by_line(line={PROBE_LINE!r}) per agency =====", flush=True)
        routes_by_agency: dict[str, object] = {}
        for label, uri in probe_agencies:
            print(f"\n--- tpl_routes_by_line | {label} | {by_uri.get(uri)!r} ---", flush=True)
            payload = await _raw(client, "tpl_routes_by_line", {"line": PROBE_LINE, "agency": uri})
            uris = _route_uris(payload)
            print(f"  _route_uris -> {len(uris)} route(s)", flush=True)
            routes_by_agency[uri] = payload

        # STEP 6 — tpl_stops_by_route raw for the first non-empty routes (calibrate _stop_entries)
        print("\n===== STEP 6: tpl_stops_by_route (first non-empty routes) =====", flush=True)
        first_route_uri = next(
            (u for _, uri in probe_agencies for u in _route_uris(routes_by_agency.get(uri))),
            None,
        )
        stops_entries: list[dict] = []
        if first_route_uri is None:
            print("  no route URI from any candidate agency — cannot probe stops", flush=True)
        else:
            print(f"  using route: {first_route_uri}", flush=True)
            stops_payload = await _raw(client, "tpl_stops_by_route", {"route": first_route_uri})
            stops_entries = _stop_entries(stops_payload)
            print(f"  _stop_entries -> {len(stops_entries)} stop(s): "
                  f"{[s.get('name') for s in stops_entries[:10]]}", flush=True)

        # STEP 7 — tpl_stop_timeline raw for the matched stop
        print(f"\n===== STEP 7: tpl_stop_timeline (match {PROBE_STOP!r}) =====", flush=True)
        match = _match_stop(stops_entries, PROBE_STOP)
        if match is None:
            print(f"  no stop matched {PROBE_STOP!r} among {len(stops_entries)} stops", flush=True)
        else:
            print(f"  matched: {match.get('name')!r} -> {match['uri']}", flush=True)
            await _raw(client, "tpl_stop_timeline", {"stop": match["uri"]})

        # STEP 8 — run_tpl_flow end-to-end (NO LLM): where does the CURRENT client chain stop?
        print("\n===== STEP 8: run_tpl_flow end-to-end (no LLM) =====", flush=True)
        for slots in (
            {"intent": "tpl_lines", "agency_text": "Autolinee Toscane"},          # test 7
            {"intent": "tpl_routes", "line_text": PROBE_LINE, "agency_text": ""},  # test 8
            {"intent": "tpl_timeline", "line_text": PROBE_LINE,                    # test 9
             "stop_text": PROBE_STOP, "agency_text": ""},
        ):
            print(f"\n--- slots: {slots} ---", flush=True)
            out = await run_tpl_flow(client, slots)
            print(f"  unsupported={out.get('unsupported')}", flush=True)
            for entry in out.get("tool_results") or []:
                print(f"    {entry['name']} {entry.get('args')} -> {_summ(entry.get('result'))}",
                      flush=True)
    print("\n>>> DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
