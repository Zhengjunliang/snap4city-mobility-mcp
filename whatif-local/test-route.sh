#!/usr/bin/env bash
# Validate that the local whatif-router returns REAL public transport (not a degenerate road
# line) once Tuscany GTFS is loaded. Run after `docker compose up tomcat` is ready.
#   bash test-route.sh
# Writes the full bus response to test-output.json (send this to referente).
#
# OD: Piazza del Duomo -> Campo di Marte (both inside the Florence AT urban network).
# Same params our client's bus_route tool uses (vehicle=bus, waypoints lng,lat;lng,lat).
set -euo pipefail
cd "$(dirname "$0")"

BASE="${WHATIF_BASE:-http://localhost:8080/whatif-router/route}"
WAYPOINTS="11.2558,43.7731;11.2200,43.8000"   # Duomo ; Campo di Marte  (lng,lat order)
START="2026-07-06T08:00:00"

echo "==> BUS request: $BASE"
curl -fsS -G "$BASE" \
  --data-urlencode "vehicle=bus" \
  --data-urlencode "waypoints=$WAYPOINTS" \
  --data-urlencode "weighting=fastest" \
  --data-urlencode "wkt=true" \
  --data-urlencode "startDatetime=$START" \
  -o test-output.json
echo "    saved -> test-output.json"

echo "==> FOOT request (control, to compare geometry) ..."
curl -fsS -G "$BASE" \
  --data-urlencode "vehicle=foot" \
  --data-urlencode "waypoints=$WAYPOINTS" \
  --data-urlencode "weighting=fastest" \
  --data-urlencode "wkt=true" \
  -o test-output-foot.json
echo "    saved -> test-output-foot.json"

# jq is optional; if present, summarise. Otherwise just point at the files.
if command -v jq >/dev/null 2>&1; then
  echo
  echo "===== BUS paths[0] summary ====="
  jq '{
        has_path: (.paths|length>0),
        distance_m: .paths[0].distance,
        time_ms: .paths[0].time,
        instruction_count: (.paths[0].instructions|length),
        # PT legs carry trip/agency/stop fields; a pure road route has only street turns.
        sample_instructions: (.paths[0].instructions[0:5])
      }' test-output.json
  echo
  echo "===== FOOT paths[0] distance (for comparison) ====="
  jq '{distance_m: .paths[0].distance, time_ms: .paths[0].time}' test-output-foot.json
  echo
  echo "PASS CRITERIA: bus instructions contain transit legs (trip/agency/line/stop fields)"
  echo "and the bus geometry/distance differs from foot. If bus == foot or instructions are"
  echo "only street turns, GTFS did not load -> check container logs + fetch-data.sh output."
else
  echo
  echo "(jq not installed) Inspect test-output.json manually: paths[0].instructions should"
  echo "contain transit legs (trip/agency/stop), not just street turn-by-turn."
fi
