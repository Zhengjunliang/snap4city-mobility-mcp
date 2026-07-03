"""Langgraph mobility advisor: the orchestration graph.

A natural-language query runs through a linear graph: understand -> execute ->
respond -> END.

- understand (LLM, forced tool call): extracts the request slots (intent, origin,
  destination, mode) from the latest user turn, place text only. Follow-ups like
  "那坐公交呢?" resolve against the conversation history.
- execute (plain Python, no LLM): for a route intent, geocodes both endpoints then
  calls routing with the requested mode. tpl_* intents go to the discovery chains
  in tpl.py; "other" falls through to an "unsupported" reply.
- respond (LLM, no tools): phrases a multilingual answer from the results and
  assembles the widget JSON (with the full route WKT and the updated messages).

The model never picks tools itself: in agentic mode Llama4 tends to emit tool calls
as pythonic text instead of structured tool_calls, which then leaks into the answer.
Letting it pick only slots and prose, with Python driving the tools, avoids that.

MCP execution and km4city quirk handling live in mcp_tools.py, the Llama4 client in
llm.py. Runs end-to-end only on the Snap4City JupyterHub.
"""
import asyncio
import json
import logging
import math
import time
from datetime import datetime
from functools import partial
from typing import Any, TypedDict

from fastmcp import Client
from langgraph.graph import END, StateGraph

from snap4city_mobility_mcp.llm import (
    Llama4Client,
    Llama4Error,
    assistant_message,
    tool_calls,
)
from snap4city_mobility_mcp.mcp_tools import (
    PARKING_CATEGORY,
    PARKING_MAX,
    PARKING_RADIUS_KM,
    PARKING_REALTIME_FROMTIME,
    _build_config,
    _label_tokens,
    _local_config,
    exec_tool,
    group_arc_legs,
    parse_parking_features,
    read_parking_realtime,
    slim_result_for_llm,
)
from snap4city_mobility_mcp.tpl import (
    REQUIRED_SLOTS as TPL_REQUIRED_SLOTS,
    TPL_INTENTS,
    TPL_TOOL_NAMES,
    extract_tpl_data,
    run_tpl_flow,
    slim_tpl_result,
    tpl_template_answer,
)

logger = logging.getLogger(__name__)

UNDERSTAND_SYSTEM = """\
You are the intent-extraction stage of a Florence (Tuscany, Italy) public-mobility \
advisor. Read the conversation and the user's LATEST message, then call \
`extract_slots` exactly once. Classify request_type, info_kind, and mode per each \
field's own description in the schema.
Rules:
- Always fill EVERY field of `extract_slots` (use '' for a slot the user truly did \
not give). Never drop the destination when the user named one.
- Extract PLACE TEXT only (e.g. "Piazza del Duomo, Firenze"). NEVER output \
coordinates — a separate tool geocodes places. Keep a city/town the user names \
attached to its place text, but NEVER add a city the user did not say.
- Ignore greetings and pleasantries ("ciao", "hello", "per favore") — they never \
change the slots.
- For a follow-up that omits a place (e.g. "what about by bus?", "那坐公交呢?"), \
reuse the origin/destination from earlier in the conversation and change only what \
the user changed (here mode → public_transport; for a transit_info follow-up, the \
info_kind).
- agency_text / line_text / stop_text stay '' unless the user asked about transport \
lines, routes, stops, or timetables.
- The service area is Tuscany only; do not invent places outside it.
<examples>
"ciao, voglio andare da stazione di Rifredi a piazza Dalmazia a piedi" → request_type=journey, origin_text="stazione di Rifredi", destination_text="piazza Dalmazia", mode=foot_shortest (all other slots '')
"da piazza Duomo a piazza Dalmazia in Firenze" → request_type=journey, origin_text="piazza Duomo, Firenze", destination_text="piazza Dalmazia, Firenze"
"quali linee collegano Santa Maria Novella e il Duomo?" → request_type=journey, origin_text="Santa Maria Novella", destination_text="Duomo" (an origin→destination trip stays a journey even with transit words)
"e in bus?" (follow-up to the piazza Duomo → piazza Dalmazia trip) → request_type=journey, origin_text="piazza Duomo, Firenze", destination_text="piazza Dalmazia, Firenze", mode=public_transport
"e le fermate della linea 6?" → request_type=transit_info, info_kind=stops, line_text="6"
</examples>"""

