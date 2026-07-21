# Front-end (Snap4City dashboard)

A natural-language **chat box** `widgetExternalContent` that asks the FastAPI bridge
(`api.py`, which wraps `run_advisor` on the JupyterHub) and draws the returned route on a
sibling `widgetMap`. The backend is the brain: it understands the question, geocodes, and
computes the route (WKT + distance/ETA); the front-end renders the reply and the line.

## How the map draws the route

Every route goes through the widgetMap's **manual** branch of `addCustomTrajectory`
(per-point `mode.routing.manual`): the widget just connects the given points with
straight segments, so the front end feeds it the backend's own route geometry and the
map **never calls a router** — the line appears together with the reply, straight from
the `/advise` response. A bus route ships its walk/ride split as per-leg geometry
(`data.routes[].legs`, cut by the backend from the single router response); foot/car draw
the whole route WKT as one leg. Each segment takes the current point's `color` (a string);
a point's non-empty `icon` becomes a marker — start/finish flags on the precise geocoded
origin/destination, the Gea-Night bus pin (`TransferServiceAndRenting_Urban_bus.png`) on
the board/alight vertices. A ride leg normally carries the real GTFS shape: the backend
swaps the router's one-vertex-per-stop chords for the matching km4city *tpl* shape cut;
when no line variant matches within tolerance, that leg degrades to the stop-to-stop
chords the router returned.

One trap that cost a debugging round (now encoded in the code): every point must carry
`mode` and an `icon` field (empty string = no marker) — a missing one crashes the map in
`addCustomTrajectoryToMap` (`Cannot read properties of undefined (reading 'length')`).

## File

- `mobility_advisor_dashboard.html` — paste into a `widgetExternalContent`.

## Put it on your dashboard

1. Add a **widgetMap** and note its widget id (e.g. `w_Map_xxxx_widgetMapyyyyy`).
2. Add a **widgetExternalContent**. In "More options", enable **Enable CKEditor**, and
   paste the whole content of `mobility_advisor_dashboard.html` into the CKEditor box.
3. In the pasted script, set `MAP_WIDGET_ID` to the widgetMap id, and `BRIDGE_BASE` to the
   URL where the FastAPI bridge (`api.py`) is reachable **from the browser** (decided per
   deployment — see below).

## Run the bridge (JupyterHub)

The bridge needs Llama4 + the MCP servers, all reachable only on the JupyterHub. Start
the local MCP server first (it serves geocoding and ALL routing — the `route` tool):

```
python -m snap4city_mobility_mcp.mcp_server   # :8020, terminal 1
uvicorn api:app --host 0.0.0.0 --port 8010    # terminal 2
```

Sanity-check without the dashboard:

```
curl -s localhost:8010/health
JOB=$(curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"da Piazza del Duomo a Santa Croce a piedi","history":[]}' \
  | python -c 'import sys, json; print(json.load(sys.stdin)["job_id"])')
curl -s localhost:8010/advise/$JOB    # 202 while computing, then the widget JSON
```

**Job + poll**: the POST only *starts* the turn (it answers `{job_id}` at once) and the
widget polls `GET /advise/{job_id}` until it returns 200. A bus turn takes ~50–70 s and the
proxy chain in front of the bridge cuts any single request past ~60 s — and heartbeat bytes do
not help, because `jupyter-server-proxy` buffers the whole body of a non-SSE response. Never
collapse the widget back into one long request.

Each 202 carries `{"status":"pending","stage":...,"elapsed_s":...}`; the widget rewrites its
"thinking" bubble from it (`STAGE_TEXT`), so a bus turn shows *"Calcolo il percorso in bus… 34s"*
rather than a blank bubble for the better part of a minute. An unrecognized stage falls back to
the generic bubble, so a new backend stage can never blank the line.

The collected 200 is the widget JSON `{status, request_type, data, messages}`; check that
`data.wkt` (the LINESTRING), `data.distance_km`, `data.duration`, and `data.mode` are
present and `messages[-1].content` is the Italian reply.

### Exposing the bridge to the browser

The browser reaches the bridge **same-origin** through `jupyter-server-proxy`:
`BRIDGE_BASE = https://www.snap4city.org/jupyterhub/user/<account>/proxy/8010`. The origin
must match the dashboard's exactly (`www.` included) — a different origin triggers a CORS
preflight that the proxy redirects to the login page, which the browser rejects.

