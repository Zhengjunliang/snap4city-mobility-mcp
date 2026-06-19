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

- **Not region-locked**: the index now holds Valencia (ES) / southern France / Maastricht (NL) entries, so `"...Firenze"` can return 100 out-of-region hits and zero Tuscan. There is no geo-constraint parameter, so the advisor pins results to a Tuscany bbox client-side ([mcp_tools._filter_geocode_to_tuscany](../src/snap4city_mobility_mcp/mcp_tools.py)).
- **POIs outrank the real place**: with `excludePOI=false`, `"Piazza del Duomo"` returns the `PRIZIO STEFANO` company before the actual square. The advisor geocodes in two passes (`excludePOI=true` first, POI fallback only when the address pass has no in-region hit, [mcp_tools._geocode_address_first](../src/snap4city_mobility_mcp/mcp_tools.py)), then picks the first feature whose label tokens are a subset of the search tokens ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)).
- **Same-name towns**: `"Piazza Duomo"` also matches squares in Castelnuovo / Pietrasanta (90 km away). A city-name ladder (named city, then Florence default, then all Tuscany) narrows it ([mcp_tools._narrow_by_city](../src/snap4city_mobility_mcp/mcp_tools.py)).
- **Pure-noise input gives HTTP 500**, not an empty FeatureCollection. Callers must tolerate 5xx, and an empty / `[]` result is not a clean "no match" signal.
- **Backend is non-deterministic over time**: the same string can return all-foreign one minute and the correct Tuscan hit the next. A bounded retry fires only when the bbox filter leaves zero Tuscan hits.

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

### Tools the advisor drives (7)

| Tool | Required input | Notable optional | Purpose |
|---|---|---|---|
| `address_search_location` | `search` (str) | `excludePOI` (default true), `maxresults`, `logic`, `lang` | Address / POI → GeoJSON FeatureCollection (§1) |
| `routing` | `startlatitude` + `startlongitude` + `endlatitude` + `endlongitude` (float) | `routetype` (default `car`; no bicycle), `startdatetime` | Best route between two GPS points (§2) |
| `tpl_agencies` | — | — | List of public-transport agencies `{name, uri}` |
| `tpl_lines` | `agency` (URI) | — | Lines of an agency |
| `tpl_routes_by_line` | `line` (URI or shortName) | `agency` | Routes of a line; item keys `route` / `wktGeometry` |
| `tpl_stops_by_route` | `route` (URI) | — | Stops of a route; GeoJSON nested under `BusStops` |
| `tpl_stop_timeline` | `stop` (service URI) | — | Lines serving a stop (+ timetable/realtime, empty live) |

Both routing/geocoding accept an optional `authentication` (Bearer); the probe surfaced no token requirement, so the advisor omits it (public km4city backend). The `tpl_*` chain is driven deterministically by [tpl.run_tpl_flow](../src/snap4city_mobility_mcp/tpl.py).

### Other native tools (not used by the orchestrator)

- **Geocoding / geometry**: `coordinates_to_address`, `get_municipality_boundary`, `distance_from_coordinates`, `wkt_to_geojson`, `geojson_to_wkt`, `point_within_polygon`
- **Service / IoT search**: `service_search_near_gps_position`, `service_search_near_service`, `service_search_within_gps_area`, `service_search_within_polygon`, `service_search_along_path`, `service_info`, `service_info_dev`, `get_service_categories`
- **Transport areas**: `transport_routes_search_near_gps_position` / `_within_gps_area` / `_within_wkt_area`, `tpl_routes_by_stop`

### `routing` failure shapes observed

- **Shape A (top-level empty wrap)**: `{"error": ""}`, no `journey`. Causes: a short-window cold-start stale (transient, clears ≥ 5 s later), or a stable server-side wrapper bug (car-in-ZTL, and in fact all car / public_transport requests; retries don't clear).
- **Shape B (km4city envelope, negative code)**: `journey.routes` possibly empty + `response.error_code = "-N"` (`-1` wrapper internal, `-2` route not found).
- **Shape C (empty routes, success envelope)**: `error_code = "0"` but `journey.routes = []` (the graph search found no path, e.g. car in a pedestrian zone). Surfaced as `"no route found (empty routes list)"`.

### TPL chain: live shapes (2026-06-12)

A raw walk of the full chain on the JupyterHub found:

- **No single "Autolinee Toscane" / `AtF` / `ATAF` agency.** The example URI in the `tpl_lines` tool description (`.../resource/AtF`) **does not exist** in the live `tpl_agencies` list (54 agencies). The brand is split into ~40 sub-networks; Florence city = `Autolinee Toscane - Urbano Area Metropolitana Fiorentina` → `…_Agency_888-48` (with it, `line="6"` → 22 routes; ExtraUrbano Arezzo → `[]`).
- `tpl_routes_by_line.line` accepts a bare shortName (`"6"`). Item keys: `firstBusStop, lastBusStop, line, route, routeName, wktGeometry`; route URI is **`route`**, geometry is **`wktGeometry`** (not `wkt`).
- `tpl_stops_by_route` returns `[service-URI array, {"BusStops": {"features": [...]}}]`: GeoJSON nested under `BusStops` (no top-level `type`); each feature carries stop `name` / `serviceUri` / coordinates under `properties`.
- `tpl_stop_timeline` returns `{"BusStop": {features:[...]}, "busLines": {results:{bindings:[...]}}, "realtime": {}, "timetable": {}}`: it gives the stop + serving lines, but `realtime` / `timetable` came back **empty** (no datetime param; scheduled times appear unavailable server-side). `respond` reports the serving lines and says times are unavailable, never inventing them.

### Open questions (carry forward to referente)

- Why are `tpl_stop_timeline.timetable` / `.realtime` empty? Is there a date/time parameter or a different tool for actual departure times, or is the GTFS schedule simply not loaded?
- Will referente fix the car / public_transport empty-body bug? `routing` returns `{"error": ""}` for car (even a drivable non-ZTL destination) and public_transport (even with `startdatetime`), while foot_* work. This is server-side.
