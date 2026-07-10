# Snap4City Smart City API: field-by-field observations

Backend reference: field-by-field notes from live-probing the km4city endpoints behind
referente's remote MCP server. §1 / §2 cover the underlying km4city `/location/` +
`/shortestpath` semantics (baseline); the actual remote tool signatures the advisor
calls (bare names, since a single-server config adds no prefix) live in §3.

Spec source: `ascapi-openapiv3.json` (OAS3, mirrored at https://www.km4city.org/swagger/external/ascapi-openapiv3.json).
Backend base URL: `https://www.snap4city.org/superservicemap/api/v1/` (what the remote tools call internally; we don't touch it directly).

---

## §1. Geocoding: `address_search_location` / km4city `/location/`

### Query
- `search` (string): free-text address / POI keywords
- `excludePOI` (bool, default true), `maxresults` (int, default 100), `logic` ("or"/"and"), `lang`

### Response shape (confirmed)

```jsonc
{
  "type": "FeatureCollection",
  "features": [
    {
      "geometry": { "type": "Point", "coordinates": [11.250053, 43.773357] },  // [lng, lat], GeoJSON order
      "properties": { "name": "CHIESA DI SANTA MARIA NOVELLA", "address": "...", "city": "..." }
    }
  ]
}
```

| Code path | Real field | Notes |
|---|---|---|
| `f["geometry"]["coordinates"][1]` → lat, `[0]` → lng | yes | order is `[lng, lat]` (GeoJSON) |
| `f["properties"]["name"]` → label | yes | uppercase KB service names, e.g. `CHIESA DI SANTA MARIA NOVELLA`; may be `null` |

### Binding gotchas

- **Not region-locked**: the index now holds Valencia (ES) / southern France / Maastricht (NL) entries, so `"...Firenze"` can return 100 out-of-region hits and zero Tuscan. There is no geo-constraint parameter; the client narrows to a city the user named ([mcp_tools._narrow_by_city](../src/snap4city_mobility_mcp/mcp_tools.py)) and otherwise picks the GPS-nearest candidate ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)) — no distance cap (a 150 km sentinel mis-killed legitimate named-city trips and was removed, L41). Usable data is effectively Tuscany-only (live-tested: no Brescia/Milan streets anywhere, L41), so out-of-region queries return fuzzy noise: test with Tuscan places.
- **POIs outrank the real place**: with `excludePOI=false`, `"Piazza del Duomo"` returns the `PRIZIO STEFANO` company before the actual square. The advisor geocodes in two passes (`excludePOI=true` first, POI fallback only when the address pass has no in-region hit, [mcp_tools._geocode_address_first](../src/snap4city_mobility_mcp/mcp_tools.py)), then picks the first feature whose label tokens are a subset of the search tokens ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)).
- **Same-name towns**: `"Piazza Duomo"` also matches squares in Castelnuovo / Pietrasanta (90 km away). A city the user names wins ([mcp_tools._narrow_by_city](../src/snap4city_mobility_mcp/mcp_tools.py)); otherwise the candidate nearest an anchor does ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)): the destination anchors on the resolved origin, the origin on the user's GPS (live-tested: without the origin anchor, "via Pisana 166" from a Florence origin picked Lucca's VIA PISANA — the server's first hit).
- **Pure-noise input gives HTTP 500**, not an empty FeatureCollection. Callers must tolerate 5xx, and an empty / `[]` result is not a clean "no match" signal.
- **Backend is non-deterministic over time**: the same string can return all-foreign one minute and the correct Tuscan hit the next. (The old bbox-keyed retry went away with the bbox filter, and the GPS far-sentinel was later removed too — named-city narrowing + GPS-nearest picking are the only client-side selection, L41.)

---

## §2. Routing: `routing` / km4city `/shortestpath`

### Query

