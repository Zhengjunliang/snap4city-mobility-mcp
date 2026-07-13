"""Langgraph mobility advisor: the orchestration graph.

A natural-language query runs through a linear graph: understand -> execute ->
respond -> END.

- understand (LLM, forced tool call): extracts the request slots (intent, origin,
  destination, category, mode, departure time) from the latest user turn, place text only.
  Follow-ups like "那坐公交呢?" resolve against the conversation history.
- execute (plain Python, no LLM): for a route intent, resolves both endpoints then routes the
  requested modes concurrently. A missing origin defaults to the user's GPS position (browser
  geolocation, threaded through run_advisor); a generic-category destination ("farmacia più
  vicina") resolves via the nearest-service search; text places geocode with GPS-nearest
  candidate picking. "other" falls through to an "unsupported" reply.
- respond (LLM, no tools): phrases a multilingual answer from the results and
  assembles the widget JSON (with the full route WKT and the updated messages).

The model never picks tools itself: in agentic mode Llama4 tends to emit tool calls
as pythonic text instead of structured tool_calls, which then leaks into the answer.
Letting it pick only slots and prose, with Python driving the tools, avoids that.

MCP execution helpers live in mcp_tools.py, the Llama4 client in llm.py. Runs
end-to-end only on the Snap4City JupyterHub.
"""
import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from fastmcp import Client
from langgraph.graph import END, StateGraph

from snap4city_mobility_mcp.geo import haversine_km
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
    parse_service_features,
    read_parking_realtime,
    reverse_geocode,
    slim_result_for_llm,
)

logger = logging.getLogger(__name__)

# The network's own timezone. Every user-facing and router-facing time lives in it: the
# What-If servlet parses `startdatetime` as a LOCAL datetime in this zone, so a naive now()
# from a UTC process would query the GTFS timetable 2h off (L39/L40), and the understand
# prompt needs today's Rome date to turn "domani alle 9" into a dated slot.
ROME = ZoneInfo("Europe/Rome")

UNDERSTAND_SYSTEM = """\
You are the intent-extraction stage of an urban-mobility advisor. Read the \
conversation and the user's LATEST message, then call `extract_slots` exactly once. \
Classify request_type and mode per each field's own description in the schema.
Rules:
- Always fill EVERY field of `extract_slots` (use '' for a slot the user truly did \
not give). Never drop the destination when the user named one.
- Extract PLACE TEXT only (e.g. "Piazza del Duomo, Firenze"). NEVER output \
coordinates — a separate tool geocodes places. Keep a city/town the user names \
attached to its place text, but NEVER add a city — or any place — the user did not say.
- When the user gives NO origin ("portami al Duomo", "come arrivo in stazione?", \
"da qui") leave origin_text '' — the system defaults to the user's own position. \
Never invent an origin.
- Ignore greetings and pleasantries ("ciao", "hello", "per favore") — they never \
change the slots.
- For a follow-up that omits a place (e.g. "what about by bus?", "那坐公交呢?"), \
reuse the origin/destination from earlier in the conversation and change only what \
the user changed (here mode → public_transport). An origin the user never stated \
stays '' on follow-ups too.
- Today is {today} (Europe/Rome). A departure time the user gives ("alle 18", "domani \
alle 9") goes in departure_time; leave it '' when they give none. NEVER invent one.
<examples>
"ciao, voglio andare da stazione di Rifredi a piazza Dalmazia a piedi" → request_type=journey, origin_text="stazione di Rifredi", destination_text="piazza Dalmazia", mode=foot (all other slots '')
"da piazza Duomo a piazza Dalmazia in Firenze" → request_type=journey, origin_text="piazza Duomo, Firenze", destination_text="piazza Dalmazia, Firenze"
"portami al Duomo" → request_type=journey, origin_text='', destination_text="Duomo"
"dov'è la farmacia più vicina?" → request_type=journey, origin_text='', destination_text="farmacia", destination_category="Pharmacy"
"e in bus?" (follow-up to the piazza Duomo → piazza Dalmazia trip) → request_type=journey, origin_text="piazza Duomo, Firenze", destination_text="piazza Dalmazia, Firenze", mode=public_transport
"da Santa Croce alla stazione in bus alle 18" → request_type=journey, origin_text="Santa Croce", destination_text="stazione", mode=public_transport, departure_time="18:00"
</examples>"""