RESPOND_SYSTEM = """\
You are a friendly Florence (Tuscany, Italy) mobility assistant. ALWAYS reply in the \
user's own language — if the user wrote Italian, reply in Italian; if \
the language is unclear (e.g. a bare greeting), default to ITALIAN. Phrasing is yours: \
natural and helpful, not robotic, \
but lead with the answer and keep it concise. A short greeting is fine ONLY on the \
FIRST turn (the message says which); on a follow-up answer directly — no greeting and \
no repeated sign-offs ("happy to help", "let me know if you need more").
Hard rules (never break these):
- Every fact must come ONLY from the RESULTS given to you. Never invent or estimate \
coordinates, distances, durations, ETAs, line names, stop names, or route IDs — not \
from coordinates, not from general knowledge, not "approximately". Do not call tools.
- If the user asks about lines / routes / stops / times but RESULTS has no matching \
entries, say plainly you don't have that information — NEVER list line numbers or name \
an operator (e.g. ATAF) from your own knowledge. Not one fabricated entry.
- Never include raw coordinates in your answer.
- For a successful route: give the distance in km and the duration/ETA; main streets, \
if listed, are a nice touch. For a public-transport route whose RESULTS carry `legs`, \
narrate the trip leg by leg (walk to X, ride the <transport> of <provider> to Y, walk \
on) using ONLY the leg fields — never invent lines, stops, or times. A route that carries \
a distance HAS BEEN FOUND: present it directly and NEVER ask the user to restate, clarify, \
or give a nearby landmark for the origin/destination — they were already located. If a \
route has no duration/ETA (e.g. a bus route), give its distance and main streets and simply \
note the schedule/time is not available — do not invent one and do not treat it as a failure.
- When RESULTS holds more than one successful route for the same trip (different travel \
modes), give each mode its own distance and duration and say which is faster, using \
ONLY the RESULTS fields.
- If a RESULTS item could not be computed (an `error`, or a route/place not found), \
say so plainly WITHOUT any numbers and suggest a sensible alternative (another mode, a \
more precise address); when geocoded addresses are present, mention how you read the \
origin/destination so the user can spot a wrong match.
- If a routing RESULTS item carries a `hint`, follow it for the alternative you \
suggest — it already decided the right one: `car_pt_blocked_try_foot` = that mode is \
likely blocked by Florence's ZTL/pedestrian core, so suggest going on foot or by \
public transport; `service_empty_try_foot_or_later` = a service-side problem for that \
mode (NOT a ZTL), so suggest walking (foot routes work) or trying again later; \
`pt_degraded_to_foot` = the public-transport request returned a walking-only journey \
(no real transit), so if other modes are present do NOT list it as a public-transport \
option, and if it is the only result say there is no direct public transport for this \
trip and give the walking distance/time instead. With NO `hint`, never claim a \
ZTL/pedestrian zone yourself.
- If RESULTS has status "missing_place", ask the user for the field(s) listed in \
`missing` (the origin/destination of a trip, or the line/stop of a public-transport \
question) — do NOT say the request is unsupported.
- For public-transport discovery RESULTS (agencies, lines, routes, stops, \
timetables): present them as a compact list, say which agency you used, and add no \
entries beyond those listed. When `count` exceeds the listed items, say how many exist \
in total; stop lists cover only the first 2 routes (directions) of the line, so say so \
when the route count is higher. If only an agency list came back (the requested agency \
was not recognized), ask the user to pick one of those agencies.
- For a stop timetable RESULT (`stop` + `lines` serving it): name the stop and list the \
lines that serve it. If there is no `timetable`/`realtime` data, say the scheduled times \
are not available right now — NEVER invent departure times. If the requested stop was not \
found on the line, say so and suggest checking the stop name or the line.
- Do NOT mention car parks / parking in your reply: the interface shows the nearby car \
parks (with live free-spaces) in its own list under your message, so listing them in text \
would just duplicate it. Answer only the trip itself.
- If RESULTS has status "unsupported", explain in your own words that for now you \
answer point-to-point trip questions (on foot, by car, or by public transport) and \
public-transport discovery questions (lines, routes, stops, timetables) and invite \
the user to rephrase."""

# Not a real MCP tool: a function schema used only to force structured output
# from the understand node.
_EXTRACT_SLOTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "extract_slots",
        "description": "Record the structured interpretation of the user's latest mobility request.",
        "parameters": {
            "type": "object",
            "properties": {
                "request_type": {
                    "type": "string",
                    "enum": ["journey", "transit_info", "other"],
                    "description": "Classify by STRUCTURE, not vocabulary. 'journey' = the user wants to get from an origin to a destination (both named, or carried over from earlier in the chat) — use 'journey' even when transit words like bus/line/tram appear. 'transit_info' = a reference question about the transport network (which lines/routes/stops, or a stop timetable) with NO origin-destination trip. 'other' = neither.",
                },
                "info_kind": {
                    "type": "string",
                    "enum": ["lines", "routes", "stops", "timeline", ""],
                    "description": "Only when request_type='transit_info' (else ''): 'lines' = which lines a network/agency runs; 'routes' = the routes of one line; 'stops' = the stops along a line; 'timeline' = the timetable at a stop.",
                },
                "origin_text": {
                    "type": "string",
                    "description": "Free-text origin place name, '' if absent.",
                },
                "destination_text": {
                    "type": "string",
                    "description": "Free-text destination place name, '' if absent.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["car", "public_transport", "foot_quiet", "foot_shortest", ""],
                    "description": "Travel mode, '' if not specified. Map: walk / on foot → foot_shortest (a quiet or scenic walk → foot_quiet); drive / car → car; bus / tram / public transport / 公交 → public_transport.",
                },
                "agency_text": {
                    "type": "string",
                    "description": "Public transport agency the user named, '' if none.",
                },
                "line_text": {
                    "type": "string",
                    "description": "Public transport line short name (e.g. '6', 'T1'), '' if none.",
                },
                "stop_text": {
                    "type": "string",
                    "description": "Public transport stop name, '' if none.",
                },
            },
            # Mark every field required: Llama4 only fills required params and
            # silently drops optional ones (one run extracted the origin but lost
            # the destination). An empty string '' marks a slot the user didn't give.
            "required": [
                "request_type", "info_kind", "origin_text", "destination_text",
                "mode", "agency_text", "line_text", "stop_text",
            ],
        },
    },
}