| Param | Type | Notes |
|---|---|---|
| `startlatitude` / `startlongitude` / `endlatitude` / `endlongitude` | float | required (ranges enforced) |
| `routetype` | enum | `car` (default) / `public_transport` / `foot_quiet` / `foot_shortest`. **No `bicycle`** in this deployment. |
| `startdatetime` | str | optional; `DD/MM/YYYY, HH:MM` or ISO. Defaults to now. |

(The km4city `/shortestpath` backend itself uses `source` / `destination` = `"lat;long"` semicolon strings; the referente tool exposes the four float params above instead.)

### Response shape (confirmed against live API)

referente passes the **raw km4city `/shortestpath` envelope through** (mild relabeling). Top-level keys on success: `elapsed_ms`, `elapsed_osmdst_ms`, `elapsed_osmsrc_ms`, `node_id_time`, `pathsearch_time`, `message_version`, `response`, `journey`.

```jsonc
{
  "response": { "current_operation": "...", "error_code": "0", "error_message": "successful" },
  "journey": {
    "source_node":      { "lat": 43.77343, "lon": 11.25596, "node_id": "..." },
    "destination_node": { "lat": 43.77658, "lon": 11.24796, "node_id": "..." },
    "search_route_type": "shortest_foot_optimization",  // transformed label, NOT echoed input
    "search_max_feet_km": 1.0,
    "routes": [                       // <-- LIST, not dict (spec is wrong)
      {
        "wkt": "LINESTRING(11.2559 43.7734, ...)",  // full path, WGS84 lng/lat, map-renderable as-is
        "distance": 0.826,            // km (float)
        "eta": "11:28:15",            // wallclock arrival
        "time": "...",                // duration
        "arc": [ { "desc": "Via Ricasoli", "distance": 0.008, "transport": "foot" } ]  // turn-by-turn; "nd" = unnamed
      }
    ]
  }
}
```

| Code path | Real field | Notes |
|---|---|---|
| `journey.routes` | **LIST**, not dict | spec error; always `isinstance(..., list)` before indexing |
| `routes[0].wkt` | yes when found | WKT LINESTRING WGS84 `lng lat` |
| `routes[0].distance` | km (float) | 0.826 for a ~700 m Firenze walk |
| `journey.search_route_type` | transformed label | `foot_shortest` → `shortest_foot_optimization`; don't compare to input verbatim |

### Critical: `error_code == "0"` is the only success signal; `ok` / HTTP 200 != route found

`response.error_message` is non-empty even on success (`"successful"`), so never use it as a failure signal. `journey.routes` may be `[]` even on a 200 (car in a pedestrian zone, src==dst). Higher-level callers **must** check `len(routes) > 0`. `error_code != "0"` means failure (`-2` = route not found, `-1` = wrapper internal error). `arc[i].desc` gives turn-by-turn text; `"nd"` entries are common (label as "unnamed street").

The advisor's `respond` surfaces `routes[0].{wkt, distance, eta, time}` + source/destination_node into the widget `data` (full WKT) via [orchestrator._extract_data](../src/snap4city_mobility_mcp/orchestrator.py). The per-segment `arc` detail is currently commented out to slim the payload; re-enable if the dashboard widget needs it.

---

## §3. Referente remote MCP server: tool signatures (R0 probe 2026-05-28)

Source: `GET http://192.168.1.117:8000/apps.json` → `Client(cfg)` → `list_tools()`; [mcp_tools._build_config](../src/snap4city_mobility_mcp/mcp_tools.py) narrows to the `native` server and rewrites the intranet IP to `DASHBOARD_URL`. Server scope: `snap4agentic_advisor_native` only. Full raw schemas: [probe-native-tools.json](../probe-native-tools.json) (25 tools).

> **This is a 2026-05-28 snapshot.** Re-probe on the JupyterHub before relying on it, in case the native server version was bumped (the one-liner is in README §7).

### Tool name policy

Names appear **without server prefix** under a single-server config (as we use). FastMCP only prefixes when merging multiple servers. So `address_search_location`, not `snap4agentic_advisor_native_address_search_location` (see the `project-referente-endpoint` memory).