RESPOND_SYSTEM = """\
You are a friendly urban-mobility assistant. ALWAYS reply in the \
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
narrate the trip leg by leg (walk to the boarding stop, ride the <line> of <provider> \
toward <headsign> from the board stop to the alight stop, then walk on), using ONLY the \
leg fields — the leg's `line`, `provider`, `headsign`, `board` and `alight` (each a stop \
name + time) and `stops_total`. Give the scheduled boarding and arrival \
times (`board.time` / `alight.time`); write every time \
you mention as plain \
HH:MM (never seconds or dates) and each distance once, in km. NO disclaimers — never \
comment on real-time information, traffic, dates, data validity, or timetable accuracy. \
You may say \
how many stops the ride covers (`stops_total`) and name the board/alight stops, but the \
stops in between are NOT given: never invent one, nor a line, operator, or time not in \
the fields. A \
route that carries a distance HAS BEEN FOUND: present it directly and NEVER ask the user \
to restate, clarify, or give a nearby landmark for the origin/destination — they were \
already located. When a bus route carries a `duration`, present it as an approximate ride \
time (walking + in-vehicle, excluding the wait at the stop), not a precise arrival. If a \
route has no duration/ETA at all (e.g. a bus route with no timetable), give its distance \
and main streets and simply note the schedule/time is not available — do not invent one \
and do not treat it as a failure.
- When RESULTS holds more than one successful route for the same trip (different travel \
modes), give each mode its own distance and duration and say which is faster, using \
ONLY the RESULTS fields.
- When RESULTS carries a `departure_time`, the trip was planned for that departure: say so \
plainly (e.g. "partendo alle 18:00"), using that field as written. With no such field, \
never mention a departure time.
- If a RESULTS item could not be computed (an `error`, or a route/place not found), \
say so plainly WITHOUT any numbers and suggest a sensible alternative (another mode, a \
more precise address); when geocoded addresses are present, mention how you read the \
origin/destination so the user can spot a wrong match.
- If a routing RESULTS item carries a `hint`, follow it for the alternative you \
suggest — it already decided the right one: `pt_degraded_to_foot` = the \
public-transport request returned a walking-only journey (no real transit), so if \
other modes are present do NOT list it as a public-transport option, and if it is the \
only result say there is no direct public transport for this trip and give the walking \
distance/time instead. With NO `hint`, never claim a ZTL/pedestrian zone yourself.
- If RESULTS has status "missing_place", ask the user for the field(s) listed in \
`missing` (the origin/destination of a trip); when "origin" is missing you may also \
suggest sharing the position — do NOT say the request is unsupported.
- When RESULTS includes a `coordinates_to_address` entry, the trip starts from the \
user's current GPS position: say so ("dalla tua posizione", optionally "vicino a \
<address>" using ONLY that entry's address). If the user gave no origin and there is \
no such entry, say "dalla tua posizione attuale" without naming any street.
- A `service_search_near_gps_position` entry whose `categories` is `Car_park` is \
parking near the destination: add ONE short closing sentence — how many car parks are \
nearby and, if any `free_spaces` is a number > 0, that there are free spots (else that \
live availability is not known right now). Do NOT list the car-park names, addresses, \
or coordinates: the map already shows their pins. Keep it to that single sentence.
- A `service_search_near_gps_position` entry with any OTHER `categories` is how the \
destination was resolved — the nearest place of that kind: name the found place (the \
first listed service) naturally in the answer.
- If RESULTS has status "unsupported", explain in your own words that for now you \
answer point-to-point trip questions (on foot, by car, or by public transport), \
including trips to the nearest place of a kind ("la farmacia più vicina"), and invite \
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
                    "enum": ["journey", "other"],
                    "description": "Classify by STRUCTURE, not vocabulary. 'journey' = the user wants to get somewhere: from an origin to a destination (named, carried over from earlier in the chat, or implicitly from their own position), including reaching the nearest place of some kind — use 'journey' even when transit words like bus/line/tram appear. 'other' = anything else (including network reference questions — which lines exist, stop timetables — that this advisor does not answer).",
                },
                "origin_text": {
                    "type": "string",
                    "description": "Free-text origin place name exactly as the user said it; '' if absent (the system then starts from the user's own position).",
                },
                "destination_text": {
                    "type": "string",
                    "description": "Free-text destination in the user's own words ('farmacia' counts), '' if absent.",
                },
                "destination_category": {
                    "type": "string",
                    "description": "Only when the destination is a GENERIC KIND of place rather than a named one ('la farmacia più vicina', 'un supermercato'): the matching English km4city service category, e.g. Pharmacy, Hospital, Supermarket, Museum, Hotel, Restaurant, Car_park, Fuel_station. '' when the destination is a named place.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["car", "public_transport", "foot", ""],
                    "description": "Travel mode, '' if not specified. Map: walk / on foot / a piedi → foot; drive / car → car; bus / tram / public transport / 公交 → public_transport.",
                },
                "departure_time": {
                    "type": "string",
                    "description": "When the user wants to LEAVE, if they said so: 'HH:MM' for a time today ('alle 18' → '18:00'), or 'YYYY-MM-DDTHH:MM' when they name another day ('domani alle 9'). '' when the user gave no time — never invent one, and never put an ARRIVAL time here ('arrivare per le 9' gives no departure).",
                },
            },
            # Mark every field required: Llama4 only fills required params and
            # silently drops optional ones (one run extracted the origin but lost
            # the destination). An empty string '' marks a slot the user didn't give.
            "required": [
                "request_type", "origin_text", "destination_text",
                "destination_category", "mode", "departure_time",
            ],
        },
    },
}


class AdvisorState(TypedDict, total=False):
    messages: list[dict[str, Any]]  # chat history (system/user/assistant) for multi-turn
    intent: str  # route | other
    slots: dict[str, Any]  # understand output (request_type, intent, origin_text, ...)
    user_gps: dict[str, Any]  # browser GPS {lat,lng} (sanitized by api.py), absent/None without consent
    tool_results: list[dict[str, Any]]  # audit: [{name, args, result}] per call
    unsupported: bool  # execute could not run a flow ("other" intent or missing slots)
    endpoints: dict[str, Any]  # precise resolved {origin, destination} {lat,lng} for the route
    parking: list[dict[str, Any]]  # car parks near the destination (car routes), with live free-spaces
    departure: str  # requested departure "HH:MM" ('' = leave now), for the reply to state
    response: dict[str, Any]  # widget JSON assembled by respond


def _request_to_intent(slots: dict[str, Any]) -> str:
    """Map the LLM classification to the internal `intent` string the graph dispatches on."""
    return "route" if slots.get("request_type") == "journey" else "other"


# What a node is busy with, reported to whoever started the turn (the bridge relays it to the
# chat box, which is otherwise a blank "thinking" bubble for the 30-45s a bus route costs).
# The graph is strictly linear, so the stages arrive in order.
StageFn = Callable[[str], None]


def _emit(on_stage: StageFn | None, stage: str) -> None:
    """Report the current stage, if anyone is listening. Never raises: a progress hiccup must
    not sink a turn that is otherwise producing a route."""
    if on_stage is None:
        return
    try:
        on_stage(stage)
    except Exception:  # noqa: BLE001 - progress is cosmetic; the turn is not
        logger.debug("on_stage(%r) raised, ignoring", stage, exc_info=True)


async def understand(
    state: AdvisorState, *, llm: Llama4Client, on_stage: StageFn | None = None
) -> dict[str, Any]:
    """LLM extracts slots from the latest user turn via a forced tool call.

    The forced tool_choice makes the gateway return structured tool_calls, so this
    stage avoids the pythonic-text shape that breaks free tool use.
    """
    _emit(on_stage, "understand")
    history = state["messages"]
    convo = [m for m in history if m.get("role") in ("user", "assistant")]
    slots: dict[str, Any] = {"intent": "other"}
    # Today's Rome date goes into the prompt: without it the model cannot date "domani alle 9"
    # (it has no clock), and a dateless departure slot cannot be told apart from today's.
    system = UNDERSTAND_SYSTEM.format(today=datetime.now(ROME).strftime("%Y-%m-%d"))
    try:
        t0 = time.perf_counter()
        resp = await llm.achat(
            messages=[{"role": "system", "content": system}, *convo],
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


def _feature_coords(feature: dict[str, Any]) -> list[float] | None:
    """A feature's [lng, lat] as floats, or None when the geometry is unusable."""
    coords = (feature.get("geometry") or {}).get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lng, lat = coords[0], coords[1]
        if isinstance(lng, (int, float)) and isinstance(lat, (int, float)):
            return [float(lng), float(lat)]
    return None


