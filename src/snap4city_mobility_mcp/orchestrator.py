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
import json
import logging
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
    _build_config,
    _label_tokens,
    exec_tool,
    group_arc_legs,
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
on) using ONLY the leg fields — never invent lines, stops, or times.
- If a RESULTS item could not be computed (an `error`, or a route/place not found), \
say so plainly WITHOUT any numbers and suggest a sensible alternative (another mode, a \
more precise address); when geocoded addresses are present, mention how you read the \
origin/destination so the user can spot a wrong match.
- If a routing RESULTS item carries a `hint`, follow it for the alternative you \
suggest — it already decided the right one: `car_pt_blocked_try_foot` = that mode is \
likely blocked by Florence's ZTL/pedestrian core, so suggest going on foot or by \
public transport; `service_empty_try_foot_or_later` = a service-side problem for that \
mode (NOT a ZTL), so suggest walking (foot routes work) or trying again later. With NO \
`hint`, never claim a ZTL/pedestrian zone yourself.
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
    final: dict[str, Any]  # widget JSON assembled by respond


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
        resp = await llm.achat(
            messages=[{"role": "system", "content": UNDERSTAND_SYSTEM}, *convo],
            tools=[_EXTRACT_SLOTS_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "extract_slots"}},
            temperature=0,
        )
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


async def execute(state: AdvisorState, *, client: Client) -> dict[str, Any]:
    """Run the tool flow for the extracted intent (no LLM).

    route: geocode both endpoints, then routing with the requested mode (plus a foot
    profile retry). tpl_* intents go to tpl.run_tpl_flow. Every call is recorded in
    tool_results so respond can mine the widget data. "other" or missing slots set
    unsupported.
    """
    slots = state.get("slots") or {}
    results: list[dict[str, Any]] = []

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
        result = await exec_tool(client, "address_search_location", args)
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

    mode = slots.get("mode") or "foot_shortest"

    async def _route(routetype: str, *, attempts: int | None = None) -> dict[str, Any]:
        # GeoJSON coordinate order is [longitude, latitude].
        args = {
            "startlatitude": origin[1],
            "startlongitude": origin[0],
            "endlatitude": dest[1],
            "endlongitude": dest[0],
            "routetype": routetype,
        }
        result = await exec_tool(client, "routing", args, routing_attempts=attempts)
        results.append({"name": "routing", "args": json.dumps(args), "result": result})
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "tool routing %s -> %s",
                args,
                json.dumps(slim_result_for_llm("routing", result), ensure_ascii=False)[:500],
            )
        return result

    routed = await _route(mode)
    if (
        mode == "public_transport"
        and isinstance(routed, dict)
        and isinstance(routed.get("journey"), dict)
        and logger.isEnabledFor(logging.DEBUG)
    ):
        # The real public-transport arc shape hasn't been observed live yet: dump
        # the first raw arcs so group_arc_legs' grouping key can be calibrated
        # offline from debug.log (gitignored).
        first = (routed["journey"].get("routes") or [{}])[0]
        arcs = first.get("arc") or []
        logger.debug(
            "PT raw arcs (first 5 of %d): %s",
            len(arcs),
            json.dumps(arcs[:5], ensure_ascii=False)[:2000],
        )
    if isinstance(routed, dict) and "error" in routed and mode in _FOOT_FALLBACK:
        # Single-shot fallback to the other foot profile. The requested profile
        # already exhausted the stale-retry ladder (~27 s), so this only probes the
        # other graph path, not the transient.
        await _route(_FOOT_FALLBACK[mode], attempts=1)

    return {"tool_results": results, "unsupported": False}


def _routetype_of(entry: dict[str, Any]) -> str | None:
    """The routetype of a routing audit entry, read back from its json args.
    None when args is absent or malformed (test entries may carry no args)."""
    try:
        return json.loads(entry.get("args") or "{}").get("routetype")
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_data(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Mine the tool-result audit for the widget payload (last successful tool wins)."""
    route_error: str | None = None
    for entry in reversed(results):
        name = entry.get("name")
        result = entry.get("result")
        if not isinstance(result, dict) and not isinstance(result, list):
            continue
        is_err = isinstance(result, dict) and "error" in result

        if name == "routing":
            if isinstance(result, dict) and isinstance(result.get("journey"), dict):
                journey = result["journey"]
                first = (journey.get("routes") or [{}])[0]
                data = {
                    "wkt": first.get("wkt"),  # full LINESTRING, not truncated
                    "distance_km": first.get("distance"),
                    "eta": first.get("eta"),
                    "duration": first.get("time"),
                    # "arcs": first.get("arc"),  # per-segment detail: bloats the payload
                    # ~90%, re-enable once referente confirms the widget needs it.
                    "source_node": journey.get("source_node"),
                    "destination_node": journey.get("destination_node"),
                }
                if _routetype_of(entry) == "public_transport":
                    # Walk/ride legs grouped from the journey arcs. Pending referente
                    # confirmation, same status as data.arcs above.
                    legs = group_arc_legs(first.get("arc") or [])
                    if legs:
                        data["legs"] = legs
                return data
            # Scanning backwards, keep overwriting: when every profile failed (mode +
            # foot fallback), we want the earliest error, the mode the user actually
            # asked for, not the fallback's.
            if is_err:
                route_error = result["error"]
    return {"route_error": route_error} if route_error else {}


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
    data = extract_tpl_data(intent, results) if intent in TPL_INTENTS else _extract_data(results)
    # The dashboard widget needs the travel mode to render the route (foot/car/bus icon
    # + leg colors), so surface it as widget data. Guarded to a successful route (wkt
    # present): never pollute a tpl payload, a route error ({route_error}), or an
    # unsupported/missing reply ({}) with a mode field. Mirror execute's default for the
    # mode the route was actually computed with. Pending referente confirmation of the
    # widget data shape, same status as the commented-out data.arcs above.
    if intent == "route" and isinstance(data, dict) and data.get("wkt"):
        data["mode"] = (state.get("slots") or {}).get("mode") or "foot_shortest"
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
        "final": {
            "status": "success",
            "request_type": intent,
            "data": data,
            "messages": messages,  # updated history (last turn = the reply)
        }
    }


def _build_graph(client: Client, llm: Llama4Client):
    g = StateGraph(AdvisorState)
    g.add_node("understand", partial(understand, llm=llm))
    g.add_node("execute", partial(execute, client=client))
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

    Pass the previous turn's final["messages"] back as `history` to continue the
    conversation (the CLI REPL and the dashboard both carry state this way). The MCP
    Client is reconnected per turn (clean lifecycle, cheap intranet handshake); config
    and LLM client persist for the whole process.
    """
    cfg, llm = await _session_deps()
    messages = list(history or [])
    messages.append({"role": "user", "content": query})
    async with Client(cfg) as client:
        graph = _build_graph(client, llm)
        out: AdvisorState = await graph.ainvoke({"messages": messages, "tool_results": []})
    return out.get("final", {"status": "error", "error": "no final state produced", "messages": messages})