Installing that extension on the JupyterHub's old `jupyter_server` needs care, because a
careless install upgrades the base Jupyter stack and bricks the singleuser server on its
next restart:

1. Pin the major version: `jupyter-server-proxy>=3.2,<4` (4.x demands `jupyter_server>=2`).
2. Install with `pip install --no-deps` so nothing upgrades `jupyter_server`/`jupyterlab`/`notebook`.
3. Add the one real dependency `--no-deps` skips: `pip install aiohttp`.
4. **Preflight without touching the running server**: `jupyter server extension list` should
   show the extension `OK`, then start a throwaway server (`jupyter server --port=9999
   --no-browser --ServerApp.token=''`) and confirm it loads with no traceback. Only then
   restart the main server from the Hub Control Panel.

CORS in `api.py` is dev-permissive (`*`) and should be tightened for a production deployment.

## GPS (near-me)

Each send calls `navigator.geolocation.getCurrentPosition` (5 s timeout, 60 s cache, low
accuracy) and POSTs the fix as `gps: {lat, lng}` — `null` on denial, unsupported API, or
timeout, and the backend then behaves exactly as before (asks for the origin when it is
missing). A `PERMISSION_DENIED` is remembered for the session so the user is not
re-prompted every turn.

Two environment requirements, both **outside this file's control**:

- **Secure context**: geolocation only works on HTTPS pages (the dashboard is HTTPS, ok).
- **Iframe permission**: the widget runs in the dashboard iframe; if the parent iframe
  lacks `allow="geolocation"` the prompt never appears and `getCurrentPosition` fails with
  code 1 — the widget silently degrades to the no-GPS flow. Whether Snap4City's
  widgetExternalContent iframes carry that permission is a platform setting: test with
  DevTools (run `navigator.permissions.query({name:'geolocation'})` in the iframe context)
  and ask the referente to add it if blocked.

## Notes

- The reply bubble is `messages[-1].content` (OpenAI standard, no custom `answer` field).
- Multi-turn: the front-end keeps `response.messages` and sends it back as `history`.
- All three modes are routed by the backend's local `route` tool (What-If GraphHopper) and
  drawn from its geometry — walking green, car blue, bus ride orange with bus pins at the
  board/alight stops. A walking-only bus itinerary (short trip — walking beats any bus)
  comes back relabeled as a foot route, so the map draws a plain green walking line.
- **Route picker**: a multi-mode turn (no mode specified → 2-3 routes back) fills the
  `#advChips` dock (a fixed strip between the chat and the input row) with one
  mode-named chip per route ("A piedi" / "In auto" / "In autobus" — distance/ETA already
  live in the reply text) plus "Mostra tutte". Tapping a chip redraws the map with
  **only** that route, shows its step-by-step block (`data.routes[].detail`,
  pre-rendered backend-side: fermate + orari for bus, turn-by-turn streets for foot/car)
  as a chat bubble, toggles the parking pins (car shows them, foot/bus hides them) and
  swaps the along-route service pins to that route's own set.
  The selection is **purely local** — no new backend turn (a bus re-route would cost
  another 30-45 s) — and only the one-line user echo ("Scelgo l'opzione: …") enters the
  carried `history`: that keeps the chosen mode in context for follow-ups while the
  detail block stays a bubble, off every later LLM prompt.
  Every routes-bearing turn **replaces** the dock (a single-route
  turn empties it); the dock hides itself when empty.
- **Along-route service pins**: when the user asked to see a category along the way, each
  route carries `data.routes[].services` and the map plots them with the same
  `addSelectorPin` pipeline as parking, violet pins — the widget resolves each
  `serviceUri` itself and renders the service's own category icon (a pharmacy pin looks
  like a pharmacy with zero icon code here). While all routes are shown the pin set is the
  deduped union; a chip narrows it to that route's list (bus lists only stop-vicinity and
  walking-leg services, backend rule).
- **Parking pins** are removed with the `removeSelectorPin` event **keyed by `desc`**
  (verified in `widgetMap.php`, disit/dashboard-builder: the map indexes each pin layer
  by `passedData.desc` and the remove handler also dereferences `passedData.query`
  unguarded) — the clear must resend the same `desc` + `query` + `display` used on add.
  Sending only `{query, queryType}` makes removal a silent no-op and pins pile up.
  Service pins ride the same rule with their own bookkeeping.