def _pick_coord(
    geocode: Any, search: str, gps: dict[str, Any] | None = None
) -> list[float] | None:
    """Best feature's [lng, lat] for `search` from a geocode result, or None.

    The server sometimes ranks a fuzzy POI hit above the real place (once a company
    1.1 km west of "Piazza Duomo"), so the candidate pool prefers features whose
    address/name tokens are all covered by the search text (rejecting extra-token
    labels). When no label matches (e.g. stations, whose features carry no address)
    the pool is the full list. With `gps` (the user's position) the nearest pool
    candidate wins (haversine — the geocoder's own proximity bias is a no-op, probed
    2026-07-09); without it, the pool's first (best-score) hit wins, as before.
    """
    if not isinstance(geocode, dict) or "error" in geocode:
        return None
    features = geocode.get("features")
    if not (isinstance(features, list) and features):
        return None
    want = _label_tokens(search)
    pool = features
    if want:
        matching = []
        for f in features:
            props = f.get("properties") or {}
            label = " ".join(str(v) for v in (props.get("address"), props.get("name")) if v)
            toks = _label_tokens(label)
            # Skip a label that is just the municipality ("FIRENZE"): it matches any
            # search ending in ", Firenze" but is never a useful pick.
            if toks and toks <= want and not toks <= _label_tokens(str(props.get("city") or "")):
                matching.append(f)
        if matching:
            pool = matching
    best = None
    if gps:
        best_dist = None
        for f in pool:
            c = _feature_coords(f)
            if c is None:
                continue
            dist = haversine_km(gps["lat"], gps["lng"], c[1], c[0])
            if best_dist is None or dist < best_dist:
                best_dist, best = dist, f
    if best is None:
        best = pool[0]
    logger.debug(
        "geocode %r picked feature (address=%r, gps=%s)",
        search, (best.get("properties") or {}).get("address"), bool(gps),
    )
    return _feature_coords(best)