class AdvisorState(TypedDict, total=False):
    messages: list[dict[str, Any]]  # chat history (system/user/assistant) for multi-turn
    intent: str  # route | tpl_lines | tpl_routes | tpl_stops | tpl_timeline | other
    slots: dict[str, Any]  # understand output (request_type, info_kind, intent, origin_text, ...)
    tool_results: list[dict[str, Any]]  # audit: [{name, args, result}] per call
    unsupported: bool  # execute could not run a flow ("other" intent or missing slots)
    endpoints: dict[str, Any]  # precise geocoded {origin, destination} {lat,lng} for the route
    parking: list[dict[str, Any]]  # car parks near the destination (car routes), with live free-spaces
    response: dict[str, Any]  # widget JSON assembled by respond


# The LLM classifies on two axes (request_type + info_kind); we fold that into the
# single `intent` string the rest of the graph dispatches on.
_INFO_KIND_TO_INTENT = {
    "lines": "tpl_lines",
    "routes": "tpl_routes",
    "stops": "tpl_stops",
    "timeline": "tpl_timeline",
}


def _request_to_intent(slots: dict[str, Any]) -> str:
    """Map the two-axis classification to one internal `intent` string.

    journey -> route; transit_info -> tpl_<info_kind> (blank info_kind -> other);
    anything else -> other."""
    rt = slots.get("request_type")
    if rt == "journey":
        return "route"
    if rt == "transit_info":
        return _INFO_KIND_TO_INTENT.get(slots.get("info_kind") or "", "other")
    return "other"


async def understand(state: AdvisorState, *, llm: Llama4Client) -> dict[str, Any]:
    """LLM extracts slots from the latest user turn via a forced tool call.

    The forced tool_choice makes the gateway return structured tool_calls, so this
    stage avoids the pythonic-text shape that breaks free tool use.
    """
    history = state["messages"]
    convo = [m for m in history if m.get("role") in ("user", "assistant")]
    slots: dict[str, Any] = {"intent": "other"}
    try:
        t0 = time.perf_counter()
        resp = await llm.achat(
            messages=[{"role": "system", "content": UNDERSTAND_SYSTEM}, *convo],
            tools=[_EXTRACT_SLOTS_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "extract_slots"}},
            temperature=0,
        )
        logger.debug("understand LLM took %.1fs", time.perf_counter() - t0)
        calls = tool_calls(resp)
        if calls:
            raw = (calls[0].get("function") or {}).get("arguments")
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict) and parsed.get("request_type"):
                slots = parsed
                slots["intent"] = _request_to_intent(parsed)
    except (json.JSONDecodeError, Llama4Error) as e:
        # Fall back to {"intent": "other"}: execute treats it as unsupported, and the
        # debug log keeps the cause visible (LLM error vs. genuinely empty slots).
        logger.debug("understand slot extraction failed: %s", e)
    logger.debug("understand slots: %s", slots)
    return {"slots": slots, "intent": slots.get("intent", "other")}


def _pick_coord(geocode: Any, search: str) -> list[float] | None:
    """Best feature's [lng, lat] for `search` from a geocode result, or None.

    The server sometimes ranks a fuzzy POI hit above the real place (once a company
    1.1 km west of "Piazza Duomo"), so we prefer the first feature whose address/name
    tokens are all covered by the search text, and reject features with extra tokens.
    When no label matches (e.g. stations, whose features carry no address) we keep the
    server's first hit. exec_tool already pins results to Tuscany, so an empty or
    non-FeatureCollection payload gives None here.
    """
    if not isinstance(geocode, dict) or "error" in geocode:
        return None
    features = geocode.get("features")
    if not (isinstance(features, list) and features):
        return None
    want = _label_tokens(search)
    idx, best = 0, features[0]
    if want:
        for i, f in enumerate(features):
            props = f.get("properties") or {}
            label = " ".join(str(v) for v in (props.get("address"), props.get("name")) if v)
            toks = _label_tokens(label)
            # Skip a label that is just the municipality ("FIRENZE"): it matches any
            # search ending in ", Firenze" but is never a useful pick.
            if toks and toks <= want and not toks <= _label_tokens(str(props.get("city") or "")):
                idx, best = i, f
                break
    logger.debug(
        "geocode %r picked feature #%d (address=%r)",
        search, idx, (best.get("properties") or {}).get("address"),
    )
    coords = (best.get("geometry") or {}).get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lng, lat = coords[0], coords[1]
        if isinstance(lng, (int, float)) and isinstance(lat, (int, float)):
            return [float(lng), float(lat)]
    return None


