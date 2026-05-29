# Snap4City Smart City API — Field-by-field Observations

**Backend reference** — field-by-field notes from live-probing the public km4city endpoints during Phase 4. The local stand-in `server.py` that originally consumed these was retired in Phase 5 §2 (切远程 server); referente's remote MCP server is presumed to wrap the same km4city backend, so §1 / §2 stay useful as a baseline. The **actual remote tool signatures** (names with server-prefix, inputSchemas as exposed by the dashboard) live in §3 — populated from the R0 probe output.

Spec source: `ascapi-openapiv3.json` (OAS3, mirrored at https://www.km4city.org/swagger/external/ascapi-openapiv3.json).
Backend base URL: `https://www.snap4city.org/superservicemap/api/v1/` (what the remote tools call internally; we don't touch it directly).

---

## §1. `GET /location/` — geocoding (Step 1.4 observations)

### Query
- `search` (string) — free-text address / POI keywords
- `maxResults` (int) — defaults to 10, capped to a reasonable number per request

### Response shape (confirmed)

```jsonc
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Point",
        "coordinates": [11.250053, 43.773357]   // [lng, lat] — GeoJSON order
      },
      "properties": {
        "name": "CHIESA DI SANTA MARIA NOVELLA"
        // ... other fields not yet inspected
      }
    }
  ]
}
```

### Field-name evidence

| Code path in `client.geocode` | Confirmed real field | Notes |
|---|---|---|
| `f["geometry"]["coordinates"][1]` → lat | yes | order is `[lng, lat]` per GeoJSON |
| `f["geometry"]["coordinates"][0]` → lng | yes | |
| `f.get("properties", {}).get("name")` → address label | yes | uppercase strings, e.g. `CHIESA DI SANTA MARIA NOVELLA`, `MUSEO DI SANTA MARIA NOVELLA`. Looks like Knowledge-Base service names, not literal street addresses. |

### Critical: default search hits the Knowledge-Base service catalogue, not real addresses

Without `excludePOI=true`, `/location/?search=...` matches against POI / service names from the Snap4City graph — the spec only mentions "names of the streets, civic number, municipality names and service names", but the default behaviour is dominated by service names. Concretely:

- `Piazza del Duomo, Firenze` returned `PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S.` (a company at lat 43.7736 lng 11.2421), not the actual square.
- `Stazione di Santa Maria Novella, Firenze` returned `(lat 37.7871, lng 20.8993, address=null)` — a point on the Greek island of Zakynthos, presumably because the tokenizer matched the word "di" to some catalogue entry there.

`client.geocode` now always passes `excludePOI=true` to force the search onto street / civic / municipality names. A retest with this fix is the next verification milestone.

### Behaviour with noisy or out-of-region input is inconsistent

- `Stazione Termini, Roma` (clean input) → returned `Biblioteca del Club Alpino Italiano - Sezione E. Bertini` at `(43.8809, 11.0957)`, in Tuscany. The API's index does not cover Lazio; out-of-region queries fall back to a fuzzy in-region match. **Conclusion: the deployment is region-locked to Tuscany.**
- `asdfasdfasdf` (clean input) → **HTTP 500 server error**: `Server error '500 ' for url '.../location/?search=asdfasdfasdf&maxResults=1'`. Pure-noise input is not handled gracefully; the API blows up rather than returning an empty FeatureCollection.
- `asdfasdfasdf` wrapped in a JSON-shaped string (earlier contaminated test) → returned `CLAUS TATTOO DI CLAUDIO CARLO ANDRESSI`. Behaviour clearly depends on whether the tokenizer can extract anything to match.

### Implications for callers

1. The earlier code comment in `client.geocode` claiming *"Returns an empty list when no match is found"* described a case that does not occur — `[]` is not a "no result" signal. The docstring has been corrected.
2. Higher-level callers must be ready for either a fuzzy match in the wrong region or an HTTP 500. The planned `mobility_advisor` orchestrator should:
   - Wrap `geocode` in a try/except that converts 5xx into a clear `ValueError("could not resolve address X — try a more specific Tuscany-area address")`.
   - Apply a sanity-check on the returned point (distance from a Florence-area centre, or distance between origin and destination) before feeding it to `/shortestpath`.
3. Address `properties.name` may be `null` (Zakynthos case) — `f.get("properties", {}).get("name")` correctly degrades to `None` and surfaces as `null` in the MCP response.

### Step 1.4 raw outputs

Run 1 (Inspector input contaminated by nested JSON wrapping — keywords seen by API were the literal text `{"address": "..."}`):

- `Santa Maria Novella, Firenze` (`maxResults=3`) → 3 hits, top: `(43.7734, 11.2501)` `CHIESA DI SANTA MARIA NOVELLA`.
- `asdfasdfasdf` → `(43.7232, 10.9741)` `CLAUS TATTOO DI CLAUDIO CARLO ANDRESSI`.
- `Stazione Termini, Roma` → `(43.8809, 11.0957)` `Biblioteca del Club Alpino Italiano`.

Run 2 (clean input, `excludePOI` not yet enabled):

- `Stazione di Santa Maria Novella, Firenze` → `(37.7871, 20.8993)` `address: null` — Greek island of Zakynthos. Confirms the POI/catalogue mismatch.
- `Piazza del Duomo, Firenze` → `(43.7736, 11.2421)` `PIAZZA DUOMO DI PRIZIO STEFANO & C. S.A.S.` — wrong type of result (company), but correct city.
- `Stazione Termini, Roma` → `(43.8809, 11.0957)` `Biblioteca del Club Alpino Italiano` — confirms the Tuscany region lock.
- `asdfasdfasdf` → HTTP 500.

Run 3 (with `excludePOI=true`) — pending.

---

## §2. `GET /shortestpath` — routing (Stage M2 observations, 2026-05-25)

### Query (per OpenAPI spec at `ascapi-openapiv3.json`)

| Param | Type | Required | Default | Notes |
|---|---|---|---|---|
| `source` | string | yes (de facto) | — | `"lat;long"` (**semicolon**, not comma) or service URI. Spec marks "no", but omitting returns no route. |
| `destination` | string | yes (de facto) | — | Same format as `source`. |
| `routeType` | string enum | no | `foot_shortest` | One of `public_transport`, `foot_shortest`, `foot_quiet`, `car`. **No `bicycle`** in this deployment. |
| `startDatetime` | ISO8601 | no | now | Used in returned `arc[i].start_datetime` / `end_datetime`. |
| `format` | string | no | `json` | `json` \| `html`. We hardcode `json`. |
| `uid` / `requestFrom` | string | no | — | User identifier params; not used by our tool. |

### Response shape (confirmed against live API)

The spec is **incomplete and partially wrong**. Real response top-level has 8 keys (spec documents 2):

```jsonc
{
  "elapsed_ms": 158,                 // total wall time, spec MISSING
  "elapsed_osmdst_ms": 12,           // OSM lookup time for destination, spec MISSING
  "elapsed_osmsrc_ms": 8,            // OSM lookup time for source, spec MISSING
  "node_id_time": 6,                 // node resolution time, spec MISSING
  "message_version": "...",          // spec MISSING
  "pathsearch_time": 76.85998,       // spec documented; unit not given - likely ms (inferred: 76 vs elapsed_ms=158 same magnitude)
  "response": "...",                 // status hint, spec MISSING
  "journey": {                       // spec documented
    "source_node":      {"lat": 43.77343, "lon": 11.25596, "node_id": "6008587975"},
    "destination_node": {"lat": 43.77658, "lon": 11.24796, "node_id": "2603441810"},
    "search_route_type": "shortest_foot_optimization",  // NOT echoed input; transformed label
    "search_max_feet_km": 1.0,        // spec MISSING
    "start_datetime": "11:04:08",
    "routes": [                       // <-- LIST, not dict as spec claims
      {
        "wkt": "LINESTRING(11.2559 43.7734, ...)",  // full path geometry
        "distance": 0.826,            // km (float, not meters)
        "eta": "11:28:15",            // wallclock arrival time
        "time": 1207,                 // unit unverified - probably seconds (inferred from magnitude vs distance/walking-pace); needs explicit probe
        "arc": [                      // turn-by-turn segments
          {
            "desc": "Via Ricasoli",   // street name (or "nd" if unknown)
            "distance": 0.008,        // segment km
            "source_node": {...},
            "destination_node": {...},
            "start_datetime": "11:04:08",
            "end_datetime": "11:04:14",
            "transport": "foot",
            "transport_provider": "private",
            "transport_service_type": "private transport"
          },
          // ... more arcs
        ]
      }
    ]
  }
}
```

### Field-name evidence

| Code path | Real field | Notes |
|---|---|---|
| `payload["journey"]["routes"]` | LIST, not dict | **Spec error**. Always check `isinstance(..., list)` before indexing. |
| `payload["journey"]["routes"][0]["wkt"]` | yes, when route found | WKT LINESTRING in WGS84 `lon lat` order (GeoJSON-like). Suitable for direct map render (Leaflet, MapLibre). |
| `payload["journey"]["routes"][0]["arc"]` | array of turn-by-turn segments | Good for human-readable directions. `desc` is street name or `"nd"`. |
| `payload["journey"]["routes"][0]["distance"]` | km (float) | Confirmed via 0.826 for ~700m Firenze walk. |
| `payload["journey"]["search_route_type"]` | transformed label | `foot_shortest` → `shortest_foot_optimization`; `car` → `fastest_car_optimization`. Don't compare to input verbatim. |

### Critical: `ok=true` does NOT mean a route was found

Across our 3 probe cases:

| Case | `route_count` | `has_wkt` | Notes |
|---|---|---|---|
| Firenze Duomo → SMN, `foot_shortest` | 1 | true | Happy path: 0.826 km, eta `11:28:15`, 50 arc segments |
| Same coords, `car` | 0 | false | API succeeds (`ok=true`) but returns empty routes list — likely pedestrian-only zone blocks car routing |
| Src == Dst, `foot_shortest` | 0 | false | API gracefully returns empty routes — no 4xx |

**Implication for callers**: `data["journey"]["routes"]` may be `[]` even when the HTTP request succeeded. Higher-level callers (M3 orchestrator) **must** check `len(routes) > 0` before assuming a path exists. The MCP envelope's `ok=true` only signals "request reached API and parsed", not "route found".

### Step 2 raw probe outputs (2026-05-25)

```
--- foot_shortest (Duomo -> SMN) ---
ok=true, pathsearch_time=76.86ms, route_count=1
  distance=0.826km, eta=11:28:15, arc_segments=50
  wkt_head="LINESTRING(11.255959400000009 43.77343290000008, ..."

--- car (Duomo -> SMN) ---
ok=true, pathsearch_time=860.06ms, route_count=0
  (no routes - pedestrian zone presumably blocks car path)

--- foot_shortest (src == dst) ---
ok=true, pathsearch_time=57.03ms, route_count=0
  (graceful empty - no crash, no 4xx)
```

### Implications for M3 orchestrator

1. After calling `shortestpath`, **always** check `payload["journey"]["routes"]` non-empty before extracting distance/wkt/arc.
2. If empty, surface a user-friendly "no route found between these points by {mode}" message; suggest trying a different `route_type`.
3. The `wkt` LINESTRING is directly map-renderable — pass to dashboard widget as-is.
4. The `arc[i].desc` chain ("Via Ricasoli" → "nd" → ...) gives turn-by-turn text; `"nd"` (no data) entries are common and should be filtered or labeled "(unnamed street)".

### Open questions (for future stages)

- Does `public_transport` `route_type` return GTFS-like leg info (line/route URIs that link to `tpl_*` tools)?
- What does `routes[0].time` measure exactly (seconds total? matches arc sum?) — needs probe.
- Is `eta` always wallclock-only (HH:MM:SS) or sometimes ISO datetime? Probe with `startDatetime` set explicitly.

---

## §3. Referente remote MCP server — tool signatures (R0 probe 2026-05-28)

Source: `http://localhost:8000/apps.json` → `Client(cfg)` → `list_tools()` after rewriting `192.168.1.117` → `localhost`. Server scope: `snap4agentic_advisor_native` only (legacy will be removed per dashboard warning; experimental is geometry helpers). Full raw schemas live in [probe-native-tools.json](../probe-native-tools.json) (24 tools, ~1350 lines, gitignore-worthy but kept for now).

### Tool name policy

Names appear **without server prefix** when `Client(cfg)` is built with a **single-server config** (as we do). FastMCP only prefixes when merging multiple servers to disambiguate. So `address_search_location` not `snap4agentic_advisor_native_address_search_location` — call them as listed below. If we ever mount native + experimental together, the prefix would kick in and these calls would break — flag this in [[project-referente-endpoint]] memory.

### Tools used by `run_trip()` orchestrator

| Tool | Required input | Notable optional input | Purpose |
|---|---|---|---|
| `address_search_location` | `search` (str) | `maxresults` (int, default 100), `logic` ("or"/"and"), `excludePOI` (bool, default true), `lang`, `authentication` | Fuzzy address / POI → GeoJSON FeatureCollection. **Backend = km4city `/location/`, see §1.** |
| `routing` | `startlatitude` + `startlongitude` + `endlatitude` + `endlongitude` (all float, ranges enforced) | `routetype` (default `car`, enum `car`/`public_transport`/`foot_quiet`/`foot_shortest` — **no bicycle**), `startdatetime` (free-form, accepts `DD/MM/YYYY, HH:MM` or ISO) | Best route between two GPS points. **Backend = km4city `/shortestpath`, see §2.** Output shape needs live invocation (description says "path, duration, and instructions" — TBD whether it's the raw km4city `journey` envelope or a flattened referente-side reshape). |

**Signature deltas vs the retired local stand-in** (see git history for `server.py` if needed):
- `locations` → `address_search_location` (renamed)
- `max_results` → `maxresults` (no underscore)
- `shortestpath` → `routing` (renamed)
- `source: "lat;lng"` string → `startlatitude` + `startlongitude` as **separate floats**
- `destination` → `endlatitude` + `endlongitude` (same)
- `route_type` → `routetype` (no underscore)
- `start_datetime` → `startdatetime` (no underscore)
- Auth is still optional `authentication` (Bearer) on both — probe didn't surface a token requirement, public km4city backend stays public-side.

### Other tools the native server exposes (not used by orchestrator yet)

Listed for future reference / Phase 5 §3 (LLM) and §4 (dashboard) work. All names without prefix per single-server policy above:

- **Geocoding / addresses**: `coordinates_to_address` (reverse), `get_municipality_boundary`
- **Distance / geometry**: `distance_from_coordinates` (Haversine), `wkt_to_geojson`, `geojson_to_wkt`, `point_within_polygon`
- **Service search (POI / IoT)**: `service_search_near_gps_position`, `service_search_near_service`, `service_search_within_gps_area`, `service_search_within_polygon`, `service_search_along_path`, `service_info`, `service_info_dev`, `get_service_categories`
- **Public transport (TPL)**: `tpl_agencies`, `tpl_lines`, `tpl_routes_by_line`, `tpl_stops_by_route`, `tpl_stop_timeline`, `tpl_routes_by_stop`, `transport_routes_search_near_gps_position`, `transport_routes_search_within_gps_area`, `transport_routes_search_within_wkt_area`

The TPL family covers what the old `Sample tool.py` candidate code did, so no need to extract our own tpl_* MCP tools — referente already has them.

### `routing` output envelope (R4 happy path 2026-05-28)

Confirmed referente passes the **raw km4city `/shortestpath` envelope through** (with only mild relabeling). Top-level keys observed on success:

```
elapsed_ms, elapsed_osmdst_ms, elapsed_osmsrc_ms, node_id_time, pathsearch_time,
message_version, response, journey
```

- `response`: `{current_operation, error_code, error_message}` — **see lesson L7**: `error_code == "0"` is the only authoritative success indicator. `error_message` is **non-empty even on success** (e.g. `"successful"`), so using it as a failure signal will erase happy paths. `error_code != "0"` → failure (e.g. `-2` = route not found, `-1` = referente wrapper internal error).
- `journey.routes`: list (often single element). Each element:
  - `distance` (float, km)
  - `eta` (str "HH:MM:SS", wallclock end time)
  - `time` (str "HH:MM:SS", duration)
  - `wkt` (str, LINESTRING WGS84 lng/lat — same as old stand-in)
  - `arc`: list of fine-grained segments, each with `desc / source_node / destination_node / start_datetime / end_datetime / distance / transport / transport_provider / transport_service_type`
- `journey.source_node` / `journey.destination_node`: `{lat, lon, node_id}` (OSM node id)
- `journey.search_max_feet_km` / `journey.search_route_type` / `journey.start_datetime` (ISO with Z)

So R4 verified our orchestrator's `_format_output` correctly surfaces `routes[0].distance / eta / wkt` into `summary` — these field names match the retired km4city stand-in baseline (L2).

### Failure shapes observed in the wild

- **Shape A (top-level empty wrap)** — payload `{"error": ""}` no `journey`. Two known causes:
  - L3 short-window stale (transient, retries clear it ≥ 5s later)
  - L8 referente car-in-ZTL wrapper bug (stable, retries don't clear)
- **Shape B (km4city envelope with negative code)** — `journey.routes = [...]` possibly empty + `response.error_code = "-N"`. Real km4city failure:
  - `-1` ≈ wrapper internal hash key missing (seen on cold start with foot_shortest, transient)
  - `-2` ≈ "route not found" (src==dst, no path, etc.)
- **Shape C (empty routes, success envelope)** — `response.error_code = "0"` but `journey.routes = []`. Same as L2: km4city said OK but graph search returned no path (e.g. car in pedestrian zone). orchestrator surfaces this as `"no route found (empty routes list)"`.

### Open questions (carry forward)

- ~~What does `routing` actually return on the wire?~~ → Resolved above
- Does `address_search_location` envelope a `{features, type}` GeoJSON directly or wrap it? — R4 inferred yes (orchestrator's `_first_coord` works), but precise top-level shape not pinned. Run a single probe call when convenient.
- Do TPL tools accept Florence `AtF` agency URI as-is? (Phase 5 §3 / §4)
- Will referente fix the car-ZTL wrapper bug (L8)? — Watch for next dashboard / native version bump.
