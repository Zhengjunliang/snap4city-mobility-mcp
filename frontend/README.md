# Front-end (Snap4City dashboard)

A natural-language **chat box** `widgetExternalContent` that asks the FastAPI bridge
(`api.py`, which wraps `run_advisor` on the JupyterHub) and draws the returned route on a
sibling `widgetMap`. The backend is the brain: it understands the question, geocodes, and
computes the route (WKT + distance/ETA); the front-end renders the reply and the line.

## How the map draws the route (Step-0 finding)

The widgetMap's `addCustomTrajectory` does **not** paint a raw polyline — it routes through
waypoints with Snap4City's own **graphhopper**. So the backend route WKT is parsed,
downsampled to ~12 vertices, and fed as waypoints with a graphhopper `mode`; the map traces
our route through them (same engine, so the line matches the backend route).

Two traps that cost a debugging round (now encoded in the code + `docs/lessons.md`):

- Each point's `mode.routing.graphhopper.type` **must** be the front-end vehicle name
  `foot` / `car` / `bus` — **not** the MCP routetype `foot_shortest`. A wrong value makes
  graphhopper return nothing and the map crashes in `addCustomTrajectoryToMap` (`Cannot
  read properties of undefined (reading 'length')`).
- Each point needs a `mode` (omitting it crashes the same way). Icons go on the first/last
  point only.

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

The bridge needs Llama4 + the MCP server, both reachable only on the JupyterHub:

```
uvicorn api:app --host 0.0.0.0 --port 8010
```

Sanity-check without the dashboard:

```
curl -s localhost:8010/health
curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"da Piazza del Duomo a Santa Croce a piedi","history":[]}'
```

The POST returns the widget JSON `{status, request_type, data, messages}`; check that
`data.wkt` (the LINESTRING), `data.distance_km`, `data.eta`, and `data.mode` are present
and `messages[-1].content` is the Italian reply.

> **Browser → bridge reachability is still open.** `jupyter-server-proxy` is not installed
> (and crashed the server once), so this round is curl-only. Decide between installing
> server-proxy or a same-origin proxy before wiring the dashboard end-to-end, minding HTTPS
> mixed-content / CORS. CORS in `api.py` is dev-permissive (`*`) and must be tightened.

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
- Public transport (`bus`) is wired via the backend `bus_route` tool (What-If GraphHopper,
  `docs/lessons.md` L19/L34). A walking-only itinerary (short trip — walking beats any bus,
  L39) comes back relabeled as a foot route, so the map draws a fast green walking line.
  For a real bus route the map still re-routes `type:"bus"` itself against the online
  whatif-router (slow until the referente deploys the perf patch + GTFS, L39).