# A failed walking route is retried once with the other foot profile: the two
# profiles take different graph paths, so one can succeed where the other returns an
# empty body. Only for walking; car/public_transport failures instead get an
# alternative suggested by respond.
_FOOT_FALLBACK = {"foot_quiet": "foot_shortest", "foot_shortest": "foot_quiet"}


async def execute(
    state: AdvisorState, *, client: Client, local_client: Client | None = None
) -> dict[str, Any]:
    """Run the tool flow for the extracted intent (no LLM).

    route: geocode both endpoints, then routing with the requested mode (plus a foot
    profile retry). tpl_* intents go to tpl.run_tpl_flow. Every call is recorded in
    tool_results so respond can mine the widget data. "other" or missing slots set
    unsupported.
    """
    slots = state.get("slots") or {}
    results: list[dict[str, Any]] = []
    # Forward geocoding goes to our local MCP server (referente's is broken, L29); routing
    # and tpl_* stay on the remote client. Tests pass only `client`, so lc falls back to it.
    lc = local_client or client

    if slots.get("intent") in TPL_INTENTS:
        return await run_tpl_flow(client, slots)
    if slots.get("intent") != "route":
        return {"tool_results": results, "unsupported": True}

    origin_text = (slots.get("origin_text") or "").strip()
    dest_text = (slots.get("destination_text") or "").strip()
    if not (origin_text and dest_text):
        return {"tool_results": results, "unsupported": True}

    async def _geocode(search: str) -> list[float] | None:
        args = {"search": search}
        result = await exec_tool(lc, "address_search_location", args)
        results.append({"name": "address_search_location", "args": json.dumps(args), "result": result})
        coord = _pick_coord(result, search)
        if logger.isEnabledFor(logging.DEBUG):
            # The slim view drops coordinates, so log the picked one here.
            logger.debug(
                "tool address_search_location %s -> %s (picked %s)",
                args,
                json.dumps(slim_result_for_llm("address_search_location", result), ensure_ascii=False)[:500],
                coord,
            )
        return coord

    origin = await _geocode(origin_text)
    dest = await _geocode(dest_text)
    if origin is None or dest is None:
        return {"tool_results": results, "unsupported": False}  # geocode error: respond explains

    mode_specified = bool(slots.get("mode"))
    # No mode given: route walking, driving AND public transport so the dashboard draws
    # all three for comparison (a foot/car/bus line each). PT returns a journey now (L19),
    # so it yields a drawable line between the endpoints; a mode that comes back empty
    # (car in a ZTL, PT on some OD) just stays absent and draws one less line. An explicit
    # mode runs that one only.
    modes = [slots["mode"]] if mode_specified else ["foot_shortest", "car", "public_transport"]

    async def _route(routetype: str, *, attempts: int | None = None) -> dict[str, Any]:
        # GeoJSON coordinate order is [longitude, latitude]. Returns the audit entry; the
        # caller appends it, so concurrent calls don't race on the shared results list.
        args = {
            "startlatitude": origin[1],
            "startlongitude": origin[0],
            "endlatitude": dest[1],
            "endlongitude": dest[0],
            "routetype": routetype,
        }
        start = time.perf_counter()
        result = await exec_tool(client, "routing", args, routing_attempts=attempts)
        elapsed = time.perf_counter() - start
        # Per-mode latency in debug.log. Probe (2026-06-29) measured routing at 1.6-4.6 s, so a
        # missing PT line is a foot-only degrade / route-not-found, NOT a slow call timing out.
        logger.debug("routing mode=%s took %.1fs", routetype, elapsed)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "tool routing %s -> %s",
                args,
                json.dumps(slim_result_for_llm("routing", result), ensure_ascii=False)[:500],
            )
        return {"name": "routing", "args": json.dumps(args), "result": result}

    async def _route_pt() -> dict[str, Any]:
        # MCP routing's public_transport never returns transit (foot-only / -2 for any
        # date/OD, L19). The bus line comes from our local `bus_route` tool instead, which
        # wraps the What-If GraphHopper router (mcp_server.py) — the same source the
        # Gea-Night dashboard draws from. Goes to the local client (lc), like geocode (L29).
        args = {
            "start_latitude": origin[1],
            "start_longitude": origin[0],
            "end_latitude": dest[1],
            "end_longitude": dest[0],
            "startdatetime": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        }
        start = time.perf_counter()
        result = await exec_tool(lc, "bus_route", args)
        logger.debug("routing mode=public_transport (bus_route) took %.1fs", time.perf_counter() - start)
        # Shaped as a routing audit entry (routetype=public_transport) so _extract_data /
        # _results_view render and narrate it exactly like a routing-derived route.
        return {"name": "routing", "args": json.dumps({"routetype": "public_transport"}), "result": result}

    async def _parking() -> dict[str, Any]:
        # Find car parks near the destination (called only when a car route is in play — the
        # feature is car-specific). Runs concurrently with routing (one flat gather below) so
        # it adds no wall-clock when routing is the long pole. The search has no free-spaces;
        # the live count is fetched per-spot afterwards (_enrich_parking). Returns the entry.
        p_args = {
            "latitude": dest[1],
            "longitude": dest[0],
            "categories": PARKING_CATEGORY,
            "maxdistance": PARKING_RADIUS_KM,
            "maxresults": PARKING_MAX * 3,
        }
        result = await exec_tool(client, "service_search_near_gps_position", p_args)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "tool service_search_near_gps_position %s -> %s",
                p_args,
                json.dumps(slim_result_for_llm("service_search_near_gps_position", result), ensure_ascii=False)[:500],
            )
        return {"name": "service_search_near_gps_position", "args": json.dumps(p_args), "result": result}

    # The modes are independent, so route them concurrently (wall-clock = the slowest one,
    # not the sum); parking (car only) runs alongside in the SAME flat gather, placed last so
    # routing keeps its modes-order append (deterministic _extract_data) and the parking entry
    # comes after. One flat gather (not nested) keeps the call order stable.
    do_parking = "car" in modes
    coros = [(_route_pt() if m == "public_transport" else _route(m)) for m in modes]
    coros += [_parking()] if do_parking else []
    gathered = await asyncio.gather(*coros)
    primary = gathered[: len(modes)]
    parking_entry = gathered[len(modes)] if do_parking else None
    results.extend(primary)
    for m, entry in zip(modes, primary):
        routed = entry["result"]
        if isinstance(routed, dict) and "error" in routed and m in _FOOT_FALLBACK:
            # Single-shot fallback to the other foot profile. The requested profile
            # already exhausted the stale-retry ladder (~27 s), so this only probes the
            # other graph path, not the transient. car/PT failures get no fallback —
            # they just stay absent from the routes, so the dashboard draws one less line.
            results.append(await _route(_FOOT_FALLBACK[m], attempts=1))

    # Parking entry goes after every routing entry so _extract_data (routing-only) is
    # unaffected and _extract_parking finds it deterministically.
    parking: list[dict[str, Any]] = []
    if parking_entry is not None:
        results.append(parking_entry)
        # Build the nearest-N list (parse + Haversine distance + sort), then enrich each with
        # its live free-spaces (service_info_dev per spot, concurrently).
        spots = _extract_parking(results, {"lat": dest[1], "lng": dest[0]})
        if spots:
            parking = await _enrich_parking(client, spots)

    # Surface the precise geocoded endpoints (origin/dest are [lng, lat]) so the front-end
    # pins markers on the real civic address, not on the routing service's road-snapped WKT
    # endpoint (which drifts ~27 m up the street). See docs/lessons.md.
    endpoints = {
        "origin": {"lat": origin[1], "lng": origin[0]},
        "destination": {"lat": dest[1], "lng": dest[0]},
    }
    return {
        "tool_results": results,
        "unsupported": False,
        "endpoints": endpoints,
        "parking": parking,
    }


