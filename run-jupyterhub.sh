#!/usr/bin/env bash
# One-shot JupyterHub launcher — starts every long-running process the advisor needs from a SINGLE
# terminal, instead of opening three:
#   1. whatif-router  (Tomcat :8080, background)  — only when a local war is present (real PT lines)
#   2. local geocode MCP server (:8020, background) — src/snap4city_mobility_mcp/mcp_server.py
#   3. advisor bridge uvicorn (:8010, foreground) — api.py
#
# Ctrl-C stops ALL of them cleanly. Tomcat is stopped via 'catalina.sh stop', which writes MapDB's
# clean-shutdown flag and so prevents the 'Wrong index checksum, store was not closed properly'
# corruption on the next boot (see whatif-local/patches/README.md).
#
# Run from a JupyterHub terminal with the s4c conda env active:
#   bash run-jupyterhub.sh
#
# Env knobs:
#   USE_WHATIF=auto|1|0   auto (default) = start the local whatif-router iff whatif-local/whatif-router.war
#                         exists; 1 = force on; 0 = skip it (use the online default router — do this
#                         once referente loads the GTFS + perf patch on the online instance).
#   REBUILD_GRAPH=1       forwarded to run-on-jupyterhub.sh — wipe data/graph-cache before boot to
#                         recover from the checksum-corruption error.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
LOGDIR="$HERE/.run-logs"; mkdir -p "$LOGDIR"
WAR="$HERE/whatif-local/whatif-router.war"
TOMCAT_DIR="$HERE/whatif-local/apache-tomcat-9.0.119"

USE_WHATIF="${USE_WHATIF:-auto}"
if [ "$USE_WHATIF" = "auto" ]; then
  if [ -f "$WAR" ]; then USE_WHATIF=1; else USE_WHATIF=0; fi
fi

MCP_PID=""
UVICORN_PID=""
cleanup() {
  echo
  echo "== stopping all =="
  [ -n "$UVICORN_PID" ] && kill "$UVICORN_PID" 2>/dev/null
  [ -n "$MCP_PID" ]     && kill "$MCP_PID" 2>/dev/null
  # clean Tomcat shutdown -> writes MapDB clean flag -> no checksum corruption next boot
  if [ "$USE_WHATIF" = "1" ] && [ -x "$TOMCAT_DIR/bin/catalina.sh" ]; then
    "$TOMCAT_DIR/bin/catalina.sh" stop 20 -force >/dev/null 2>&1
  fi
}
trap cleanup EXIT INT TERM

if [ "$USE_WHATIF" = "1" ]; then
  echo "== [1/3] whatif-router (Tomcat :8080, background) =="
  WHATIF_DAEMON=1 REBUILD_GRAPH="${REBUILD_GRAPH:-0}" bash whatif-local/run-on-jupyterhub.sh
  export S4C_WHATIF_ROUTER_URL="http://localhost:8080/whatif-router/route"
  echo "   note: first boot builds the graph-cache (minutes). Wait for 'PT router ready.' before"
  echo "         testing public transport:  tail -f $TOMCAT_DIR/logs/catalina.out"
else
  echo "== [1/3] whatif-router SKIPPED (no local war / USE_WHATIF=0) — bus_route uses the online default =="
fi

echo "== [2/3] local geocode MCP server (:8020, background) — log: $LOGDIR/mcp_server.log =="
python -m snap4city_mobility_mcp.mcp_server >"$LOGDIR/mcp_server.log" 2>&1 &
MCP_PID=$!

echo "== [3/3] advisor bridge uvicorn (:8010, foreground) — Ctrl-C stops everything =="
uvicorn api:app --host 0.0.0.0 --port 8010 &
UVICORN_PID=$!
wait "$UVICORN_PID"
