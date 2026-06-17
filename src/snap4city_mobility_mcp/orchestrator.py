"""Langgraph mobility advisor — deterministic orchestration layer.

A natural-language query drives a single linear graph:

  understand → execute → respond → END

- `understand` (LLM, forced tool call): extracts {intent, origin_text,
  destination_text, mode} from the latest user turn (place TEXT only — coordinates
  come from a tool). Resolves follow-ups ("那坐公交呢?") against the conversation.
  A forced `tool_choice` guarantees structured output, so this stage is reliable.
- `execute` (pure Python, NO LLM): for a `route` intent it deterministically runs
  the fixed tool flow — geocode origin, geocode destination, then `routing` with
  the requested mode — instead of letting the model free-call tools. tpl_* intents
  dispatch to the deterministic discovery chains in tpl.py (`run_tpl_flow`); only
  "other" falls through to a friendly "unsupported" reply.
- `respond` (LLM, tool_choice="none" — NO tools): phrases a concise, multilingual
  answer from the structured results, then assembles the widget JSON (incl. the
  FULL route WKT and the updated `messages` for multi-turn).

Why deterministic: Llama4 with `tool_choice="auto"` is unreliable — when it wants
to narrate ("retrying with a different profile…") it emits tool calls as pythonic
TEXT instead of structured `tool_calls`, which leak into the final answer (see
lesson L13). Letting the model pick *slots* (forced) and *prose* (none), while
Python drives the tools, removes that failure mode entirely.

Deterministic MCP execution + km4city quirk handling live in mcp_tools.py; the
Llama4 client in llm.py. Runs end-to-end only on the Snap4City JupyterHub.
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

# Synthetic function (not an MCP tool) — forces structured output from `understand`.
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
            # All fields required: Llama4 only fills required params, silently dropping
            # optional ones (a real run extracted origin but no destination). '' marks
            # a genuinely absent slot.
            "required": [
                "request_type", "info_kind", "origin_text", "destination_text",
                "mode", "agency_text", "line_text", "stop_text",
            ],
        },
    },
}


class AdvisorState(TypedDict, total=False):
    messages: list[dict[str, Any]]  # chat history (system + user + assistant) for multi-turn
    intent: str  # from understand: route | tpl_lines | tpl_routes | tpl_stops | tpl_timeline | other
    slots: dict[str, Any]  # understand output: {request_type, info_kind, intent (mapped), origin_text, destination_text, mode, agency_text, line_text, stop_text}
    tool_results: list[dict[str, Any]]  # structured audit: [{name, args, result}] per call
    unsupported: bool  # True when execute could not run a deterministic flow ("other" intent / missing required slots)
    final: dict[str, Any]  # widget JSON assembled by respond node


# Two-axis classification (request_type + info_kind) folds into the internal `intent`
# vocabulary the deterministic flow dispatches on, so execute/tpl/respond stay unchanged.
_INFO_KIND_TO_INTENT = {
    "lines": "tpl_lines",
    "routes": "tpl_routes",
    "stops": "tpl_stops",
    "timeline": "tpl_timeline",
}


def _request_to_intent(slots: dict[str, Any]) -> str:
    """Map the LLM's two-axis classification to one internal `intent` string.

    journey → route; transit_info → tpl_<info_kind> (unknown/blank info_kind → other,
    handled downstream as unsupported); anything else → other."""
    rt = slots.get("request_type")
    if rt == "journey":
        return "route"
    if rt == "transit_info":
        return _INFO_KIND_TO_INTENT.get(slots.get("info_kind") or "", "other")
    return "other"


async def understand(state: AdvisorState, *, llm: Llama4Client) -> dict[str, Any]:
    """LLM extracts slots from the latest user turn via a forced tool call.

    A forced `tool_choice` makes the gateway return structured `tool_calls`, so this
    stage never falls back to the pythonic-text shape that plagues free tool use.
    The model classifies on two axes (request_type + info_kind); `_request_to_intent`
    folds that into the internal `intent` the rest of the graph dispatches on.
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
        # Fall back to {"intent": "other"} — execute treats it as unsupported; the
        # debug log keeps the cause visible (LLM error vs. genuinely empty slots).
        logger.debug("understand slot extraction failed: %s", e)
    logger.debug("understand slots: %s", slots)
    return {"slots": slots, "intent": slots.get("intent", "other")}