async def _enrich_parking(
    client: Client, spots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fill each car park's live free/total spaces via service_info_dev (concurrent, one per
    spot), then re-sort so spots with known availability come first (most free first), the
    rest by distance. Plain POI car parks have no realtime → free stays None (degraded
    display). The enrichment calls are NOT added to the audit (internal, not LLM-facing)."""

    async def one(spot: dict[str, Any]) -> dict[str, Any]:
        if not spot.get("uri"):
            return spot
        res = await exec_tool(
            client, "service_info_dev",
            {"serviceuri": spot["uri"], "fromtime": PARKING_REALTIME_FROMTIME},
        )
        rt = read_parking_realtime(res)
        if rt["free_spaces"] is not None:
            spot["free_spaces"] = rt["free_spaces"]
        if rt["total_spaces"] is not None:
            spot["total_spaces"] = rt["total_spaces"]
        return spot

    enriched = await asyncio.gather(*(one(s) for s in spots))
    return sorted(enriched, key=_parking_sort_key)


def _routetype_of(entry: dict[str, Any]) -> str | None:
    """The routetype of a routing audit entry, read back from its json args.
    None when args is absent or malformed (test entries may carry no args)."""
    try:
        return json.loads(entry.get("args") or "{}").get("routetype")
    except (json.JSONDecodeError, TypeError):
        return None


# Maps an MCP routetype to the dashboard vehicle family (mirrors the front-end
# vehicleOf): foot profiles collapse to one walking line, car/PT to their own.
_VEHICLE = {
    "foot_shortest": "foot",
    "foot_quiet": "foot",
    "car": "car",
    "public_transport": "bus",
}


def _route_minutes(route: dict[str, Any]) -> float:
    """A route's travel time in minutes, for ordering routes fastest-first. The server
    gives `duration` as "HH:MM:SS"; an unparseable one sorts last (inf)."""
    dur = route.get("duration")
    if isinstance(dur, (int, float)):
        return float(dur)
    if isinstance(dur, str):
        parts = dur.split(":")
        if len(parts) == 3:
            try:
                h, m, s = (int(p) for p in parts)
                return h * 60 + m + s / 60
            except ValueError:
                pass
    return float("inf")


def _pt_is_foot_only(result: Any) -> bool:
    """A public_transport routing that came back with no real transit leg — only
    walking. On short central trips the server degrades PT to a single foot leg (L19),
    which must NOT be drawn or narrated as a public-transport option. True also for an
    empty/legless journey (nothing rideable). Non-journey results (errors) are not this
    case (handled elsewhere)."""
    if not isinstance(result, dict) or not isinstance(result.get("journey"), dict):
        return False
    first = (result["journey"].get("routes") or [{}])[0]
    legs = group_arc_legs(first.get("arc") or [])
    return not any((leg.get("transport") or "foot") != "foot" for leg in legs)


def _extract_data(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Mine the tool-result audit for the widget payload.

    Collects every successful routing into a `routes` list, one per vehicle family
    (foot/car/bus) — a later success overwrites an earlier one, so a foot-profile
    fallback wins over its failed sibling. routes is ordered fastest-first; the
    top-level wkt/mode/distance mirror routes[0] for single-route consumers and the
    template. With no success, returns the earliest error (the mode the user asked for).
    """
    route_error: str | None = None
    by_vehicle: dict[str, dict[str, Any]] = {}
    for entry in results:
        if entry.get("name") != "routing":
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        if isinstance(result.get("journey"), dict):
            journey = result["journey"]
            first = (journey.get("routes") or [{}])[0]
            routetype = _routetype_of(entry)
            if routetype == "public_transport" and _pt_is_foot_only(result):
                # Degraded to a walking-only journey: not a real PT option, so don't
                # surface a (foot-duplicate) bus line. No route_error either — it
                # "succeeded", just isn't public transport. Logged so this silent drop is
                # distinguishable from a timeout/empty error when diagnosing a missing PT line.
                logger.debug("PT route dropped: foot-only degraded journey (no transit leg)")
                continue
            route = {
                "mode": routetype,
                "wkt": first.get("wkt"),  # full LINESTRING, not truncated
                "distance_km": first.get("distance"),
                "eta": first.get("eta"),
                "duration": first.get("time"),
                # "arcs": first.get("arc"),  # per-segment detail: bloats the payload
                # ~90%, re-enable once referente confirms the widget needs it.
                "source_node": journey.get("source_node"),
                "destination_node": journey.get("destination_node"),
            }
            if routetype == "public_transport":
                # Walk/ride legs grouped from the journey arcs. Pending referente
                # confirmation, same status as data.arcs above.
                legs = group_arc_legs(first.get("arc") or [])
                if legs:
                    route["legs"] = legs
            by_vehicle[_VEHICLE.get(routetype or "", routetype or "")] = route
        elif "error" in result and route_error is None:
            # First error wins = the mode the user actually asked for (modes run in
            # request order), not a later fallback's.
            route_error = result["error"]
    if not by_vehicle:
        return {"route_error": route_error} if route_error else {}
    routes = sorted(by_vehicle.values(), key=_route_minutes)
    return {**routes[0], "routes": routes}


