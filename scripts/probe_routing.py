"""Direct routing probe — bypass the orchestrator, see RAW per-mode server returns.

Run on JupyterHub (s4c env): python scripts/probe_routing.py
`routing` is a public backend (auth dropped from the schema) → no LLM creds needed.

Why this exists: the orchestrator wraps `routing` in a retry ladder + slim + error
reshaping, so a chat failure can't tell "client misused the tool" from "this specific
mode is broken server-side". Calling `call_tool("routing", ...)` directly, one raw
payload per (OD, routetype), localizes the fault. 2026-06-11 result: car/public_transport
return bare {"error": ""} for every OD while foot_quiet returns a journey for the same
ODs → car/PT are broken server-side, not a client or ZTL issue (see docs/lessons.md L19).
"""

import asyncio
import json

from fastmcp import Client

from snap4city_mobility_mcp.mcp_tools import _build_config, _unwrap

# Coords taken from a failed chat run's debug.log (geocode was correct). Both ODs share
# the Duomo origin — to fully rule out "car can't originate in the Duomo pedestrian
# zone", add an OD whose origin is also drivable (see plan §B confirmation probe).
ODS = {
    "Duomo->CampoMarte (drivable dest, non-ZTL)": dict(
        startlatitude=43.772556, startlongitude=11.257641,
        endlatitude=43.774815, endlongitude=11.282918,
    ),
    "Duomo->SantaCroce (central)": dict(
        startlatitude=43.772556, startlongitude=11.257641,
        endlatitude=43.767956, endlongitude=11.260316,
    ),
}
MODES = ["car", "foot_shortest", "foot_quiet", "public_transport"]

# public_transport is timetable-dependent — an empty body may mean "no departure time
# given", not "mode broken". Probe PT a second time WITH a startdatetime to disambiguate.
# api-notes §3: routing.startdatetime is free-form ("DD/MM/YYYY, HH:MM" or ISO), default now.
# Edit to a weekday daytime that actually has service if the default returns nothing.
PT_STARTDATETIME = "15/06/2026, 09:00"  # Mon morning — buses running


async def _probe(client: Client, label: str, args: dict) -> None:
    """One raw routing call, dumped (shared by the per-mode loop and the PT@datetime pass)."""
    print(f"\n### {label} ###  (calling...)", flush=True)
    try:
        res = await asyncio.wait_for(client.call_tool("routing", args), timeout=30)
        print(json.dumps(_unwrap(res), ensure_ascii=False)[:2000], flush=True)
    except asyncio.TimeoutError:
        print("[TIMEOUT >30s]", flush=True)
    except Exception as e:  # noqa: BLE001 — diagnostic script, surface anything
        print(f"[EXC] {type(e).__name__}: {e}", flush=True)


async def main() -> None:
    print(">>> connecting / fetching apps.json ...", flush=True)
    cfg = await _build_config()
    print(">>> connected, listing tools ...", flush=True)
    async with Client(cfg) as client:
        tools = {t.name: t for t in await client.list_tools()}
        print("TOOLS:", list(tools), flush=True)
        print("routing schema:",
              json.dumps(tools["routing"].inputSchema, ensure_ascii=False), flush=True)
        for label, od in ODS.items():
            for rt in MODES:
                await _probe(client, f"{label} | routetype={rt}", {**od, "routetype": rt})
        # PT-with-departure-time pass: same ODs, public_transport + startdatetime. Compare
        # against the no-datetime public_transport rows above — if these return a journey
        # while the bare ones were empty, the orchestrator just needs to pass a startdatetime.
        print(f"\n--- public_transport WITH startdatetime={PT_STARTDATETIME!r} ---", flush=True)
        for label, od in ODS.items():
            await _probe(
                client,
                f"{label} | routetype=public_transport @{PT_STARTDATETIME}",
                {**od, "routetype": "public_transport", "startdatetime": PT_STARTDATETIME},
            )
    print("\n>>> DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
