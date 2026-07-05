#!/usr/bin/env bash
# Run the whatif-router natively on JupyterHub (no Docker) so mcp_server can reach it at
# http://localhost:8080 — avoids the Cloudflare quick-tunnel being blocked from JupyterHub egress.
#
# Prereq: upload the prebuilt war to whatif-local/whatif-router.war
#   (locally it is whatif-local/whatif-router-src/target/whatif-router-1.0-SNAPSHOT.war,
#    ~16MB, self-contained — carries the PT-singleton + warmup fix and all GraphHopper deps).
#   Drag it into the Jupyter file browser, renamed to whatif-router.war.
#
# Then, from the repo (s4c conda env active):
#   bash whatif-local/run-on-jupyterhub.sh
#
# It installs Java 8, downloads Tomcat 9, fetches the OSM+GTFS data, deploys the war, and starts
# Tomcat in the foreground. First boot builds the graph-cache (minutes) + warms the PT router;
# leave it running in this terminal. In a second terminal point mcp_server at it:
#   export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
#   python -m snap4city_mobility_mcp.mcp_server
#
# Env knobs:
#   WHATIF_DAEMON=1   start Tomcat in the background (catalina.sh start) and return, instead of
#                     foreground. Used by the repo-root run-jupyterhub.sh one-shot launcher.
#   REBUILD_GRAPH=1   wipe data/graph-cache/ before boot. Use this to recover from a
#                     'Wrong index checksum, store was not closed properly' error, which means a
#                     previous run was hard-killed (kill -9 / OOM / terminal closed) before the PT
#                     singleton could write MapDB's clean-shutdown flag. Rebuild takes minutes.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"          # whatif-local/
cd "$HERE"
DATA="$HERE/data"
WAR="$HERE/whatif-router.war"
TOMCAT_VER="9.0.119"
TOMCAT_DIR="$HERE/apache-tomcat-$TOMCAT_VER"

echo "== 0. checks =="
if [ ! -f "$WAR" ]; then
  echo "!! missing $WAR" >&2
  echo "   Upload the prebuilt war here first (rename to whatif-router.war). See header." >&2
  exit 1
fi

# --- 1. toolchain: Java 8 (+ curl/unzip if absent), installed into the active conda env ---
need=()
command -v java  >/dev/null 2>&1 || need+=("openjdk=8")
command -v curl  >/dev/null 2>&1 || need+=("curl")
command -v unzip >/dev/null 2>&1 || need+=("unzip")
if [ "${#need[@]}" -gt 0 ]; then
  echo "== 1. conda install: ${need[*]} =="
  conda install -y -c conda-forge "${need[@]}"
else
  echo "== 1. toolchain present =="
fi
java -version 2>&1 | head -1

# --- 2. Tomcat 9 (download once) ---
if [ ! -d "$TOMCAT_DIR" ]; then
  echo "== 2. downloading Tomcat $TOMCAT_VER =="
  TARBALL="apache-tomcat-$TOMCAT_VER.tar.gz"
  # archive.apache.org keeps every point release; dlcdn only keeps latest.
  curl -fL -o "$HERE/$TARBALL" \
    "https://archive.apache.org/dist/tomcat/tomcat-9/v$TOMCAT_VER/bin/$TARBALL"
  tar -xzf "$HERE/$TARBALL" -C "$HERE"
  rm -f "$HERE/$TARBALL"
else
  echo "== 2. Tomcat present =="
fi

# --- 3. data (OSM PBF + GTFS) — sources per referente / dati.toscana.it (rt-oraritb) ---
if [ "${REBUILD_GRAPH:-0}" = "1" ] && [ -d "$DATA/graph-cache" ]; then
  echo "== 3a. REBUILD_GRAPH=1 -> wiping data/graph-cache (recover from checksum corruption) =="
  rm -rf "$DATA/graph-cache"/*
fi
mkdir -p "$DATA" "$DATA/graph-cache" "$DATA/typical_ttt"
if [ ! -f "$DATA/centro-latest.osm.pbf" ] || [ ! -f "$DATA/at.gtfs" ] || [ ! -f "$DATA/gest.gtfs" ]; then
  echo "== 3. fetching data (PBF ~450MB + GTFS) =="
  curl -fL -o "$DATA/centro-latest.osm.pbf" "https://download.geofabrik.de/europe/italy/centro-latest.osm.pbf"
  curl -fL -o "$DATA/at.gtfs"   "https://regionetoscana.smartregion.toscana.it/mobility/artifacts/gtfs"        # Autolinee Toscane
  curl -fL -o "$DATA/gest.gtfs" "https://dati.toscana.it/dataset/8bb8f8fe-fe7d-41d0-90dc-49f2456180d1/resource/1f62d551-65f4-49f8-9a99-e19b02077be3/download/gest.gtfs"  # GEST tram
  # sanity-check each GTFS zip carries the mandatory tables (a missing table -> partial transit graph)
  for f in "$DATA/at.gtfs" "$DATA/gest.gtfs"; do
    unzip -l "$f" >/dev/null 2>&1 || { echo "   !! $f not a valid zip (URL returned an error page?)" >&2; continue; }
    for tbl in stops.txt routes.txt trips.txt stop_times.txt; do
      unzip -l "$f" | grep -qi "$tbl" && echo "   ok   $f $tbl" || echo "   MISS $f $tbl" >&2
    done
  done
else
  echo "== 3. data present =="
fi

# --- 4. deploy war ---
echo "== 4. deploy war =="
cp -f "$WAR" "$TOMCAT_DIR/webapps/whatif-router.war"

# --- 5. env for the war (absolute paths, read by the war at startup) + heap ---
export GH_MAP_PBF="$DATA/centro-latest.osm.pbf"
export GH_LOCATION_PFX="$DATA/graph-cache"
export GH_TYPICAL_TTT_PATH="$DATA/typical_ttt"
export GH_GTFS_FILES="$DATA/at.gtfs,$DATA/gest.gtfs"
# 55GB RAM available -> give the graph build/load plenty; -Xmx6g was the docker floor.
export CATALINA_OPTS="-Xmx12g -Xms2g"

echo "      endpoint: http://localhost:8080/whatif-router/route"
if [ "${WHATIF_DAEMON:-0}" = "1" ]; then
  # Background mode (used by run-jupyterhub.sh): start as a daemon and return. Logs go to
  # $TOMCAT_DIR/logs/catalina.out. The caller is responsible for a clean 'catalina.sh stop'
  # (which writes MapDB's clean-shutdown flag and prevents the checksum corruption on next boot).
  echo "== 5. starting Tomcat (background daemon). First boot builds the graph-cache (minutes),"
  echo "      then 'PtWarmupListener: PT router ready.' in logs/catalina.out. =="
  "$TOMCAT_DIR/bin/catalina.sh" start
else
  echo "== 5. starting Tomcat (foreground). First boot builds the graph-cache (minutes),"
  echo "      then 'PtWarmupListener: PT router ready.' — leave this terminal running. =="
  echo
  exec "$TOMCAT_DIR/bin/catalina.sh" run
fi
