# Front-end (Snap4City dashboard)

Step 1 deliverable: a self-contained `widgetExternalContent` that draws routes on a
native `widgetMap` using Snap4City's own **graphhopper** engine. No backend / MCP /
bridge involved yet — the map computes and draws the route from two waypoints. Public
transport is drawn as a graphhopper **multimodal** trajectory (walk + ride legs).

The natural-language chat box and the FastAPI bridge to the MCP `run_advisor` come in a
later step (NL needs Llama4, which is only reachable through the bridge).

## File

- `mobility_advisor_dashboard.html` — paste into a `widgetExternalContent`.

## Put it on your dashboard

1. On your Snap4City dashboard, add a **widgetMap** and note its widget id
   (Dashboard Management / the widget's id, e.g. `w_Map_xxxx_widgetMapyyyyy`).
2. Add a **widgetExternalContent**. In "More options", enable **Enable CKEditor**, and
   paste the whole content of `mobility_advisor_dashboard.html` into the CKEditor box.
3. In the pasted script, set `MAP_WIDGET_ID` to the widgetMap id from step 1.

## Test

- **Fallback (raw coords, always works):** Origin `43.7734,11.2559`,
  Destination `43.7766,11.2480`, mode **Pedestrian** → **Compute** → the widgetMap draws
  the walking line. Proves the graphhopper draw path works.
- **Car:** same two points, mode **Car** → a driving line.
- **Public Transport (multimodal):** mode **Public Transport** → a multi-color line
  (walking green + ride blue) with start/finish icons.
- **Clear:** removes the trajectory.
- **Place names (depends on geocode endpoint):** Origin `Piazza del Duomo, Firenze`,
  Destination `Santa Croce` → works if the servicemap text-search proxy is reachable;
  otherwise the status line says geocode failed — use a `lat,lng` pair instead.

## Notes

- `GEOCODE_BASE` uses the dashboard's same-origin `superservicemapProxy.php`. The exact
  text-search parameters should be confirmed on the JupyterHub; raw `lat,lng` input is
  the guaranteed path meanwhile.
- Coordinates from geocoding are GeoJSON `[lng, lat]`; the code maps them to `{lat,lng}`.
