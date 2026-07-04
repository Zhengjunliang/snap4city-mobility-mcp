#!/usr/bin/env bash
# Download the data the whatif-router container needs into ./data/.
# Run from the whatif-local/ directory (Git-Bash on Windows, or any bash):
#   bash fetch-data.sh
#
# Produces (names match GH_GTFS_FILES in docker-compose.yml):
#   data/centro-latest.osm.pbf   OSM road network, Italy "Centro" (covers Tuscany) - you fetch, per referente
#   data/at.gtfs                 Autolinee Toscane GTFS (main Tuscan bus operator)
#   data/gest.gtfs               GEST GTFS (Florence tram)
# GTFS sources: dati.toscana.it/dataset/rt-oraritb (CC-BY, static GTFS).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data data/typical_ttt data/graph-cache

PBF_URL="https://download.geofabrik.de/europe/italy/centro-latest.osm.pbf"
AT_URL="https://regionetoscana.smartregion.toscana.it/mobility/artifacts/gtfs"
GEST_URL="https://dati.toscana.it/dataset/8bb8f8fe-fe7d-41d0-90dc-49f2456180d1/resource/1f62d551-65f4-49f8-9a99-e19b02077be3/download/gest.gtfs"

echo "==> OSM PBF (Italy Centro, ~450MB) ..."
curl -fL -o data/centro-latest.osm.pbf "$PBF_URL"

echo "==> Autolinee Toscane GTFS -> data/at.gtfs ..."
curl -fL -o data/at.gtfs "$AT_URL"

echo "==> GEST tram GTFS -> data/gest.gtfs ..."
curl -fL -o data/gest.gtfs "$GEST_URL"

# GTFS files are zip archives; sanity-check each carries the mandatory tables. A missing table
# means the container will build an empty/partial transit graph and PT routing will degrade.
echo "==> Verifying GTFS archives ..."
for f in data/at.gtfs data/gest.gtfs; do
  echo "--- $f"
  if ! unzip -l "$f" >/dev/null 2>&1; then
    echo "    !! $f is not a valid zip (download may have returned an error page). Re-check the URL." >&2
    continue
  fi
  for tbl in stops.txt routes.txt trips.txt stop_times.txt; do
    if unzip -l "$f" | grep -qi "$tbl"; then echo "    ok   $tbl"; else echo "    MISS $tbl" >&2; fi
  done
  # calendar.txt OR calendar_dates.txt satisfies GTFS service definition.
  if unzip -l "$f" | grep -qiE "calendar(_dates)?\.txt"; then echo "    ok   calendar(_dates).txt"; else echo "    MISS calendar*.txt" >&2; fi
done

echo "==> Done. data/ ready:"
ls -lh data/