# Nearest-category destination search: widening radius ladder (km) around the anchor
# (the user's GPS, or the geocoded destination text without one). An empty rung means
# "no such service within <radius>", so the next rung widens; all-empty falls back to
# the plain text geocode. The near results come back distance-sorted with a `distance`
# field (probed 2026-07-09), so [0] is the nearest.
NEAREST_SERVICE_RADII_KM = (0.5, 2.0, 10.0)
NEAREST_SERVICE_MAX = 10


def _parse_departure(text: str, now: datetime) -> datetime | None:
    """The user's requested departure as a Rome-aware datetime, or None when they gave none
    (or gave something unusable — the caller then departs now, never guesses).

    The understand slot is "HH:MM" (a time today) or "YYYY-MM-DDTHH:MM" (a dated one, for
    "domani alle 9" — the prompt carries today's Rome date so the model can date it). A bare
    HH:MM already past rolls to tomorrow: "alle 8" asked at 22:00 means the next 8 o'clock,
    and departing in the past would query a dead GTFS window.
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        if "T" in t:
            dt = datetime.fromisoformat(t)
            return dt.replace(tzinfo=ROME) if dt.tzinfo is None else dt.astimezone(ROME)
        h, m = (int(p) for p in t.split(":", 1))
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (TypeError, ValueError):
        logger.debug("departure_time %r unparseable: departing now", text)
        return None
    return when + timedelta(days=1) if when < now else when


def _audit(name: str, args: dict[str, Any], result: Any, *, extra: str = "") -> dict[str, Any]:
    """Audit entry for one tool call, debug-logging its slim view along the way.

    The entry keeps the full payload (respond and the widget mine it); only the log line
    is slimmed. `extra` appends what the slim view drops (e.g. the picked coordinate)."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "tool %s %s -> %s%s",
            name,
            args,
            json.dumps(slim_result_for_llm(name, result), ensure_ascii=False)[:500],
            extra,
        )
    return {"name": name, "args": json.dumps(args), "result": result}