def _pick_coord(geocode: Any, search: str) -> list[float] | None:
    """Best feature's `[lng, lat]` for `search` from an `address_search_location`
    result, or None.

    The server ranks fuzzy POI hits above the real place (L17: "Piazza Duomo"'s
    first feature was a company 1.1 km west of the square), so prefer the first
    feature whose address/name tokens are all covered by the search text — strict
    on extra tokens, so that company hit never matches. When no label matches
    (e.g. stations, whose POI features carry no address) the server's first hit
    stands. `exec_tool` already pins the result to Tuscany and returns
    `{"error": ...}` when nothing matches, so a non-FeatureCollection / empty
    payload yields None here.
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
            # A label that is just the municipality ("FIRENZE") would match any
            # search ending in ", Firenze" — never a useful pick target.
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


# Walking modes share a deterministic fallback: a walking route that fails is retried
# once with the other foot profile (the profiles hit different graph paths, so one can
# succeed where the other returns an empty body) — the same recovery the model used to
# attempt by hand, now driven by Python so it always runs. Applied once, never across
# semantics (car / public_transport failures get alternatives suggested by `respond`).
_FOOT_FALLBACK = {"foot_quiet": "foot_shortest", "foot_shortest": "foot_quiet"}


async def execute(state: AdvisorState, *, client: Client) -> dict[str, Any]:
    """Deterministically run the tool flow for the extracted intent (NO LLM).

    `route`: geocode both endpoints, then `routing` with the requested mode (+ a
    foot profile retry). tpl_* intents delegate to tpl.run_tpl_flow (discovery
    chains). Every call is recorded in `tool_results` so the widget data can be
    mined from the audit. "other" / missing required slots set `unsupported` for
    the respond node.
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
        if logger.isEnabledFor(logging.DEBUG):  # json.dumps is not free on the hot path
            # The slim view no longer carries coordinates — log the picked one here.
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
        return {"tool_results": results, "unsupported": False}  # geocode error → respond explains

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
        if logger.isEnabledFor(logging.DEBUG):  # json.dumps is not free on the hot path
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
        # Live probe: the real PT arc shape has never been observed — dump the
        # first raw arcs so group_arc_legs' provisional grouping key can be
        # calibrated offline from debug.log (gitignored).
        first = (routed["journey"].get("routes") or [{}])[0]
        arcs = first.get("arc") or []
        logger.debug(
            "PT raw arcs (first 5 of %d): %s",
            len(arcs),
            json.dumps(arcs[:5], ensure_ascii=False)[:2000],
        )
    if isinstance(routed, dict) and "error" in routed and mode in _FOOT_FALLBACK:
        # Deterministic walking-profile fallback, single shot: the requested profile
        # already burned the full L3 stale ladder (~27 s), so the transient is ruled
        # out — this only probes the other foot graph path.
        await _route(_FOOT_FALLBACK[mode], attempts=1)

    return {"tool_results": results, "unsupported": False}


def _routetype_of(entry: dict[str, Any]) -> str | None:
    """The `routetype` of a routing audit entry, read back from its json `args`.
    None when args is absent or malformed (entries built by tests may carry no args)."""
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
                    "wkt": first.get("wkt"),  # FULL LINESTRING — not truncated
                    "distance_km": first.get("distance"),
                    "eta": first.get("eta"),
                    "duration": first.get("time"),
                    # "arcs": first.get("arc"),  # per-segment detail hidden — bloats payload
                    # ~90%; re-enable once referente confirms the dashboard widget needs it.
                    "source_node": journey.get("source_node"),
                    "destination_node": journey.get("destination_node"),
                }
                if _routetype_of(entry) == "public_transport":
                    # NEW field, pending referente confirmation (same status as
                    # data.arcs): walk/ride legs grouped from the journey arcs.
                    legs = group_arc_legs(first.get("arc") or [])
                    if legs:
                        data["legs"] = legs
                return data
            # Keep overwriting while scanning backwards: when every profile failed
            # (mode + foot fallback), the EARLIEST error — the mode the user actually
            # asked for — is the one worth surfacing, not the fallback's.
            if is_err:
                route_error = result["error"]
    return {"route_error": route_error} if route_error else {}


def _template_answer(
    intent: str, data: dict[str, Any], *, unsupported: bool, missing: list[str] | None = None
) -> str:
    """Deterministic fallback answer when the respond LLM is unavailable.

    Italian — the advisor's default language (Florence service; see RESPOND_SYSTEM)."""
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
        return "📍 " + " · ".join(bits)
    if data.get("route_error"):
        return f"⚠ {data['route_error']}"
    return "Mi dispiace, non sono riuscito a trovare un percorso per questa richiesta."


# Required slots per intent: route needs both places; the tpl table comes from
# tpl.py (single source — run_tpl_flow skips its chain on the same keys).
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
    """Deterministic suggestion key for a FAILED routing attempt (L19).

    Keeps the ZTL-vs-service-side judgement in Python instead of asking the respond
    LLM to pattern-match `result["error"]` — the two error strings come from
    mcp_tools.routing deterministically. None → no special hint; respond's generic
    error rule handles it (geocode failures, transient call errors, etc.).
    """
    if not isinstance(result, dict):
        return None
    err = result.get("error")
    if not isinstance(err, str):
        return None
    if "empty response from routing service" in err:
        # Service-side failure for this mode — NOT a ZTL/pedestrian restriction.
        return "service_empty_try_foot_or_later"
    if "empty routes list" in err and routetype in ("car", "public_transport"):
        # A car/PT route with no result is often Florence's ZTL/pedestrian core.
        return "car_pt_blocked_try_foot"
    return None