def _parking_sort_key(s: dict[str, Any]) -> tuple[bool, float, float]:
    """Sort key for car parks: spots with a known free-space count first (most free
    first), then by distance; spots with unknown free (realtime not loaded, the agreed
    degraded case) sort after, nearest first."""
    free = s.get("free_spaces")
    dist = s.get("distance_km")
    return (free is None, -(free or 0), dist if dist is not None else float("inf"))


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng points."""
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _extract_parking(
    results: list[dict[str, Any]], dest: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """Mine the parking search entry into the widget payload.

    dest is the geocoded destination {"lat","lng"} (from the endpoints). Each spot gets a
    Haversine distance from dest (the search envelope is not relied on for distance — units
    are unverified, L-style probe discipline). Sort: spots with a known free-space count
    first (most free first), then by distance; spots with unknown free (realtime not loaded,
    the agreed degraded case) sort after, nearest first. Capped to PARKING_MAX. None when
    there is no parking entry or it is empty/errored (route still returned without it)."""
    for entry in results:
        if entry.get("name") != "service_search_near_gps_position":
            continue
        spots = parse_parking_features(entry.get("result"))
        if not spots:
            return None
        for s in spots:
            if dest and s.get("lat") is not None and s.get("lng") is not None:
                s["distance_km"] = round(
                    _haversine_km(dest["lat"], dest["lng"], s["lat"], s["lng"]), 3
                )
            else:
                s["distance_km"] = None

        spots.sort(key=_parking_sort_key)
        return spots[:PARKING_MAX]
    return None


def _template_answer(
    intent: str, data: dict[str, Any], *, unsupported: bool, missing: list[str] | None = None
) -> str:
    """Fallback answer in Italian (the advisor's default) when the respond LLM
    is unavailable."""
    if missing:
        labels = {
            "origin": "il punto di partenza",
            "destination": "la destinazione",
            "line": "la linea (es. '6')",
            "stop": "il nome della fermata",
        }
        asked = " e ".join(labels[m] for m in missing)
        return f"Mi serve ancora {asked} per rispondere."
    if unsupported:
        return (
            "Al momento rispondo a domande su percorsi punto-punto (a piedi, in auto "
            "o con i mezzi pubblici) e su linee, percorsi, fermate e orari del "
            "trasporto pubblico, es. 'da Piazza Duomo a Santa Croce a piedi'."
        )
    if intent in TPL_INTENTS:
        return (
            tpl_template_answer(intent, data)
            or "Mi dispiace, non ho trovato informazioni per questa richiesta."
        )
    routes = data.get("routes") or []
    if len(routes) > 1:
        labels = {"foot": "a piedi", "car": "in auto", "bus": "con i mezzi"}
        parts = []
        for r in routes:
            label = labels.get(_VEHICLE.get(r.get("mode") or "", ""), "percorso")
            bits = []
            if r.get("distance_km") is not None:
                bits.append(f"{r['distance_km']} km")
            if r.get("duration"):
                bits.append(f"~{r['duration']}")
            if bits:
                parts.append(f"{label}: {' · '.join(bits)}")
        if parts:
            return "; ".join(parts)
    if data.get("distance_km") is not None:
        bits = [f"{data['distance_km']} km"]
        if data.get("duration"):
            bits.append(f"~{data['duration']}")
        if data.get("eta"):
            bits.append(f"arrivo {data['eta']}")
        return " · ".join(bits)
    if data.get("route_error"):
        return data["route_error"]
    return "Mi dispiace, non sono riuscito a trovare un percorso per questa richiesta."


# Required slots per intent: route needs both places. The tpl table lives in tpl.py
# (single source: run_tpl_flow skips its chain on the same keys).
_REQUIRED_SLOTS: dict[str, tuple[tuple[str, str], ...]] = {
    "route": (("origin", "origin_text"), ("destination", "destination_text")),
    **TPL_REQUIRED_SLOTS,
}


def _missing_slots(intent: str, slots: dict[str, Any]) -> list[str]:
    """Which required slots of `intent` the extraction left blank."""
    return [
        label
        for label, key in _REQUIRED_SLOTS.get(intent, ())
        if not (slots.get(key) or "").strip()
    ]


def _routing_hint(routetype: str | None, result: Any) -> str | None:
    """Suggestion key for a failed routing attempt.

    Keeps the ZTL-vs-service-side judgement in Python rather than asking the respond
    LLM to pattern-match result["error"]. None means no special hint, and respond's
    generic error rule handles it (geocode failures, transient call errors, etc.).
    """
    if not isinstance(result, dict):
        return None
    if routetype == "public_transport" and _pt_is_foot_only(result):
        # PT came back as a walking-only journey (no transit leg) — not a real PT
        # option. respond must not present it as public transport.
        return "pt_degraded_to_foot"
    err = result.get("error")
    if not isinstance(err, str):
        return None
    if "empty response from routing service" in err:
        # Service-side failure for this mode, not a ZTL/pedestrian restriction.
        return "service_empty_try_foot_or_later"
    if "empty routes list" in err and routetype in ("car", "public_transport"):
        # A car/PT route with no result is often Florence's ZTL/pedestrian core.
        return "car_pt_blocked_try_foot"
    return None


def _results_view(
    results: list[dict[str, Any]], *, unsupported: bool, missing: list[str] | None = None
) -> dict[str, Any]:
    """Compact, LLM-facing summary of what execute produced (no huge WKT)."""
    if missing:
        return {"status": "missing_place", "missing": missing}
    if unsupported:
        return {
            "status": "unsupported",
            "supported": "point-to-point trips (foot, car, public transport) and "
            "public-transport discovery (lines, routes, stops, timetables)",
        }
    view = []
    for e in results:
        name = e.get("name")
        # Parking is shown by the front-end's own list (data.parking); keep it out of the
        # LLM view so the reply doesn't repeat the spots/free-spaces already listed below it.
        if name == "service_search_near_gps_position":
            continue
        slim = (
            slim_tpl_result(name, e.get("result"))
            if name in TPL_TOOL_NAMES
            else slim_result_for_llm(name, e.get("result"))
        )
        item = {"name": name, "result": slim}
        if name == "routing":
            # Surface which mode this attempt used: on failure the LLM can only
            # suggest a sensible alternative ("in auto non si può, prova a piedi")
            # when it knows which mode failed.
            routetype = _routetype_of(e)
            item["routetype"] = routetype
            # The hint carries the ZTL-vs-service judgement so the prompt just
            # follows it instead of pattern-matching the error string.
            hint = _routing_hint(routetype, e.get("result"))
            if hint:
                item["hint"] = hint
        view.append(item)
    return {"status": "ok", "results": view}


async def respond(state: AdvisorState, *, llm: Llama4Client) -> dict[str, Any]:
    """LLM phrases a multilingual answer from the results (no tools), then assembles
    the widget JSON. Falls back to a template if the LLM errors."""
    messages = list(state.get("messages") or [])
    intent = state.get("intent", "other")
    results = state.get("tool_results") or []
    unsupported = bool(state.get("unsupported"))
    # The dashboard widget needs each route's travel mode to render it (foot/car icon +
    # line color). _extract_data already carries `mode` per route (and on the top-level
    # primary), read back from the routetype the route was computed with, so respond
    # adds nothing here. Pending referente confirmation of the widget data shape.
    data = extract_tpl_data(intent, results) if intent in TPL_INTENTS else _extract_data(results)
    # Hand the precise geocoded endpoints to the front-end (route intent only) so it can pin
    # the start/finish markers on the real address instead of the WKT road-snap point.
    endpoints = state.get("endpoints") or {}
    if intent == "route" and data.get("routes") and endpoints:
        data["origin"] = endpoints.get("origin")
        data["destination"] = endpoints.get("destination")
    # Free car parks near the destination (car routes), built + enriched in execute. Injected
    # independently of data["routes"] so it still shows when the car route itself came back
    # empty (ZTL/L8) — "couldn't drive there, but here's parking nearby". data.parking is
    # consumed by our own front-end (like data.origin/destination, L32), not the referente
    # widget contract (rule 8).
    parking = state.get("parking")
    if intent == "route" and parking:
        data["parking"] = parking
    # An intent execute refused means slot extraction left a required slot blank
    # (a place for route, line/stop for tpl). respond then asks for it instead of
    # claiming the request is unsupported.
    missing = (
        _missing_slots(intent, state.get("slots") or {})
        if unsupported and intent in _REQUIRED_SLOTS
        else None
    ) or None

    user_query = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"), ""
    )
    # A prior assistant turn means this is a follow-up, so respond answers directly
    # without re-greeting (RESPOND_SYSTEM keys the greeting off this marker). messages
    # here is [history..., current user]; the assistant turn isn't appended yet.
    is_followup = any(m.get("role") == "assistant" for m in messages)
    view = _results_view(results, unsupported=unsupported, missing=missing)
    answer: str | None = None
    try:
        t0 = time.perf_counter()
        resp = await llm.achat(
            messages=[
                {"role": "system", "content": RESPOND_SYSTEM},
                {
                    "role": "user",
                    "content": f"User asked: {user_query}\n\n"
                    f"Conversation turn: {'follow-up' if is_followup else 'first'}\n\n"
                    "RESULTS:\n" + json.dumps(view, ensure_ascii=False),
                },
            ],
            tool_choice="none",
            # Low temperature to stay grounded: at 0.7 Llama4 invented line numbers and
            # operators (ATAF) when RESULTS lacked them, and sometimes drifted to
            # English. Phrasing room matters less than never fabricating. (Slots use 0.)
            temperature=0.2,
        )
        logger.debug("respond LLM took %.1fs", time.perf_counter() - t0)
        content = assistant_message(resp).get("content")
        if isinstance(content, str) and content.strip():
            answer = content.strip()
    except Llama4Error:
        pass  # fall back to the deterministic template below
    if answer is None:
        answer = _template_answer(intent, data, unsupported=unsupported, missing=missing)

    messages.append({"role": "assistant", "content": answer})
    # Widget JSON. The reply lives in messages[-1].content (OpenAI standard), with no
    # custom top-level `answer` field. status is the JSend-style outcome, request_type
    # names the served intent, data is the route payload, messages is the history to
    # pass back for the next turn.
    return {
        "response": {
            "status": "success",
            "request_type": intent,
            "data": data,
            "messages": messages,  # updated history (last turn = the reply)
        }
    }


def _build_graph(client: Client, llm: Llama4Client, local_client: Client | None = None):
    g = StateGraph(AdvisorState)
    g.add_node("understand", partial(understand, llm=llm))
    g.add_node("execute", partial(execute, client=client, local_client=local_client))
    g.add_node("respond", partial(respond, llm=llm))
    g.set_entry_point("understand")
    g.add_edge("understand", "execute")
    g.add_edge("execute", "respond")
    g.add_edge("respond", END)
    return g.compile()


# Process-wide session state, built lazily on the first turn and reused after.
# Rebuilding per turn would re-fetch /apps.json and re-create the TokenManager (a
# stderr log block per question). Token refresh stays correct: TokenManager.get_token()
# re-checks expiry on every call.
_CFG: dict[str, Any] | None = None  # dashboard /apps.json topology (static)
_LLM: Llama4Client | None = None  # owns the TokenManager


async def _session_deps() -> tuple[dict[str, Any], Llama4Client]:
    # Unlocked check-then-set: concurrent first turns may double-build (harmless, last
    # write wins). An asyncio.Lock would bind to one event loop and break callers that
    # run each request in its own asyncio.run(); neither cached object is loop-bound.
    global _CFG, _LLM
    if _CFG is None:
        _CFG = await _build_config()
    if _LLM is None:
        _LLM = Llama4Client()
    return _CFG, _LLM


async def run_advisor(query: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Multi-turn mobility advisor. Returns widget JSON including updated messages.

    Pass the previous turn's response["messages"] back as `history` to continue the
    conversation (the dashboard front-end carries state this way). The MCP
    Client is reconnected per turn (clean lifecycle, cheap intranet handshake); config
    and LLM client persist for the whole process.
    """
    cfg, llm = await _session_deps()
    messages = list(history or [])
    messages.append({"role": "user", "content": query})
    t0 = time.perf_counter()
    async with Client(cfg) as client, Client(_local_config()) as local_client:
        graph = _build_graph(client, llm, local_client)
        out: AdvisorState = await graph.ainvoke({"messages": messages, "tool_results": []})
    logger.debug("advisor turn total %.1fs", time.perf_counter() - t0)
    return out.get("response", {"status": "error", "error": "no response produced", "messages": messages})