async def execute(
    state: AdvisorState,
    *,
    client: Client,
    local_client: Client | None = None,
    on_stage: StageFn | None = None,
) -> dict[str, Any]:
    """Run the tool flow for the extracted intent (no LLM).

    route: resolve both endpoints, then route the requested modes concurrently (every mode goes
    to the local What-If `route` tool). The origin defaults to the user's GPS position when no
    text was given (reverse-geocoded once so respond can name it); a generic-category
    destination resolves to the nearest service (see _nearest_service). Every call is
    recorded in tool_results so respond can mine the widget data. "other" intent or
    an unresolvable endpoint sets unsupported (respond asks for what's missing).
    """
    slots = state.get("slots") or {}
    user_gps = state.get("user_gps") or None
    logger.debug("execute user_gps=%s", user_gps)
    results: list[dict[str, Any]] = []
    # Forward geocoding AND routing go to our local MCP server (L29; L46 — the referente
    # remote `routing` tool is retired); reverse geocode and near-search stay on the remote
    # client. Tests pass only `client`, so lc falls back to it.
    lc = local_client or client

    if slots.get("intent") != "route":
        return {"tool_results": results, "unsupported": True}

    origin_text = (slots.get("origin_text") or "").strip()
    dest_text = (slots.get("destination_text") or "").strip()
    dest_category = (slots.get("destination_category") or "").strip()
    # An endpoint is resolvable when the user gave text, or something covers it: GPS
    # covers a missing origin; GPS anchors a text-less category destination. Mirrors
    # _missing_route_slots so respond asks exactly for what execute refused on.
    if not (origin_text or user_gps) or not (dest_text or (dest_category and user_gps)):
        return {"tool_results": results, "unsupported": True}

    _emit(on_stage, "geocode")

    async def _geocode(search: str, anchor: dict[str, Any] | None = None) -> list[float] | None:
        # `anchor` disambiguates same-name streets across towns (with no named city and
        # no GPS, the server's first hit once put a Florence trip's "via Pisana" in
        # Lucca): the pool candidate nearest to it wins. The destination anchors on the
        # resolved origin (endpoints of one trip are usually neighbours), the origin on
        # the user's GPS; a city the user named still dominates either (the feature pool
        # was already narrowed upstream, mcp_tools._narrow_by_city).
        args = {"search": search}
        result = await exec_tool(lc, "address_search_location", args)
        coord = _pick_coord(result, search, gps=anchor)
        results.append(_audit("address_search_location", args, result, extra=f" (picked {coord})"))
        return coord

    async def _nearest_service(anchor: list[float], category: str) -> list[float] | None:
        # Nearest service of `category` around anchor [lng, lat], widening the radius per
        # rung. Only the deciding call enters the audit: the winning rung (respond names
        # the found place from it), or the last empty one (so respond can explain a miss).
        entry: dict[str, Any] | None = None
        for radius in NEAREST_SERVICE_RADII_KM:
            n_args = {
                "latitude": anchor[1],
                "longitude": anchor[0],
                "categories": category,
                "maxdistance": radius,
                "maxresults": NEAREST_SERVICE_MAX,
            }
            result = await exec_tool(client, "service_search_near_gps_position", n_args)
            entry = _audit("service_search_near_gps_position", n_args, result)
            spots = parse_service_features(result)
            if spots:
                results.append(entry)
                nearest = spots[0]  # server returns distance-sorted features
                logger.debug(
                    "nearest %s within %s km: %r", category, radius, nearest.get("name")
                )
                if nearest.get("lat") is not None and nearest.get("lng") is not None:
                    return [nearest["lng"], nearest["lat"]]
                return None
        if entry is not None:
            results.append(entry)
        logger.debug("nearest %s: no service within %s km", category, NEAREST_SERVICE_RADII_KM[-1])
        return None

    # --- origin: user text, else the GPS position itself (labelled via reverse geocode).
    if origin_text:
        origin = await _geocode(origin_text, user_gps)
    else:
        origin = [user_gps["lng"], user_gps["lat"]]
        rev_args = {"latitude": user_gps["lat"], "longitude": user_gps["lng"]}
        rev = await reverse_geocode(client, user_gps["lat"], user_gps["lng"])
        if isinstance(rev, dict) and "error" not in rev:
            # Only a successful lookup enters the audit: respond keys "dalla tua
            # posizione (vicino a ...)" off this entry, and a failure entry would
            # trigger its error rule for a trip that is actually fine.
            results.append(_audit("coordinates_to_address", rev_args, rev))

    # --- destination: nearest-category service when asked, else plain text geocode.
    # Its geocodes anchor on the resolved origin (see _geocode), falling back to GPS.
    dest_anchor = {"lat": origin[1], "lng": origin[0]} if origin is not None else user_gps
    dest = None
    if dest_category:
        geocoded = None
        if user_gps:
            anchor = [user_gps["lng"], user_gps["lat"]]
        else:
            # No GPS: anchor on the geocoded destination text ("farmacia, Pisa" lands in
            # Pisa via the named-city ladder), then snap to the nearest real service.
            geocoded = await _geocode(dest_text, dest_anchor)
            anchor = geocoded
        if anchor is not None:
            dest = await _nearest_service(anchor, dest_category)
        if dest is None:
            # Category miss (bad category name / nothing within the widest rung):
            # degrade to the text geocode so the trip still resolves when possible.
            # Without GPS the text was already geocoded above (never re-call it).
            dest = geocoded if not user_gps else (await _geocode(dest_text, dest_anchor) if dest_text else None)
    else:
        dest = await _geocode(dest_text, dest_anchor)

    if origin is None or dest is None:
        return {"tool_results": results, "unsupported": False}  # geocode error: respond explains

    mode_specified = bool(slots.get("mode"))
    # No mode given: route ALL THREE modes concurrently, so a plain "from A to B" answers with a
    # walking, a driving and a public-transport option (one line each on the map) and the reply
    # compares them. The wall clock is the slowest coroutine, and until referente merges the
    # pt-router-singleton patch that is the bus one: every vehicle=bus request rebuilds the PT
    # graph server-side (~30-45s, probed 2026-07-13, vs sub-second for foot/car). That cost is
    # accepted for the comparison (the chat box shows the stage + elapsed while it runs); it
    # drops to nothing once the patch lands. An explicit mode runs that one only — asking for
    # "a piedi" must not pay the bus latency.
    modes = [slots["mode"]] if mode_specified else ["foot", "car", "public_transport"]
    # A departure the user asked for ("alle 18"), else None = leave now. Only the public
    # transport leg can honour it (it is the only mode with a timetable).
    departure = _parse_departure(slots.get("departure_time") or "", datetime.now(ROME))
    if departure:
        logger.debug("departure requested: %s", departure.strftime("%Y-%m-%dT%H:%M"))

    async def _route(mode: str) -> dict[str, Any]:
        # EVERY mode goes to the local `route` tool (What-If GraphHopper, mcp_server.py —
        # the referente remote `routing` tool is retired, L46), so the three modes share
        # one request/response shape. GeoJSON coordinate order is [longitude, latitude].
        # Returns the audit entry; the caller appends it, so concurrent calls don't race
        # on the shared results list.
        args: dict[str, Any] = {
            "start_latitude": origin[1],
            "start_longitude": origin[0],
            "end_latitude": dest[1],
            "end_longitude": dest[0],
            "vehicle": _ROUTER_VEHICLE.get(mode, mode),
        }
        if mode == "public_transport":
            # The GTFS timetable window: the user's requested departure, else now. Pinned to
            # the network's timezone either way — the servlet parses this as a LOCAL datetime
            # in its own zone, so a naive now() from a UTC process would query the timetable
            # 2h off (wrong service window on time-sensitive trips). Only public transport has
            # a timetable: GraphHopper has no time-dependent foot/car model, so passing a
            # departure there would imply an accuracy that does not exist.
            args["startdatetime"] = (departure or datetime.now(ROME)).strftime("%Y-%m-%dT%H:%M")
        start = time.perf_counter()
        result = await exec_tool(lc, "route", args)
        # Per-mode latency in debug.log. foot/car are sub-second; bus reloads the PT graph
        # (~30-45s) until the singleton patch lands — so a missing PT line is a foot-only
        # degrade / route-not-found, NOT a slow call timing out.
        logger.debug("routing mode=%s took %.1fs", mode, time.perf_counter() - start)
        # Audited under the historical "routing" name (routetype = the slot mode) so
        # _extract_data / _results_view render and narrate every mode uniformly.
        return _audit("routing", {"routetype": mode, **args}, result)

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
        return _audit("service_search_near_gps_position", p_args, result)

    # The modes are independent, so route them concurrently (wall-clock = the slowest one,
    # not the sum); parking (car only) runs alongside in the SAME flat gather, placed last so
    # routing keeps its modes-order append (deterministic _extract_data) and the parking entry
    # comes after. One flat gather (not nested) keeps the call order stable.
    do_parking = "car" in modes
    # Emitted ONCE here, not inside _route: the mode coroutines run concurrently, so reporting
    # from each of them would fire once per mode, in an order set by whoever suspends first. A bus
    # leg gets its own stage name because it is the one that makes the user wait (the router
    # rebuilds its PT graph per request until the perf patch lands) — the chat box says so.
    _emit(on_stage, "routing_bus" if "public_transport" in modes else "routing")
    coros = [_route(m) for m in modes]
    coros += [_parking()] if do_parking else []
    gathered = await asyncio.gather(*coros)
    primary = gathered[: len(modes)]
    parking_entry = gathered[len(modes)] if do_parking else None
    results.extend(primary)

    # Parking is only meaningful when a car route was actually found: if the car routing
    # failed (ZTL / route-not-found), there is nowhere to drive to, so we drop the parking
    # (the search was fetched concurrently above to add no wall-clock, and is simply discarded
    # here). Parking entry goes after every routing entry so _extract_data (routing-only) is
    # unaffected and _extract_parking finds it deterministically.
    car_ok = any(
        m == "car"
        and isinstance(entry["result"], dict)
        and isinstance(entry["result"].get("journey"), dict)
        for m, entry in zip(modes, primary)
    )
    parking: list[dict[str, Any]] = []
    if parking_entry is not None and car_ok:
        results.append(parking_entry)
        # Build the nearest-N list (parse + Haversine distance + sort), then enrich each with
        # its live free-spaces (service_info_dev per spot, concurrently). The entry is passed
        # directly (not scanned from results): a category-destination search logs an entry
        # under the same tool name, and only this one is parking.
        spots = _extract_parking(parking_entry, {"lat": dest[1], "lng": dest[0]})
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
        # HH:MM only (never a date or seconds, L43): this is the one form the reply may print.
        # Absent when the user asked for no particular time — respond then says nothing about
        # departure rather than announcing "now".
        "departure": departure.strftime("%H:%M") if departure else "",
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