def _results_view(
    results: list[dict[str, Any]], *, unsupported: bool, missing: list[str] | None = None
) -> dict[str, Any]:
    """Compact, LLM-facing summary of what execute produced (slim — no huge WKT)."""
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
            # Surface WHICH mode this routing attempt used: on failure the LLM can
            # only suggest a sensible alternative ("in auto non si può, prova a
            # piedi") when it knows the mode that failed.
            routetype = _routetype_of(e)
            item["routetype"] = routetype
            # A deterministic hint replaces the per-case error-string rules the
            # respond prompt used to carry (L19): the prompt now just follows it.
            hint = _routing_hint(routetype, e.get("result"))
            if hint:
                item["hint"] = hint
        view.append(item)
    return {"status": "ok", "results": view}


async def respond(state: AdvisorState, *, llm: Llama4Client) -> dict[str, Any]:
    """LLM phrases a multilingual answer from the structured results (NO tools),
    then assembles the widget JSON. Falls back to a template if the LLM errors."""
    messages = list(state.get("messages") or [])
    intent = state.get("intent", "other")
    results = state.get("tool_results") or []
    unsupported = bool(state.get("unsupported"))
    data = extract_tpl_data(intent, results) if intent in TPL_INTENTS else _extract_data(results)
    # An intent that execute refused = slot extraction left a required slot blank
    # (route: a place; tpl: line/stop); respond then asks for it instead of
    # claiming the request is unsupported.
    missing = (
        _missing_slots(intent, state.get("slots") or {})
        if unsupported and intent in _REQUIRED_SLOTS
        else None
    ) or None

    user_query = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"), ""
    )
    # A prior assistant turn means this is a follow-up — respond then answers directly
    # without re-greeting (RESPOND_SYSTEM keys the greeting off this marker). `messages`
    # here is [history..., current user]; the current assistant turn is not appended yet.
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
            # Low temperature to stay grounded: at 0.7 Llama4 "helpfully" invented line
            # numbers / operators (ATAF) when RESULTS lacked them, and occasionally drifted
            # to English. Phrasing room is secondary to never fabricating. (Slots use 0.)
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
    # Widget JSON: the reply lives in `messages[-1].content` (OpenAI-standard) — no
    # custom top-level `answer` field. `status` is the JSend-style outcome
    # ("success"/"error"); `request_type` names the served intent; `data` is the route
    # payload; `messages` is the multi-turn history to pass back.
    return {
        "final": {
            "status": "success",
            "request_type": intent,
            "data": data,
            "messages": messages,  # updated history for multi-turn (last turn = the reply)
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


# Process-wide session state, built lazily on the first turn. Rebuilding these per
# turn re-fetched /apps.json and re-created the TokenManager (one [INIT]/[LOAD_TOKEN]
# stderr block per question); a chat session reuses them. Token refresh stays correct:
# TokenManager.get_token() re-checks expiry on every call.
_CFG: dict[str, Any] | None = None  # dashboard /apps.json topology — static
_LLM: Llama4Client | None = None  # owns the TokenManager


async def _session_deps() -> tuple[dict[str, Any], Llama4Client]:
    # Unlocked check-then-set: concurrent first turns may double-build (benign — last
    # write wins). An asyncio.Lock would bind to one event loop and break callers that
    # run each request in its own asyncio.run(); neither cached object is loop-bound.
    global _CFG, _LLM
    if _CFG is None:
        _CFG = await _build_config()
    if _LLM is None:
        _LLM = Llama4Client()
    return _CFG, _LLM


async def run_advisor(query: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Multi-turn mobility advisor. Returns widget JSON including updated `messages`.

    Pass the previous turn's `final["messages"]` back as `history` to continue the
    conversation (the CLI REPL and the dashboard both carry state this way). The MCP
    `Client` is deliberately reconnected per turn (clean lifecycle, cheap intranet
    handshake); config and LLM client persist for the whole process.
    """
    cfg, llm = await _session_deps()
    messages = list(history or [])
    messages.append({"role": "user", "content": query})
    async with Client(cfg) as client:
        graph = _build_graph(client, llm)
        out: AdvisorState = await graph.ainvoke({"messages": messages, "tool_results": []})
    return out.get("final", {"status": "error", "error": "no final state produced", "messages": messages})
