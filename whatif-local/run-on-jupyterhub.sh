#!/usr/bin/env bash
# Run the whatif-router natively on JupyterHub (no Docker) so mcp_server can reach it at
# http://localhost:8080 — avoids the Cloudflare quick-tunnel being blocked from JupyterHub egress.
#
# Prereq: upload the prebuilt war to whatif-local/whatif-router.war
#   (locally it is whatif-local/whatif-router-src/target/whatif-router-1.0-SNAPSHOT.war,
#    ~16MB, self-contained — carries the PT-singleton + warmup fix and all GraphHopper deps).
#   Drag it into the Jupyter file browser, renamed to whatif-router.war.
#
# Usage (from the repo, s4c conda env active):
#   bash whatif-local/run-on-jupyterhub.sh [start|run|stop|status|logs]
#
#   start   (default) Full setup (Java 8, Tomcat 9, OSM+GTFS data, deploy war), then start Tomcat
#           as a DETACHED daemon (setsid, own session): closing this terminal does NOT touch the
#           JVM, so bus_route keeps working and the graph-cache cannot be corrupted by a closed
#           tab. Waits for 'PtWarmupListener: PT router ready.' in the log (first boot builds the
#           graph-cache, minutes) — but the wait is only cosmetic, the daemon is already up.
#   run     Same setup, Tomcat in the FOREGROUND (debug). Stop it with Ctrl-C ONLY — closing the
#           terminal hard-kills the JVM before MapDB's clean-shutdown flag is written -> next
#           boot fails with 'Wrong index checksum' and rebuilds the graph (minutes).
#   stop    Graceful shutdown: catalina.sh stop, waits up to 60s for the JVM to exit. This is the
#           path that flushes the GTFS store and writes MapDB's clean-shutdown flag. Never
#           kill -9 the JVM.
#   status  pid liveness + HTTP probe of http://localhost:8080/whatif-router/.
#   logs    tail -f Tomcat's catalina.out.
#
# Then point mcp_server at it (second terminal):
#   export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
#   python -m snap4city_mobility_mcp.mcp_server
#
# Env knobs:
#   REBUILD_GRAPH=1   wipe data/graph-cache/ before boot. Use this to recover from a
#                     'Wrong index checksum, store was not closed properly' error, which means a
#                     previous JVM was hard-killed (kill -9 / OOM) before the PT singleton could
#                     write MapDB's clean-shutdown flag. Rebuild takes minutes.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"          # whatif-local/
cd "$HERE"
DATA="$HERE/data"
WAR="$HERE/whatif-router.war"
TOMCAT_VER="9.0.119"
TOMCAT_DIR="$HERE/apache-tomcat-$TOMCAT_VER"
CATALINA_OUT="$TOMCAT_DIR/logs/catalina.out"
export CATALINA_PID="$TOMCAT_DIR/tomcat.pid"   # catalina.sh start writes it; stop waits on it

CMD="${1:-start}"

tomcat_running() {
  [ -f "$CATALINA_PID" ] && kill -0 "$(cat "$CATALINA_PID")" 2>/dev/null
}

case "$CMD" in
  stop)
    if ! tomcat_running; then
      rm -f "$CATALINA_PID"
      echo "not running (no live pid at $CATALINA_PID)."
      echo "if an old instance still holds :8080, SIGTERM it (graceful, runs the shutdown hook):"
      echo "  pkill -f 'catalina.base=$TOMCAT_DIR'"
      exit 0
    fi
    echo "== stopping Tomcat gracefully (waits up to 60s; flushes GTFS store + MapDB clean flag) =="
    # no -force: force = kill -9, which is exactly what corrupts the graph-cache.
    # '|| true': on timeout catalina.sh exits non-zero — we report it ourselves below.
    "$TOMCAT_DIR/bin/catalina.sh" stop 60 || true
    if tomcat_running; then
      echo "!! still running after 60s (graph build in progress?). Retry later; do NOT kill -9." >&2
      exit 1
    fi
    echo "== stopped cleanly — next start loads the graph-cache in seconds =="
    exit 0
    ;;
  status)
    if tomcat_running; then
      echo "running (pid $(cat "$CATALINA_PID"))"
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:8080/whatif-router/" || true)"
      echo "http probe on /whatif-router/: $code (200 = deployed; PT readiness: see 'logs')"
    else
      echo "not running"
    fi
    exit 0
    ;;
  logs)
    [ -f "$CATALINA_OUT" ] || { echo "no log yet at $CATALINA_OUT — start Tomcat first." >&2; exit 1; }
    exec tail -f "$CATALINA_OUT"
    ;;
  start|run) ;;                                 # fall through to setup below
  *)
    echo "usage: $0 [start|run|stop|status|logs]" >&2
    exit 1
    ;;
esac

echo "== 0. checks =="
if [ ! -f "$WAR" ]; then
  echo "!! missing $WAR" >&2
  echo "   Upload the prebuilt war here first (rename to whatif-router.war). See header." >&2
  exit 1
fi
if tomcat_running; then
  echo "!! Tomcat already running (pid $(cat "$CATALINA_PID")) — use '$0 stop' first." >&2
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
if [ "$CMD" = "run" ]; then
  echo "== 5. starting Tomcat (FOREGROUND, debug). First boot builds the graph-cache (minutes),"
  echo "      then 'PtWarmupListener: PT router ready.'. Stop with Ctrl-C ONLY — closing the"
  echo "      terminal hard-kills the JVM and corrupts the graph-cache. =="
  echo
  exec "$TOMCAT_DIR/bin/catalina.sh" run
fi

# start (default): detached daemon. setsid puts the JVM in its own session, so closing this
# terminal (pty SIGHUP / process-group kill) can never reach it — the only ways it stops are
# '$0 stop' (graceful -> contextDestroyed -> MapDB clean flag) or an OOM/explicit kill.
echo "== 5. starting Tomcat (DETACHED daemon — safe to close this terminal) =="
mkdir -p "$TOMCAT_DIR/logs"
touch "$CATALINA_OUT"
OFFSET="$(stat -c%s "$CATALINA_OUT")"          # only scan log lines from this boot onward
setsid "$TOMCAT_DIR/bin/catalina.sh" start < /dev/null
echo "      pid file: $CATALINA_PID | log: $CATALINA_OUT"
echo "      waiting for 'PT router ready.' (first boot builds the graph-cache, minutes;"
echo "      Ctrl-C or closing this terminal only stops the wait, NOT the daemon)..."
for i in $(seq 1 900); do
  boot_log="$(tail -c +"$((OFFSET + 1))" "$CATALINA_OUT")"
  if printf '%s' "$boot_log" | grep -q "PT router ready."; then
    echo "== PT router ready — endpoint: http://localhost:8080/whatif-router/route =="
    echo "   manage with: $0 status | logs | stop"
    exit 0
  fi
  if printf '%s' "$boot_log" | grep -q "Wrong index checksum"; then
    echo "!! graph-cache is corrupted (previous JVM was hard-killed)." >&2
    echo "   Recover: $0 stop && REBUILD_GRAPH=1 $0 start   (rebuild takes minutes)" >&2
    exit 1
  fi
  if ! tomcat_running; then
    echo "!! Tomcat exited during startup — inspect: $0 logs" >&2
    exit 1
  fi
  sleep 2
done
echo "!! timed out after 30min still waiting for 'PT router ready.' — daemon is still running;" >&2
echo "   watch '$0 logs' (graph build is silent, check data/graph-cache/ file growth)." >&2
exit 1