# Slot mode -> What-If router vehicle (the slot keeps the user-facing public_transport
# name; the router speaks vehicle=bus).
_ROUTER_VEHICLE = {"foot": "foot", "car": "car", "public_transport": "bus"}

# Maps a slot mode to the dashboard vehicle family (mirrors the front-end vehicleOf).
_VEHICLE = {
    "foot": "foot",
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
    (foot/car/bus). routes is ordered fastest-first; the top-level wkt/mode/distance
    mirror routes[0] for single-route consumers and the template. With no success,
    returns the earliest error (the mode the user asked for).
    """
    route_error: str | None = None
    by_vehicle: dict[str, dict[str, Any]] = {}
    pt_walk: dict[str, Any] | None = None
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
            route = {
                "mode": routetype,
                "wkt": first.get("wkt"),  # full LINESTRING, not truncated
                "distance_km": first.get("distance"),
                "duration": first.get("time"),
                # "arcs": first.get("arc"),  # per-segment detail: bloats the payload
                # ~90%, re-enable once referente confirms the widget needs it.
            }
            if isinstance(first.get("legs"), list) and first["legs"]:
                # Per-leg geometry (walk/ride split) from the route tool: the dashboard
                # draws the colored split + stop pins from this, no second router call (L44).
                route["legs"] = first["legs"]
            if routetype == "public_transport" and _pt_is_foot_only(result):
                # Walking-only journey: not a real PT option (respond gets the
                # pt_degraded_to_foot hint and says so), but the walk itself is real —
                # keep it as a foot candidate so an explicit bus request still gets a
                # drawable walking line instead of nothing (L39).
                logger.debug("PT route degraded: foot-only journey (no transit leg)")
                route["mode"] = "foot"
                pt_walk = route
                continue
            by_vehicle[_VEHICLE.get(routetype or "", routetype or "")] = route
        elif "error" in result and route_error is None:
            # First error wins = the mode the user actually asked for (modes run in
            # request order), not a later fallback's.
            route_error = result["error"]
    if pt_walk is not None and "foot" not in by_vehicle:
        # Only when no real foot route exists (explicit-bus request); in a multi-mode
        # run the genuine foot result wins and the degraded PT walk is a dup.
        by_vehicle["foot"] = pt_walk
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


def _extract_parking(
    entry: dict[str, Any], dest: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """Mine the parking search entry into the widget payload.

    entry is the Car_park search's audit entry (passed directly by execute — a
    category-destination search logs an entry under the same tool name). dest is the
    resolved destination {"lat","lng"} (from the endpoints). Each spot gets a Haversine
    distance from dest (the search envelope is not relied on for distance — units are
    unverified, L-style probe discipline). Sort: spots with a known free-space count
    first (most free first), then by distance; spots with unknown free (realtime not
    loaded, the agreed degraded case) sort after, nearest first. Capped to PARKING_MAX.
    None when the entry is empty/errored (route still returned without it)."""
    spots = parse_service_features(entry.get("result"))
    if not spots:
        return None
    for s in spots:
        if dest and s.get("lat") is not None and s.get("lng") is not None:
            s["distance_km"] = round(
                haversine_km(dest["lat"], dest["lng"], s["lat"], s["lng"]), 3
            )
        else:
            s["distance_km"] = None

    spots.sort(key=_parking_sort_key)
    return spots[:PARKING_MAX]


def _template_answer(
    data: dict[str, Any], *, unsupported: bool, missing: list[str] | None = None
) -> str:
    """Fallback answer in Italian (the advisor's default) when the respond LLM
    is unavailable."""
    if missing:
        labels = {
            "origin": "il punto di partenza (o la tua posizione)",
            "destination": "la destinazione",
        }
        asked = " e ".join(labels[m] for m in missing)
        return f"Mi serve ancora {asked} per rispondere."
    if unsupported:
        return (
            "Al momento rispondo a domande su percorsi punto-punto (a piedi, in auto "
            "o con i mezzi pubblici), anche verso il luogo più vicino di un certo "
            "tipo, es. 'da Piazza Duomo a Santa Croce a piedi' o 'portami alla "
            "farmacia più vicina'."
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


def _missing_route_slots(slots: dict[str, Any], user_gps: Any) -> list[str]:
    """Which route endpoints are unresolvable (mirrors execute's endpoint gate).

    GPS covers a missing origin; a category destination is resolvable with GPS even
    without destination text."""
    missing = []
    if not (slots.get("origin_text") or "").strip() and not user_gps:
        missing.append("origin")
    if not (slots.get("destination_text") or "").strip() and not (
        (slots.get("destination_category") or "").strip() and user_gps
    ):
        missing.append("destination")
    return missing


def _routing_hint(routetype: str | None, result: Any) -> str | None:
    """Suggestion key for a routing result respond must not take at face value.

    Keeps the walking-only-PT judgement in Python rather than asking the respond LLM
    to pattern-match the journey. None means no special hint, and respond's generic
    error rule handles plain failures (geocode misses, router errors, transient call
    errors). The km4city-specific hints (stale/ZTL error-string matching) retired with
    the remote routing tool (L46): the What-If GraphHopper knows nothing about ZTLs, so
    there is no blocked-zone signal to translate anymore.
    """
    if routetype == "public_transport" and _pt_is_foot_only(result):
        # PT came back as a walking-only journey (no transit leg) — not a real PT
        # option. respond must not present it as public transport.
        return "pt_degraded_to_foot"
    return None


def _results_view(
    results: list[dict[str, Any]],
    *,
    unsupported: bool,
    missing: list[str] | None = None,
    departure: str = "",
) -> dict[str, Any]:
    """Compact, LLM-facing summary of what execute produced (no huge WKT).

    `departure` is the HH:MM the user asked to leave at ('' = they asked for no time): it
    rides at the root, not on a routing item, because it applies to the whole request."""
    if missing:
        return {"status": "missing_place", "missing": missing}
    if unsupported:
        return {
            "status": "unsupported",
            "supported": "point-to-point trips (foot, car, public transport), "
            "including trips to the nearest place of a kind (e.g. nearest pharmacy)",
        }
    view = []
    for e in results:
        name = e.get("name")
        # Near-search entries ARE shown to the LLM (slimmed to count + per-spot name/
        # free_spaces by slim_result_for_llm): the parking one feeds the availability
        # sentence, a category-destination one names the found place. The map still
        # plots parking pins (data.parking); the reply no longer lists spot names.
        item = {"name": name, "result": slim_result_for_llm(name, e.get("result"))}
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
        elif name == "service_search_near_gps_position":
            # Surface the searched category: the prompt tells parking (Car_park) apart
            # from a nearest-category destination by this field.
            try:
                item["categories"] = json.loads(e.get("args") or "{}").get("categories")
            except (json.JSONDecodeError, TypeError):
                pass
        view.append(item)
    out = {"status": "ok", "results": view}
    if departure:
        out["departure_time"] = departure
    return out


async def respond(
    state: AdvisorState, *, llm: Llama4Client, on_stage: StageFn | None = None
) -> dict[str, Any]:
    """LLM phrases a multilingual answer from the results (no tools), then assembles
    the widget JSON. Falls back to a template if the LLM errors."""
    _emit(on_stage, "respond")
    messages = list(state.get("messages") or [])
    intent = state.get("intent", "other")
    results = state.get("tool_results") or []
    unsupported = bool(state.get("unsupported"))
    # The dashboard widget needs each route's travel mode to render it (foot/car icon +
    # line color). _extract_data already carries `mode` per route (and on the top-level
    # primary), read back from the routetype the route was computed with, so respond
    # adds nothing here. Pending referente confirmation of the widget data shape.
    data = _extract_data(results)
    # Hand the precise geocoded endpoints to the front-end (route intent only) so it can pin
    # the start/finish markers on the real address instead of the WKT road-snap point.
    endpoints = state.get("endpoints") or {}
    if intent == "route" and data.get("routes") and endpoints:
        data["origin"] = endpoints.get("origin")
        data["destination"] = endpoints.get("destination")
    # Free car parks near the destination, built + enriched in execute ONLY when a car route
    # was actually found (execute drops parking on a car failure — nowhere to drive to). So
    # state["parking"] is present only for a successful car route, and injecting it here needs
    # no extra guard. data.parking is consumed by our own front-end (like data.origin/
    # destination, L32), not the referente widget contract (rule 8).
    parking = state.get("parking")
    if intent == "route" and parking:
        data["parking"] = parking
    # A route intent execute refused means an endpoint was unresolvable (no text and
    # nothing covering it). respond then asks for it instead of claiming the request
    # is unsupported; a GPS-covered origin is never asked for.
    missing = (
        _missing_route_slots(state.get("slots") or {}, state.get("user_gps"))
        if unsupported and intent == "route"
        else None
    ) or None

    user_query = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"), ""
    )
    # A prior assistant turn means this is a follow-up, so respond answers directly
    # without re-greeting (RESPOND_SYSTEM keys the greeting off this marker). messages
    # here is [history..., current user]; the assistant turn isn't appended yet.
    is_followup = any(m.get("role") == "assistant" for m in messages)
    view = _results_view(
        results,
        unsupported=unsupported,
        missing=missing,
        departure=state.get("departure") or "",
    )
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
        answer = _template_answer(data, unsupported=unsupported, missing=missing)

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


def _build_graph(
    client: Client,
    llm: Llama4Client,
    local_client: Client | None = None,
    on_stage: StageFn | None = None,
):
    # on_stage rides on the partials, NOT in AdvisorState: a callable in the graph state would
    # break any future checkpointer (state must stay serializable). The graph is rebuilt per
    # turn (run_advisor), so a per-turn callback binds cleanly.
    g = StateGraph(AdvisorState)
    g.add_node("understand", partial(understand, llm=llm, on_stage=on_stage))
    g.add_node("execute", partial(
        execute, client=client, local_client=local_client, on_stage=on_stage
    ))
    g.add_node("respond", partial(respond, llm=llm, on_stage=on_stage))
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


async def run_advisor(
    query: str,
    history: list[dict[str, Any]] | None = None,
    gps: dict[str, Any] | None = None,
    on_stage: StageFn | None = None,
) -> dict[str, Any]:
    """Multi-turn mobility advisor. Returns widget JSON including updated messages.

    Pass the previous turn's response["messages"] back as `history` to continue the
    conversation (the dashboard front-end carries state this way). `gps` is the user's
    sanitized {lat, lng} browser position (or None): it defaults a missing origin and
    drives nearest-candidate picking. `on_stage` is called with the name of each stage as
    the turn moves through the graph (understand / geocode / routing / routing_bus /
    respond) — the bridge relays it so the chat box can show what is running during the
    30-45s a bus route currently costs. The MCP Client is reconnected per turn (clean
    lifecycle, cheap intranet handshake); config and LLM client persist for the whole
    process.
    """
    cfg, llm = await _session_deps()
    messages = list(history or [])
    messages.append({"role": "user", "content": query})
    t0 = time.perf_counter()
    async with Client(cfg) as client, Client(_local_config()) as local_client:
        graph = _build_graph(client, llm, local_client, on_stage)
        out: AdvisorState = await graph.ainvoke(
            {"messages": messages, "tool_results": [], "user_gps": gps}
        )
    logger.debug("advisor turn total %.1fs", time.perf_counter() - t0)
    return out.get("response", {"status": "error", "error": "no response produced", "messages": messages})