### Tools the advisor drives

Remote (referente server); forward geocoding and `bus_route` instead go to the LOCAL MCP server (mcp_server.py, L28/L29/L19):

| Tool | Required input | Notable optional | Purpose |
|---|---|---|---|
| `routing` | `startlatitude` + `startlongitude` + `endlatitude` + `endlongitude` (float) | `routetype` (default `car`; no bicycle), `startdatetime` | Best route between two GPS points (§2) |
| `coordinates_to_address` | `latitude` + `longitude` | — | Reverse geocode; labels a GPS-defaulted origin for the reply |
| `service_search_near_gps_position` | `latitude` + `longitude` | `categories`, `maxdistance` (km), `maxresults` | Nearest-category POIs: car parks + "farmacia più vicina" destinations |
| `service_info_dev` | `serviceUri` | `fromTime` | Latest realtime free-spaces for a car park |

Both routing/geocoding accept an optional `authentication` (Bearer); the probe surfaced no token requirement, so the advisor omits it (public km4city backend).

### Other native tools (not used by the orchestrator)

- **Geocoding / geometry**: `address_search_location` (server-side broken, L28 — the advisor uses its local equivalent), `get_municipality_boundary`, `distance_from_coordinates`, `wkt_to_geojson`, `geojson_to_wkt`, `point_within_polygon`
- **Service / IoT search**: `service_search_near_service`, `service_search_within_gps_area`, `service_search_within_polygon`, `service_search_along_path`, `service_info`, `get_service_categories`
- **Transport discovery** (tpl feature removed from the advisor 2026-07): `tpl_agencies`, `tpl_lines`, `tpl_routes_by_line`, `tpl_stops_by_route`, `tpl_stop_timeline`, `tpl_routes_by_stop`, `transport_routes_search_near_gps_position` / `_within_gps_area` / `_within_wkt_area`

### `routing` failure shapes observed

- **Shape A (top-level empty wrap)**: `{"error": ""}`, no `journey`. Causes: a short-window cold-start stale (transient, clears ≥ 5 s later), or a stable server-side wrapper bug (car-in-ZTL, and in fact all car / public_transport requests; retries don't clear).
- **Shape B (km4city envelope, negative code)**: `journey.routes` possibly empty + `response.error_code = "-N"` (`-1` wrapper internal, `-2` route not found).
- **Shape C (empty routes, success envelope)**: `error_code = "0"` but `journey.routes = []` (the graph search found no path, e.g. car in a pedestrian zone). Surfaced as `"no route found (empty routes list)"`.
- **Shape D (zero-distance route with real geometry)**: success envelope, `routes[0]` carries a plausible multi-point WKT but `distance = 0`, `time = "00:00:00"`, `eta` = the call time (live 2026-07-10, `routetype=car`, short intra-Florence OD). Surfaced as `"routing failed: zero-distance route (server-side data bug)"` with the `service_empty_try_foot_or_later` hint.

### Open questions (carry forward to referente)

- `routing` (car) can return a **zero-distance route**: real WKT polyline but `distance=0`, `time=00:00:00`, `eta` = call time (shape D above, 2026-07-10). Same family as the empty-body bug? Client fails the mode rather than telling the user "0 km".
- Will referente fix the car / public_transport empty-body bug? `routing` returns `{"error": ""}` for car (even a drivable non-ZTL destination) and public_transport (even with `startdatetime`), while foot_* work. This is server-side.
- SuperServiceMap (his backend, `www.snap4city.org/superservicemap/api/v1`): ranking is broken ("via zara firenze" ranks a Maastricht bus stop first, the L28 failure mode), federated `/shortestpath` 500s, and Brescia city has no data despite the GardaLake federation (Sirmione only). Which regions are actually supported, and will the ranking be fixed? (Client can switch via `S4C_SERVICEMAP_BASE` once it is.)
