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
- respond (plain Python, no LLM): composes a deterministic Italian reply from the
  results and assembles the widget JSON (full route WKT + updated messages). The reply
  is fully derivable from execute's structured output, so the LLM (a second slow gateway
  round-trip, doubling the 504 exposure) was removed — understand keeps its (L57).

The model never picks tools itself: in agentic mode Llama4 tends to emit tool calls
as pythonic text instead of structured tool_calls, which then leaks into the answer.
Letting it pick only slots and prose, with Python driving the tools, avoids that.

MCP execution helpers live in mcp_tools.py, the Llama4 client in llm.py. Runs
end-to-end only on the Snap4City JupyterHub.
"""
import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from fastmcp import Client
from langgraph.graph import END, StateGraph

from snap4city_mobility_mcp.geo import haversine_km, wkt_points
from snap4city_mobility_mcp.llm import (
    Llama4Client,
    Llama4Error,
    tool_calls,
)
from snap4city_mobility_mcp.mcp_tools import (
    PARKING_CATEGORY,
    PARKING_MAX,
    PARKING_RADII_KM,
    _build_config,
    _label_tokens,
    _local_config,
    _narrow_by_city,
    _stop_view,
    exec_tool,
    group_arc_legs,
    parse_service_features,
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
Classify request_type and mode per each field's schema description.
Rules:
- Fill EVERY field ('' for a slot the user did not give); never drop a destination \
the user named.
- Extract PLACE TEXT only (e.g. "Piazza del Duomo, Firenze"), NEVER coordinates — a \
separate tool geocodes places. Keep a city the user names attached to its place text; \
NEVER add a city — or any place — they did not say.
- No origin given ("portami al Duomo", "da qui") → origin_text '' (the system \
defaults to the user's own position). Never invent one.
- Greetings and pleasantries ("ciao", "per favore") never change the slots.
- A follow-up that omits a place ("what about by bus?", "那坐公交呢?") reuses the \
origin/destination from earlier in the conversation and changes only what the user \
changed (here mode → public_transport); an origin the user never stated stays ''.
- Today is {today} (Europe/Rome). departure_time only when the user says when to \
LEAVE ("alle 18", "domani alle 9"); '' when they give none — NEVER invent one.
<examples>
"portami al Duomo" → request_type=journey, origin_text='', destination_text="Duomo"
"dov'è la farmacia più vicina?" → request_type=journey, origin_text='', destination_text="farmacia", destination_category="Pharmacy"
"portami al Duomo e mostrami i ristoranti lungo il percorso" → request_type=journey, origin_text='', destination_text="Duomo", services_category="Restaurant"
"e in bus?" (follow-up to the piazza Duomo → piazza Dalmazia trip) → request_type=journey, origin_text="piazza Duomo, Firenze", destination_text="piazza Dalmazia, Firenze", mode=public_transport
"da Santa Croce alla stazione in bus alle 18" → request_type=journey, origin_text="Santa Croce", destination_text="stazione", mode=public_transport, departure_time="18:00"
</examples>"""

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
                "services_category": {
                    "type": "string",
                    "description": "Only when the user asks to SEE services of a generic kind ALONG the trip, not as its destination ('con le farmacie lungo il percorso', 'mostrami i ristoranti lungo la strada', follow-up 'ci sono supermercati sul percorso?'): the matching English km4city service category, e.g. Pharmacy, Restaurant, Supermarket, Fuel_station. On such a follow-up keep the previous origin/destination and fill only this. '' when the user asked to see nothing along the way.",
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
                "destination_category", "services_category", "mode", "departure_time",
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
    labels: dict[str, Any]  # resolved endpoint labels {origin, destination} for the reply's "Da .. a .." lead
    parking: list[dict[str, Any]]  # car parks near the destination (car routes), with live free-spaces
    services: dict[str, list[dict[str, Any]]]  # along-route services per mode, when the user asked for a category
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
    # Slot extraction needs only the RECENT exchange (follow-up carry-over lives in the
    # last few turns); an unbounded history makes the LLM prompt grow every turn and the
    # gateway is already minute-slow on bad days (67s measured, L54). 8 messages ≈ 4
    # turns, comfortably covering the mode/place carry-over the few-shots exercise.
    convo = [m for m in history if m.get("role") in ("user", "assistant")][-8:]
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


# Road-type words carry no identity on their own: an augmented-geocode candidate that
# shares ONLY these with the user's text ("Verde sportivo di VIA ..." for "via Verdi")
# is noise, not a match (see _signal_subset); a label that HAS one is street-shaped
# (see the civic ladder in _pick_feature).
_ROAD_WORDS = frozenset("via viale piazza piazzale corso largo vicolo strada".split())

# Italian civics are short trailing numbers; 5+ digit tokens (postal codes) never match.
_CIVIC_RE = re.compile(r"\b(\d{1,4})\b")


def _house_number(search: str) -> str | None:
    """The house number in a place text ("via Laura 11, Firenze" -> "11"), or None.

    The LAST standalone 1-4 digit token wins: Italian civics follow the street name,
    so a numbered street name ("via venti settembre 5") still yields the civic."""
    nums = _CIVIC_RE.findall(search)
    return nums[-1] if nums else None


def _feature_label(f: dict[str, Any]) -> str:
    """address + name joined — the label the pick/subset filters match against."""
    props = f.get("properties") or {}
    return " ".join(str(v) for v in (props.get("address"), props.get("name")) if v)


def _pick_feature(
    geocode: Any, search: str, gps: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Best feature for `search` from a geocode result, or None.

    The server sometimes ranks a fuzzy POI hit above the real place (once a company
    1.1 km west of "Piazza Duomo"), so the candidate pool prefers features whose
    address/name tokens are all covered by the search text (rejecting extra-token
    labels). When no label matches (e.g. stations, whose features carry no address)
    the pool is the full list. A search that carries a house number then narrows the
    pool to the civic-exact StreetNumber hits (the server ranks them first but the
    anchor override below would bury them, L52) — or, with no civic hit, to
    street-shaped labels (a name-only POI like "LAURA" must not beat "VIA LAURA" for
    "via Laura 11"). With `gps` (the anchor) the nearest pool candidate wins
    (haversine — the geocoder's own proximity bias is a no-op, probed 2026-07-09);
    without it, the pool's first (best-score) hit wins, as before.
    """
    if not isinstance(geocode, dict) or "error" in geocode:
        return None
    features = geocode.get("features")
    if not (isinstance(features, list) and features):
        return None
    # Tokenized once per feature (unicodedata normalization inside): both the matching
    # loop and the civic street filter below read from this map.
    toks_of = {id(f): _label_tokens(_feature_label(f)) for f in features}
    want = _label_tokens(search)
    pool = features
    if want:
        matching = []
        for f in features:
            toks = toks_of[id(f)]
            # Skip a label that is just the municipality ("FIRENZE"): it matches any
            # search ending in ", Firenze" but is never a useful pick.
            props = f.get("properties") or {}
            if toks and toks <= want and not toks <= _label_tokens(str(props.get("city") or "")):
                matching.append(f)
        if matching:
            pool = matching
    # Civic ladder: each level narrows only on a hit, so a number-less search (or an
    # empty level) leaves the pool — and every L17/L43/L49 behavior — exactly as is.
    civic = _house_number(search)
    if civic:
        exact = [
            f for f in pool
            if str((f.get("properties") or {}).get("civic") or "").strip() == civic
        ]
        if exact:
            pool = exact  # civic-exact StreetNumber hits outrank anchor-nearest
        else:
            # Address-shaped query with no civic hit in the pool (none returned, or a
            # compound civic like "11/A" missing the exact match): street features
            # (label carries a road-type word) beat name-only POIs (the 'LAURA' bug).
            streets = [f for f in pool if toks_of[id(f)] & _ROAD_WORDS]
            if streets:
                pool = streets
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
        "geocode %r picked feature (address=%r, civic=%r, gps=%s)",
        search, (best.get("properties") or {}).get("address"),
        (best.get("properties") or {}).get("civic"), bool(gps),
    )
    return best


def _pick_coord(
    geocode: Any, search: str, gps: dict[str, Any] | None = None
) -> list[float] | None:
    """Best feature's [lng, lat] for `search` from a geocode result (see _pick_feature)."""
    best = _pick_feature(geocode, search, gps)
    return _feature_coords(best) if best else None


def _signal_subset(geocode: Any, search: str) -> dict[str, Any] | None:
    """The geocode's features that share a non-road token with `search`, or None.

    Guards the anchor-city augmented re-query (_geocode): its result is dominated by the
    injected city, so a candidate must still share an identity token with the user's own
    text ("garibaldi", "stazione") to count — sharing only via/piazza-type words means the
    city matched but the place did not, and the caller must fall back to the plain result
    (a famous landmark in another town keeps winning there).
    """
    if not isinstance(geocode, dict) or "error" in geocode:
        return None
    features = geocode.get("features")
    if not isinstance(features, list):
        return None
    signal = _label_tokens(search) - _ROAD_WORDS
    if not signal:
        return None
    kept = []
    for f in features:
        if _label_tokens(_feature_label(f)) & signal:
            kept.append(f)
    return {"features": kept} if kept else None


def _rev_municipality(rev: Any) -> str | None:
    """The municipality of a coordinates_to_address result's best candidate, or None."""
    if isinstance(rev, dict) and "error" not in rev:
        entries = rev.get("result")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            m = entries[0].get("municipality")
            if isinstance(m, str) and m.strip():
                return m.strip()
    return None


# Nearest-category destination search: widening radius ladder (km) around the anchor
# (the user's GPS, or the geocoded destination text without one). An empty rung means
# "no such service within <radius>", so the next rung widens; all-empty falls back to
# the plain text geocode. The near results come back distance-sorted with a `distance`
# field (probed 2026-07-09), so [0] is the nearest.
NEAREST_SERVICE_RADII_KM = (0.5, 2.0, 10.0)
NEAREST_SERVICE_MAX = 10

# Along-route services (referente item 3): when the user names a category to SEE along
# the trip ("con le farmacie lungo il percorso"), execute samples anchor points on each
# found route's geometry and near-searches each one — the remote tool is point+radius
# only (no corridor parameter), so the corridor is client-made: RADIUS is its half-width,
# SPACING ≈ 2x radius keeps the sampled discs covering the line without stacking, and the
# anchor / per-route caps bound the remote fan-out (worst case MAX_ANCHORS calls per
# routed mode).
SERVICES_RADIUS_KM = 0.25
SERVICES_SPACING_KM = 0.4
SERVICES_MAX_ANCHORS = 8
SERVICES_PER_CALL = 10
SERVICES_MAX = 10


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
    destination resolves to the nearest service (see _nearest_service); a place text without
    a named city re-geocodes with the anchor's municipality appended (see _geocode, L49).
    Every call is recorded in tool_results so respond can mine the widget data. "other"
    intent or an unresolvable endpoint sets unsupported (respond asks for what's missing).
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

    async def _geocode(
        search: str,
        anchor: dict[str, Any] | None = None,
        anchor_city: Any = None,
    ) -> tuple[list[float] | None, str | None, str | None]:
        # Returns the picked ([lng, lat], city, human label) — (None, None, None) when
        # nothing resolves. The label ("Via X 12, Firenze") lets respond echo the endpoint.
        # `anchor` disambiguates same-name streets across towns (with no named city and
        # no GPS, the server's first hit once put a Florence trip's "via Pisana" in
        # Lucca): the pool candidate nearest to it wins. The destination anchors on the
        # resolved origin (endpoints of one trip are usually neighbours), the origin on
        # the user's GPS; a city the user named still dominates either (the feature pool
        # was already narrowed upstream, mcp_tools._narrow_by_city).
        # `anchor_city` (the anchor's municipality — a string, or an async provider
        # awaited only on need) covers what `anchor` alone cannot: the geocoder ranks by
        # text relevance only (every proximity parameter is a no-op, probed 2026-07-16)
        # and truncates to top-N, so the anchor city's own candidates are often absent
        # from the plain result entirely (L49). When the user named no city, re-ask with
        # the city appended (riding the server's text index and the named-city ladder);
        # the augmented pick must pass _signal_subset, else the plain result stands.
        args = {"search": search}
        result = await exec_tool(lc, "address_search_location", args)
        feats = result.get("features") if isinstance(result, dict) else None
        if anchor_city is not None and (not feats or _narrow_by_city(feats, search) is None):
            city = await anchor_city() if callable(anchor_city) else anchor_city
            if city:
                aug_args = {"search": f"{search}, {city}"}
                aug = await exec_tool(lc, "address_search_location", aug_args)
                subset = _signal_subset(aug, search)
                picked = _pick_feature(subset, aug_args["search"], gps=anchor) if subset else None
                coord = _feature_coords(picked) if picked else None
                if coord is not None:
                    # Only the deciding call enters the audit (same rule as
                    # _nearest_service): respond reads the geocode entry to say how it
                    # read the place, and two entries for one endpoint would confuse it.
                    results.append(_audit("address_search_location", aug_args, aug, extra=f" (picked {coord})"))
                    return coord, (picked.get("properties") or {}).get("city"), _endpoint_label(picked)
        picked = _pick_feature(result, search, gps=anchor)
        coord = _feature_coords(picked) if picked else None
        results.append(_audit("address_search_location", args, result, extra=f" (picked {coord})"))
        city = (picked.get("properties") or {}).get("city") if picked and coord else None
        return coord, city, (_endpoint_label(picked) if picked and coord else None)

    async def _nearest_service(anchor: list[float], category: str) -> tuple[list[float] | None, str | None]:
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
                    return [nearest["lng"], nearest["lat"]], nearest.get("name")
                return None, None
        if entry is not None:
            results.append(entry)
        logger.debug("nearest %s: no service within %s km", category, NEAREST_SERVICE_RADII_KM[-1])
        return None, None

    # Lazily-resolved GPS reverse geocode. The origin-default path needs it anyway
    # (respond's "dalla tua posizione (vicino a ...)" label) and its municipality feeds
    # the anchor-city augmentation; the origin-text path pays the call only when its
    # augmentation actually fires — and never audits it, because a coordinates_to_address
    # entry makes respond claim the trip starts from the user's GPS position.
    rev_result: Any = None

    async def _gps_reverse() -> Any:
        nonlocal rev_result
        if rev_result is None:
            rev_result = await reverse_geocode(client, user_gps["lat"], user_gps["lng"])
        return rev_result

    async def _gps_city() -> str | None:
        return _rev_municipality(await _gps_reverse()) if user_gps else None

    # --- origin: user text, else the GPS position itself (labelled via reverse geocode).
    if origin_text:
        origin, origin_city, origin_label = await _geocode(origin_text, user_gps, anchor_city=_gps_city)
    else:
        origin = [user_gps["lng"], user_gps["lat"]]
        rev_args = {"latitude": user_gps["lat"], "longitude": user_gps["lng"]}
        rev = await _gps_reverse()
        if isinstance(rev, dict) and "error" not in rev:
            # Only a successful lookup enters the audit: respond keys "dalla tua
            # posizione (vicino a ...)" off this entry, and a failure entry would
            # trigger its error rule for a trip that is actually fine.
            results.append(_audit("coordinates_to_address", rev_args, rev))
        origin_city = _rev_municipality(rev)
        # The origin label the reply echoes: "la tua posizione", enriched with the reverse-
        # geocoded street when available.
        addr = _rev_address(rev)
        origin_label = f"la tua posizione (vicino a {addr.title()})" if addr else "la tua posizione"

    # --- destination: nearest-category service when asked, else plain text geocode.
    # Its geocodes anchor on the resolved origin (see _geocode), falling back to GPS;
    # the anchor city is the origin's municipality (picked feature's city, or the GPS
    # reverse geocode's) so a no-city destination resolves in the same town as the start.
    dest_anchor = {"lat": origin[1], "lng": origin[0]} if origin is not None else user_gps
    dest_anchor_city = origin_city
    dest = None
    dest_label = None
    if dest_category:
        geocoded = None
        geocoded_label = None
        if user_gps:
            anchor = [user_gps["lng"], user_gps["lat"]]
        else:
            # No GPS: anchor on the geocoded destination text ("farmacia, Pisa" lands in
            # Pisa via the named-city ladder), then snap to the nearest real service.
            geocoded, _, geocoded_label = await _geocode(dest_text, dest_anchor, anchor_city=dest_anchor_city)
            anchor = geocoded
        if anchor is not None:
            dest, dest_label = await _nearest_service(anchor, dest_category)  # label = found place name
        if dest is None:
            # Category miss (bad category name / nothing within the widest rung):
            # degrade to the text geocode so the trip still resolves when possible.
            # Without GPS the text was already geocoded above (never re-call it).
            if not user_gps:
                dest, dest_label = geocoded, geocoded_label
            elif dest_text:
                dest, _, dest_label = await _geocode(dest_text, dest_anchor, anchor_city=dest_anchor_city)
    else:
        dest, _, dest_label = await _geocode(dest_text, dest_anchor, anchor_city=dest_anchor_city)

    if origin is None or dest is None:
        return {"tool_results": results, "unsupported": False}  # geocode error: respond explains

    mode_specified = bool(slots.get("mode"))
    # No mode given: route ALL THREE modes concurrently, so a plain "from A to B" answers with a
    # walking, a driving and a public-transport option (one line each on the map) and the reply
    # compares them. The wall clock is the slowest coroutine — the bus one: every vehicle=bus
    # request rebuilds the PT graph server-side (~30-45s, probed 2026-07-13, vs sub-second for
    # foot/car). That cost is accepted for the comparison (the chat box shows the stage +
    # elapsed while it runs). An explicit mode runs that one only — asking for "a piedi" must
    # not pay the bus latency.
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
            "vehicle": _VEHICLE.get(mode, mode),
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
        # _extract_data renders and narrates every mode uniformly.
        return _audit("routing", {"routetype": mode, **args}, result)

    async def _parking() -> dict[str, Any]:
        # Find car parks near the destination (called only when a car route is in play — the
        # feature is car-specific). Runs concurrently with routing (one flat gather below) so
        # it adds no wall-clock when routing is the long pole. Widening radius ladder like
        # _nearest_service: an empty rung re-searches wider (a suburban destination often has
        # no car park within the first rung — silently pinless before the ladder, L56), and
        # only the deciding call becomes the entry: the winning rung, or the last empty one.
        # The result carries only the spots' coordinates (no live free-spaces, and none is
        # fetched — parking is pins-only). Returns the entry.
        entry: dict[str, Any] = {}
        for radius in PARKING_RADII_KM:
            p_args = {
                "latitude": dest[1],
                "longitude": dest[0],
                "categories": PARKING_CATEGORY,
                "maxdistance": radius,
                "maxresults": PARKING_MAX * 3,
            }
            result = await exec_tool(client, "service_search_near_gps_position", p_args)
            entry = _audit("service_search_near_gps_position", p_args, result)
            if parse_service_features(result):
                break
        return entry

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
    # here). The entry is NOT audited — parking is pins-only (data.parking → drawParkingPins),
    # exactly like along-route services: the map pins ARE the answer, so the LLM view carries
    # nothing about it (the old "N car parks, availability unknown" sentence was noise — the
    # front-end never reads free_spaces, so live enrichment is skipped too).
    car_ok = any(
        m == "car"
        and isinstance(entry["result"], dict)
        and isinstance(entry["result"].get("journey"), dict)
        for m, entry in zip(modes, primary)
    )
    parking: list[dict[str, Any]] = []
    if parking_entry is not None and car_ok:
        # Nearest-N list (parse + Haversine distance + sort), nearest first. free_spaces stays
        # None: the pins are widget-rendered from each serviceUri, which resolves its own live
        # availability — our side needs only {name, lat, lng, uri, distance_km}.
        parking = _extract_parking(parking_entry, {"lat": dest[1], "lng": dest[0]}) or []

    # Along-route services (referente item 3): only when the user named a category to see.
    # A second, sequential gather — the anchors are sampled FROM the routed geometry, so
    # this cannot join the routing gather above (and FakeClient's FIFO stays deterministic:
    # all routing/parking responses first, then the anchor searches in modes order).
    services_category = (slots.get("services_category") or "").strip()
    services: dict[str, list[dict[str, Any]]] = {}
    if services_category:
        _emit(on_stage, "services")
        services = await _along_route_services(client, services_category, modes, primary)

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
        # How each endpoint was resolved, for the reply's "Da ORIGIN a DESTINATION:" lead
        # (a geocoded address, "la tua posizione", or the found category place; None when
        # the feature carried no address). Own-frontend/reply field, not the widget contract.
        "labels": {"origin": origin_label, "destination": dest_label},
        "parking": parking,
        "services": services,
        # HH:MM only (never a date or seconds, L43): this is the one form the reply may print.
        # Absent when the user asked for no particular time — respond then says nothing about
        # departure rather than announcing "now".
        "departure": departure.strftime("%H:%M") if departure else "",
    }


def _sample_polyline(
    pts: list[tuple[float, float]], spacing_km: float
) -> list[tuple[float, float]]:
    """Points every ~spacing_km of arc length along a (lng, lat) polyline, endpoints
    included (existing vertices only — no interpolation, a vertex is never farther than
    one hop past the spacing mark and real route geometry is dense)."""
    out = [pts[0]]
    acc = 0.0
    for a, b in zip(pts, pts[1:]):
        acc += haversine_km(a[1], a[0], b[1], b[0])
        if acc >= spacing_km:
            out.append(b)
            acc = 0.0
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def _service_anchors(routetype: str, first: dict[str, Any]) -> list[tuple[float, float]]:
    """Anchor points on one found route for the along-route service search.

    foot/car: the full geometry sampled every SERVICES_SPACING_KM. public_transport:
    per referente item 3, services are shown near the board/alight stops and along the
    walking legs — so each bus leg contributes only its boundary vertices while foot
    legs are sampled like a foot route (a ride's intermediate stops are not places the
    user can stop at). Deduped, then evenly thinned to SERVICES_MAX_ANCHORS (first/last
    kept) to bound the remote fan-out.
    """
    anchors: list[tuple[float, float]] = []
    legs = first.get("legs") if isinstance(first.get("legs"), list) else None
    if routetype == "public_transport" and legs:
        for leg in legs:
            pts = wkt_points(leg.get("wkt") or "") if isinstance(leg, dict) else None
            if not pts:
                continue
            if leg.get("type") == "bus":
                anchors += [pts[0], pts[-1]]
            else:
                anchors += _sample_polyline(pts, SERVICES_SPACING_KM)
    else:
        pts = wkt_points(first.get("wkt") or "")
        if pts:
            anchors = _sample_polyline(pts, SERVICES_SPACING_KM)
    seen: set[tuple[float, float]] = set()
    uniq = [p for p in anchors if not (p in seen or seen.add(p))]
    if len(uniq) > SERVICES_MAX_ANCHORS:
        n = len(uniq)
        keep = {round(i * (n - 1) / (SERVICES_MAX_ANCHORS - 1)) for i in range(SERVICES_MAX_ANCHORS)}
        uniq = [p for i, p in enumerate(uniq) if i in keep]
    return uniq


async def _along_route_services(
    client: Client, category: str, modes: list[str], entries: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Services of `category` along each successfully routed mode: {mode: [spots]}.

    One near-search per anchor (anchors per _service_anchors), concurrent within a mode.
    The calls are NOT audited — internal like the parking search, and an audited non-Car_park
    near-search entry would trip respond's "how the destination was resolved" rule. The
    LLM view carries nothing about them either: the map pins ARE the answer (the old
    one-sentence summary was dropped as noise, 2026-07-16). Spots are
    deduped across anchors by uri (keeping the smallest anchor distance), sorted by that
    distance and capped to SERVICES_MAX per mode. Item shape mirrors data.parking minus
    the occupancy fields: {name, lat, lng, uri, distance_km}. Modes whose routing failed
    (or found nothing nearby) simply contribute no key.
    """
    per_mode: dict[str, list[dict[str, Any]]] = {}
    for mode, entry in zip(modes, entries):
        result = entry.get("result")
        if not (isinstance(result, dict) and isinstance(result.get("journey"), dict)):
            continue
        first = (result["journey"].get("routes") or [{}])[0]
        anchors = _service_anchors(mode, first if isinstance(first, dict) else {})
        if not anchors:
            continue

        async def one(pt: tuple[float, float]) -> tuple[tuple[float, float], list[dict[str, Any]]]:
            res = await exec_tool(client, "service_search_near_gps_position", {
                "latitude": pt[1],
                "longitude": pt[0],
                "categories": category,
                "maxdistance": SERVICES_RADIUS_KM,
                "maxresults": SERVICES_PER_CALL,
            })
            return pt, parse_service_features(res)

        found = await asyncio.gather(*(one(p) for p in anchors))
        by_uri: dict[str, dict[str, Any]] = {}
        for pt, spots in found:
            for s in spots:
                if not s.get("uri") or s.get("lat") is None or s.get("lng") is None:
                    continue
                d = round(haversine_km(pt[1], pt[0], s["lat"], s["lng"]), 3)
                prev = by_uri.get(s["uri"])
                if prev is None or d < prev["distance_km"]:
                    by_uri[s["uri"]] = {
                        "name": s.get("name"), "lat": s["lat"], "lng": s["lng"],
                        "uri": s["uri"], "distance_km": d,
                    }
        spots = sorted(by_uri.values(), key=lambda s: s["distance_km"])[:SERVICES_MAX]
        if spots:
            per_mode[mode] = spots
            logger.debug("along-route %s (%s): %d services", category, mode, len(spots))
    return per_mode


def _routetype_of(entry: dict[str, Any]) -> str | None:
    """The routetype of a routing audit entry, read back from its json args.
    None when args is absent or malformed (test entries may carry no args)."""
    try:
        return json.loads(entry.get("args") or "{}").get("routetype")
    except (json.JSONDecodeError, TypeError):
        return None


# Slot mode -> vehicle name, for the router request AND the dashboard vehicle family (the
# front-end vehicleOf mirrors it). The slot keeps the user-facing public_transport name; both
# consumers say bus.
_VEHICLE = {"foot": "foot", "car": "car", "public_transport": "bus"}


def _route_minutes(route: dict[str, Any]) -> float:
    """A route's travel time in minutes, for ordering routes fastest-first. `duration`
    is always the route tool's _fmt_hms "H:MM:SS" string (the only producer); an
    unparseable or missing one sorts last (inf)."""
    dur = route.get("duration")
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
    # A direct arc scan: every leg's transport comes verbatim from its arcs' transport
    # (group_arc_legs), so building the full leg list (stops copies included) just to
    # ask "any non-foot?" was wasted work.
    return not any(
        (arc.get("transport") or "foot") != "foot"
        for arc in first.get("arc") or []
        if isinstance(arc, dict)
    )


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


def _parking_sort_key(s: dict[str, Any]) -> float:
    """Sort key for car parks: nearest first. free_spaces is never fetched (pins-only, the
    widget resolves each spot's live availability itself), so distance is the only order."""
    dist = s.get("distance_km")
    return dist if dist is not None else float("inf")


def _extract_parking(
    entry: dict[str, Any], dest: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """Mine the parking search entry into the widget payload.

    entry is the Car_park search entry (passed directly by execute — NOT audited, parking
    is pins-only). dest is the resolved destination {"lat","lng"} (from the endpoints). Each
    spot gets a Haversine distance from dest (the search envelope is not relied on for
    distance — units are unverified, L-style probe discipline). Sorted nearest first and
    capped to PARKING_MAX; free_spaces stays None (the widget resolves each pin's live
    availability itself). None when the entry is empty/errored (route still returned without
    it)."""
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


# km4city English service category -> Italian (singular, plural-with-article), for the
# deterministic reply: the category-destination name ("la farmacia più vicina") and the
# along-route services acknowledgement ("ho segnato le farmacie ..."). Covers the classes
# the understand schema emits; an unknown one degrades to a spaced lowercase fallback.
_CATEGORY_IT = {
    "Pharmacy": ("farmacia", "le farmacie"),
    "Hospital": ("ospedale", "gli ospedali"),
    "Supermarket": ("supermercato", "i supermercati"),
    "Museum": ("museo", "i musei"),
    "Hotel": ("hotel", "gli hotel"),
    "Restaurant": ("ristorante", "i ristoranti"),
    "Car_park": ("parcheggio", "i parcheggi"),
    "Fuel_station": ("distributore", "i distributori"),
}

# User-facing Italian label per travel mode (keyed by the slot's routetype, not the vehicle
# family): the deterministic reply reads it directly.
_MODE_LABEL = {"foot": "a piedi", "car": "in auto", "public_transport": "con i mezzi"}


def _category_it(category: str, *, plural: bool = False) -> str:
    """Italian label for a km4city category — the plural carries its article ('le farmacie')."""
    pair = _CATEGORY_IT.get(category)
    if pair:
        return pair[1] if plural else pair[0]
    return category.replace("_", " ").lower()


def _endpoint_label(feature: dict[str, Any] | None) -> str | None:
    """A human 'Via Ciro Menotti 19, Firenze' from a picked geocode feature (address +
    civic + city, title-cased), or None when the feature carries no address. Lets the reply
    echo how each endpoint was resolved so the user can spot a wrong geocode."""
    props = (feature or {}).get("properties") or {}
    addr = props.get("address")
    if not (isinstance(addr, str) and addr.strip()):
        return None
    label = addr.strip().title()
    civic = props.get("civic")
    if civic is not None and str(civic).strip():
        label += f" {str(civic).strip()}"
    city = props.get("city")
    if isinstance(city, str) and city.strip():
        label += f", {city.strip().title()}"
    return label


def _rev_address(rev: Any) -> str | None:
    """The best candidate's street address from a coordinates_to_address RESULT, or None
    (mirrors _rev_municipality — takes the raw reverse-geocode result, not an audit entry)."""
    if isinstance(rev, dict) and "error" not in rev:
        entries = rev.get("result")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            addr = entries[0].get("address")
            if isinstance(addr, str) and addr.strip():
                return addr.strip()
    return None


def _human_km(km: Any) -> str | None:
    """A distance as natural Italian '2.6 km' (1 decimal, trailing zero trimmed), or None."""
    if not isinstance(km, (int, float)):
        return None
    return f"{km:.1f}".rstrip("0").rstrip(".") + " km"


def _human_duration(duration: Any) -> str | None:
    """A duration as natural Italian 'circa 5 minuti' (rounded to the minute), or None. The
    exact H:MM:SS stays in each route's detail block; the chat lead reads like a person."""
    mins = _route_minutes({"duration": duration})
    if mins == float("inf"):
        return None
    m = max(1, round(mins))
    return "circa un minuto" if m == 1 else f"circa {m} minuti"


def _mode_phrase(route: dict[str, Any]) -> str:
    """One mode as a natural fragment: 'in auto 2.6 km, circa 5 minuti' (figures omitted
    when absent, so a route with no ETA still reads 'in auto 2.6 km')."""
    label = _MODE_LABEL.get(route.get("mode") or "", "il percorso")
    tail = ", ".join(
        p for p in (_human_km(route.get("distance_km")), _human_duration(route.get("duration"))) if p
    )
    return f"{label} {tail}" if tail else label


def _route_block(routes: list[dict[str, Any]]) -> str:
    """The route comparison as a tidy multi-line block: one 'In auto: 2.6 km, circa 5 minuti'
    line per mode (newline-separated — the chat bubble renders \\n via white-space: pre-wrap),
    plus a fastest-option line when more than one mode is present. '' when nothing is drawable."""
    lines = []
    for r in routes:
        tail = ", ".join(
            p for p in (_human_km(r.get("distance_km")), _human_duration(r.get("duration"))) if p
        )
        if tail:
            label = _MODE_LABEL.get(r.get("mode") or "", "percorso").capitalize()
            lines.append(f"{label}: {tail}")
    if not lines:
        return ""
    block = "\n".join(lines)
    if len(routes) > 1:
        fastest = _MODE_LABEL.get(routes[0].get("mode") or "", "il percorso")
        block += f"\nL'opzione più veloce è {fastest}."
    return block


# A message that is ONLY a greeting/pleasantry ("ciao", "buongiorno", "grazie"): the reply
# welcomes the user instead of the dry "unsupported" pitch (the understand stage classes a
# bare greeting as 'other'). Words are matched after stripping punctuation and casing.
_GREETINGS = frozenset(
    "ciao salve buongiorno buonasera buonanotte hey ehi hola hello hi".split()
)
_PLEASANTRIES = frozenset("grazie prego per favore piacere".split())


def _is_greeting(text: str) -> bool:
    """True when the user's message is nothing but a greeting/pleasantry."""
    words = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    return bool(words) and all(w in _GREETINGS or w in _PLEASANTRIES for w in words)


def _compose_reply(
    data: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    slots: dict[str, Any],
    unsupported: bool,
    missing: list[str] | None,
    departure: str,
    services_found: bool,
    first_turn: bool,
    user_text: str,
    labels: dict[str, Any],
) -> str:
    """The deterministic Italian reply (no LLM, L57).

    Everything it states is derived from execute's structured output: the routes
    (fastest-first, from _extract_data), the requested departure, and the resolved endpoint
    `labels` (origin/destination — a geocoded address, "la tua posizione", or the found
    category place). It reimplements the cases the old respond prompt covered — a bare
    greeting, missing endpoints, unsupported intent, a route error, a single/multi-mode
    comparison, a public-transport degrade, and the along-route-services acknowledgement —
    with no gateway round-trip and no fabrication risk. The answer leads with a
    "Da ORIGIN a DESTINATION:" line (so the user can spot a wrong geocode) then one line per
    mode. A short "Ciao!" leads only on the FIRST turn (like the old prompt); follow-ups
    answer directly. Italian only; the reply lives in messages[-1].content.
    """
    if _is_greeting(user_text):
        # Pure greeting: welcome + one-line onboarding, never the dry unsupported pitch.
        return (
            "Ciao! Sono l'assistente per la mobilità: dimmi da dove a dove vuoi andare "
            "(a piedi, in auto o con i mezzi) e ti trovo il percorso."
        )
    hi = "Ciao! " if first_turn else ""
    if missing:
        labels = {
            "origin": "il punto di partenza (o la tua posizione)",
            "destination": "la destinazione",
        }
        asked = " e ".join(labels[m] for m in missing)
        return hi + f"Mi serve ancora {asked} per rispondere."
    if unsupported:
        return hi + (
            "Al momento rispondo a domande su percorsi punto-punto (a piedi, in auto "
            "o con i mezzi pubblici), anche verso il luogo più vicino di un certo "
            "tipo, es. 'da Piazza Duomo a Santa Croce a piedi' o 'portami alla "
            "farmacia più vicina'."
        )
    routes = data.get("routes") or []
    if not routes:
        # No drawable route (geocode miss / router failure): say so in Italian and suggest
        # an alternative — never echo the raw English error string.
        label = _MODE_LABEL.get((slots.get("mode") or "").strip())
        suffix = f" {label}" if label else ""
        return hi + (
            f"Non sono riuscito a calcolare il percorso{suffix}. "
            "Prova un altro mezzo o un indirizzo più preciso."
        )

    # Lead: "Da ORIGIN a DESTINATION[, partenza alle HH:MM]:" from execute's resolved
    # endpoint labels — the user sees how each end was read and can spot a wrong geocode.
    origin_label = (labels or {}).get("origin")
    dest_label = (labels or {}).get("destination")
    lead = None
    if origin_label and dest_label:
        # "da" contracts with the article of "la tua posizione" -> "dalla"; a street label is
        # title-cased ("Via ...") with no article, so "da Via ...". Street labels never start
        # with a lowercase "la ", so this cleanly tells the GPS-origin phrasing apart.
        if origin_label.startswith("la "):
            lead = f"Dalla {origin_label[3:]} a {dest_label}"
        else:
            lead = f"Da {origin_label} a {dest_label}"
        if departure:
            lead += f", partenza alle {departure}"
        lead += ":"
    elif departure:
        lead = f"Partenza alle {departure}:"

    # A public-transport request that degraded to a walking-only journey (L39): _extract_data
    # relabelled the single route 'foot', but the audit still flags the degrade — say there is
    # no direct transit and give the walk. In a multi-mode run the real modes carry the answer
    # and the degraded PT was dropped, so this only fires on an explicit bus request.
    degraded = any(
        _routing_hint(_routetype_of(e), e.get("result")) == "pt_degraded_to_foot"
        for e in results if e.get("name") == "routing"
    )
    if (slots.get("mode") or "").strip() == "public_transport" and degraded:
        base = "Non c'è un collegamento diretto con i mezzi pubblici"
        has_figures = _human_km(routes[0].get("distance_km")) or _human_duration(routes[0].get("duration"))
        body = f"{base}, ma {_mode_phrase(routes[0])}." if has_figures else base + "."
    else:
        body = _route_block(routes)

    # Along-route services the user asked to SEE: the pins are the answer; the reply only
    # confirms (or reports none found), on its own line. The plural label carries its article.
    services_category = (slots.get("services_category") or "").strip()
    svc_line = None
    if services_category:
        plural = _category_it(services_category, plural=True)
        svc_line = (
            f"Ho segnato {plural} lungo il percorso sulla mappa."
            if services_found
            else f"Non ho trovato {plural} lungo il percorso."
        )
    return hi + "\n".join(b for b in (lead, body, svc_line) if b)


def _routing_entry_for(
    results: list[dict[str, Any]], mode: str | None
) -> dict[str, Any] | None:
    """The routing audit entry for `mode` (its routetype), for the detail block to mine.

    Prefers an exact routetype match; falls back to the sole routing entry when none
    matches (an explicit-bus request that degraded to a foot-only journey keeps its
    audit entry under routetype 'public_transport' while data.routes relabels it 'foot').
    None when the match is ambiguous (more than one routing entry, none matching)."""
    routing = [e for e in results if e.get("name") == "routing"]
    for e in routing:
        if _routetype_of(e) == mode:
            return e
    return routing[0] if len(routing) == 1 else None


def _bus_detail_lines(arc: list[Any]) -> list[str]:
    """Leg-by-leg lines for a public-transport journey: walk legs + each ride with its
    FULL stop list (name + HH:MM = fermate + timeline). Built from the audit arc via
    group_arc_legs (which keeps the whole stop list on a ride leg — only slim's
    _leg_boarding collapses it to board/alight). Stop times are ISO in the arc; _stop_view
    formats each to HH:MM (L43)."""
    lines: list[str] = []
    for leg in group_arc_legs(arc):
        if leg.get("transport") == "bus":
            head = " ".join(
                t for t in (
                    f"Linea {leg['line']}" if leg.get("line") else None,
                    f"({leg['provider']})" if leg.get("provider") else None,
                ) if t
            ) or "In autobus"
            if leg.get("headsign"):
                head += f" -> {leg['headsign']}"
            lines.append(head)
            for stop in leg.get("stops") or []:
                sv = _stop_view(stop)
                if not sv or not sv.get("name"):
                    continue
                lines.append(f"  {sv['name']}  {sv['time']}" if sv.get("time") else f"  {sv['name']}")
        else:  # walking leg between/around rides
            dist = leg.get("distance_km")
            if dist is not None:
                lines.append(f"A piedi {dist} km")
            elif leg.get("from"):
                lines.append(str(leg["from"]))
    return lines


def _street_detail_lines(arc: list[Any]) -> list[str]:
    """Turn-by-turn street lines for a foot/car journey, one per named stretch (consecutive
    same-street arcs merge, their distances summed; unnamed 'nd' arcs drop). Each line is
    '<street> (<km> km)', or just the street when the arc carries no distance."""
    steps: list[list[Any]] = []  # [street, dist_km_or_None]
    for a in arc:
        if not isinstance(a, dict):
            continue
        desc = a.get("desc")
        if not desc or desc == "nd":
            continue
        dist = a.get("distance") if isinstance(a.get("distance"), (int, float)) else None
        if steps and steps[-1][0] == desc:
            prev = steps[-1][1]
            steps[-1][1] = (prev or 0) + (dist or 0) if (prev is not None or dist is not None) else None
        else:
            steps.append([desc, dist])
    return [f"{name} ({round(d, 3)} km)" if d is not None else str(name) for name, d in steps]


def _format_detail(
    results: list[dict[str, Any]], route: dict[str, Any], departure: str = ""
) -> str:
    """Deterministic, exact step-by-step block for ONE route (Italian).

    Built in Python from the audit — never through the LLM — so the timetable stays exact:
    Llama4 garbles long stop lists (the very reason slim's _leg_boarding collapses them to
    board/alight, L12). respond attaches it to every drawable route as routes[i].detail
    (the dashboard picker shows it on selection, L50) and appends it beneath its concise
    reply when the user asked for one mode. `route` is a data.routes entry (its real mode +
    totals); `results` is the tool audit (full arc/stops). Defensive: a route whose audit
    arc is missing yields just the totals line, never raises. '' when there is nothing to
    add."""
    mode = route.get("mode")
    entry = _routing_entry_for(results, mode)
    arc: list[Any] = []
    if entry is not None:
        result = entry.get("result")
        if isinstance(result, dict) and isinstance(result.get("journey"), dict):
            first = (result["journey"].get("routes") or [{}])[0]
            arc = first.get("arc") or []
    lines: list[str] = []
    if departure:
        lines.append(f"Partenza: {departure}")
    lines.extend(_bus_detail_lines(arc) if mode == "public_transport" else _street_detail_lines(arc))
    bits = []
    if route.get("distance_km") is not None:
        bits.append(f"{route['distance_km']} km")
    if route.get("duration"):
        bits.append(str(route["duration"]))
    if bits:
        lines.append(f"Totale: {' · '.join(bits)}")
    return "\n".join(line for line in lines if line).strip()


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


async def respond(
    state: AdvisorState, *, on_stage: StageFn | None = None
) -> dict[str, Any]:
    """Compose the deterministic Italian reply from the results and assemble the widget
    JSON (no LLM, L57 — the reply is fully derivable from execute's structured output)."""
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
    # Along-route services (referente item 3), keyed per mode in execute: each drawable
    # route gets ITS OWN list as routes[i].services, so the front-end pin set follows the
    # chips picker locally (car shows the car route's services, bus the stop-vicinity
    # ones). Own-frontend field like origin/destination/parking/detail (rule 8 precedent).
    # A PT journey degraded to foot (mode relabelled by _extract_data) misses its key and
    # simply ships none — its anchors were bus-stop-based and no longer match the walk.
    services_map = state.get("services") or {}
    if intent == "route" and services_map:
        for r in data.get("routes") or []:
            svc = services_map.get(r.get("mode") or "")
            if svc:
                r["services"] = svc
    # Every drawable route ships its deterministic step-by-step block (fermate + orari for
    # bus, turn-by-turn streets for foot/car) as routes[i].detail. The front-end owns ALL of
    # its rendering (renderDetail): a single-route turn shows it under the reply, a multi-mode
    # turn shows the tapped route's on selection — WITHOUT starting a new turn (a bus re-route
    # costs 30-45s, L31), and WITHOUT it ever entering the reply text (so it never rides
    # messages[-1].content into the next understand prompt). Built in Python from the audit,
    # NOT the LLM (exact timetable; the LLM view carries no legs/streets). Small strings only —
    # no raw arcs (the rule-8 note on data.arcs stands). Own front-end field like data.origin/
    # destination/parking.
    if intent == "route":
        for r in data.get("routes") or []:
            block = _format_detail(results, r, state.get("departure") or "")
            if block:
                r["detail"] = block
    # A route intent execute refused means an endpoint was unresolvable (no text and
    # nothing covering it). respond then asks for it instead of claiming the request
    # is unsupported; a GPS-covered origin is never asked for.
    missing = (
        _missing_route_slots(state.get("slots") or {}, state.get("user_gps"))
        if unsupported and intent == "route"
        else None
    ) or None

    # A greeting leads only on the FIRST turn (no prior assistant message, like the old
    # prompt); the latest user message also drives the bare-greeting welcome. messages here is
    # [history..., current user] — the assistant turn is appended after.
    first_turn = not any(m.get("role") == "assistant" for m in messages)
    user_text = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"), ""
    )
    # Along-route services (services_category slot) are pins-only (data.routes[].services, L53):
    # the reply just acknowledges they are on the map (or reports none found). services_map is
    # non-empty only when the user asked AND some were found.
    answer = _compose_reply(
        data,
        results,
        slots=state.get("slots") or {},
        unsupported=unsupported,
        missing=missing,
        departure=state.get("departure") or "",
        services_found=bool(services_map),
        first_turn=first_turn,
        user_text=user_text or "",
        labels=state.get("labels") or {},
    )
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
    g.add_node("respond", partial(respond, on_stage=on_stage))
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
