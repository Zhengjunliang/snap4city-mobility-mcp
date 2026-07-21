# Snap4City Smart City API: field-by-field observations

Backend reference: field-by-field notes from live-probing the km4city endpoints behind
referente's remote MCP server. §1 covers the geocoding semantics the advisor depends on, §2
the remote tool signatures it calls (bare names — a single-server config adds no prefix), §3
the probe evidence for why forward geocoding is served locally.

Spec source: `ascapi-openapiv3.json` (OAS3, mirrored at https://www.km4city.org/swagger/external/ascapi-openapiv3.json).
Backend base URL: `https://www.snap4city.org/superservicemap/api/v1/` (what the remote tools call internally; we don't touch it directly).

---

## §1. Geocoding: `address_search_location` / km4city `/location/`

Served by our **local** MCP server (`mcp_server.py`) wrapping the public km4city ServiceMap —
the remote tool of the same name is server-side broken (see §3). The response
shape below is what both return, so the client's parsing is identical either way.

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
      "properties": { "name": "CHIESA DI SANTA MARIA NOVELLA", "address": "...", "city": "...",
                      "serviceType": "StreetNumber", "civic": "3" }  // civic fields on /location/ house-number hits only
    }
  ]
}
```

`f["geometry"]["coordinates"]` is `[lng, lat]` (GeoJSON order); `properties.name` is an
uppercase KB service name and may be `null`.

### Binding gotchas

- **Not region-locked**: the index holds Valencia (ES) / southern France / Maastricht (NL) entries, so `"...Firenze"` can return 100 out-of-region hits and zero Tuscan. There is no geo-constraint parameter; the client narrows to a city the user named ([mcp_tools._narrow_by_city](../src/snap4city_mobility_mcp/mcp_tools.py)) and otherwise picks the candidate nearest an anchor ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)) — no distance cap (a 150 km sentinel mis-killed legitimate named-city trips and was removed: naming a far-away city is a legitimate query). Usable data is effectively Tuscany-only (live-tested: no Brescia/Milan streets anywhere), so out-of-region queries return fuzzy noise: test with Tuscan places.
- **POIs outrank the real place**: with `excludePOI=false`, `"Piazza del Duomo"` returns the `PRIZIO STEFANO` company before the actual square. The advisor geocodes in two passes (`excludePOI=true` first, POI fallback only when the address pass has no named-city hit, [mcp_tools._geocode_address_first](../src/snap4city_mobility_mcp/mcp_tools.py)), then prefers features whose label tokens are a subset of the search tokens ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)).
- **Same-name towns**: `"Piazza Duomo"` also matches squares in Castelnuovo / Pietrasanta (90 km away). A city the user names wins; otherwise the candidate nearest an anchor does — the destination anchors on the resolved origin, the origin on the user's GPS (live-tested: without the origin anchor, "via Pisana 166" from a Florence origin picked Lucca's VIA PISANA, the server's first hit).
- **House numbers ride the search text** (no dedicated query param exists): `/location/?search=via Zara 3` ranks the exact `serviceType:"StreetNumber", civic:"3"` feature first with the top score (measured). But the client's anchor-nearest pick would bury it (the destination is always anchored) — so `_normalize_feature` passes `civic`/`serviceType` through and [orchestrator._pick_feature](../src/snap4city_mobility_mcp/orchestrator.py) narrows to the civic-exact hit when the user's text carries a house number, falling back to street-shaped labels (a name-only POI like "LAURA" must not win "via Laura 11") and only then to anchor-nearest.
- **Pure-noise input gives HTTP 500**, not an empty FeatureCollection. Callers must tolerate 5xx, and an empty / `[]` result is not a clean "no match" signal.
- **Backend is non-deterministic over time**: the same string can return all-foreign one minute and the correct Tuscan hit the next.

---

## §2. Referente remote MCP server: tool signatures (probe 2026-05-28)

Source: `GET http://192.168.1.117:8000/apps.json` → `Client(cfg)` → `list_tools()`; [mcp_tools._build_config](../src/snap4city_mobility_mcp/mcp_tools.py) narrows to the `native` server and rewrites the intranet IP to `DASHBOARD_URL`. Server scope: `snap4agentic_advisor_native` only (25 tools exposed).

> **This is a 2026-05-28 snapshot.** Re-probe on the JupyterHub before relying on it (one-liner in README §5), in case the native server version was bumped.

Names appear **without server prefix** under a single-server config (as we use): `coordinates_to_address`, not `snap4agentic_advisor_native_coordinates_to_address`. FastMCP only prefixes when merging multiple servers.

### Tools the advisor drives (remote)

| Tool | Required input | Notable optional | Purpose |
|---|---|---|---|
| `coordinates_to_address` | `latitude` + `longitude` | — | Reverse geocode; labels a GPS-defaulted origin for the reply |
| `service_search_near_gps_position` | `latitude` + `longitude` | `categories`, `maxdistance` (km), `maxresults` | Nearest-category POIs: car parks + "farmacia più vicina" destinations |
| `service_info_dev` | `serviceUri` | `fromTime` | Latest realtime free-spaces for a car park |

Forward geocoding (`address_search_location`) and routing (`route`, all modes) come from the **local** MCP server instead (§1, §3). The probed tools accept an optional `authentication` (Bearer); the probe surfaced no token requirement, so the advisor omits it (public km4city backend).

---

## §3. Why forward geocoding is served locally

**`address_search_location` (remote) is server-side broken** — bare-probe comparison, same query `via zara 3`: the public ServiceMap returns `VIA ZARA, FIRENZE` (score 12.64) first, while the remote MCP tool returns **zero Tuscan hits** (top hits Antwerpen / Greece, score 3–7) **and does not sort by score**. The schema exposes no sort/bbox/region parameter, and raising `maxresults` to 5000 does not surface the Tuscan hit — it is not in the result set at all. Hence the local geocode tool. SuperServiceMap, the federated backend, has the same ranking failure ("via zara firenze" ranks a Maastricht bus stop first). The client can switch back with one env var (`S4C_SERVICEMAP_BASE`) if the ranking is fixed.
