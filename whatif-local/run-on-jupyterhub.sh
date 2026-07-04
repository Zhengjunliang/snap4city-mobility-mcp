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

# --- 3. data (OSM PBF + GTFS) ---
if [ ! -f "$DATA/centro-latest.osm.pbf" ] || [ ! -f "$DATA/at.gtfs" ]; then
  echo "== 3. fetching data (PBF ~450MB + GTFS) =="
  bash "$HERE/fetch-data.sh"
else
  echo "== 3. data present =="
fi
mkdir -p "$DATA/graph-cache" "$DATA/typical_ttt"

# --- 4. deploy war ---
echo "== 4. deploy war =="
cp -f "$WAR" "$TOMCAT_DIR/webapps/whatif-router.war"

# --- 5. env for the war (absolute paths; matches docker-compose.yml) + heap ---
export GH_MAP_PBF="$DATA/centro-latest.osm.pbf"
export GH_LOCATION_PFX="$DATA/graph-cache"
export GH_TYPICAL_TTT_PATH="$DATA/typical_ttt"
export GH_GTFS_FILES="$DATA/at.gtfs,$DATA/gest.gtfs"
# 55GB RAM available -> give the graph build/load plenty; -Xmx6g was the docker floor.
export CATALINA_OPTS="-Xmx12g -Xms2g"

echo "== 5. starting Tomcat (foreground). First boot builds the graph-cache (minutes),"
echo "      then 'PtWarmupListener: PT router ready.' — leave this terminal running. =="
echo "      endpoint: http://localhost:8080/whatif-router/route"
echo
exec "$TOMCAT_DIR/bin/catalina.sh" run
