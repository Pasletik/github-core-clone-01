#!/usr/bin/env bash
# Local grading run: kit env server + this forge + kit scorer.
# Mirrors the trial: the env drives the 120 conformance checks and the four
# benchmark workloads, writes <state>/export/{state,perf}.json, and the
# scorer turns that into feature + agent + perf.
#
# Usage: tests/grade-local.sh [run-name]
#   The run lands in local-run/<run-name>/ (state/, scores.json, logs).
#   Re-run with a new name to keep an old run's export around.

set -u
cd "$(dirname "$0")/.."
KIT="$HOME/workspace/tools/makefun/github-core-clone-01-kit"
RUN="${1:-run-1}"
BASE="$PWD/local-run/$RUN"
STATE="$BASE/state"
mkdir -p "$STATE" "$BASE"

TOKEN="${MAKEFUN_SHOP_TOKEN:-local-test-token}"
ENV_PORT="${MAKEFUN_ENV_PORT:-18780}"
ENV_URL="http://127.0.0.1:$ENV_PORT"

echo "[grade] run dir: $BASE"
echo "[grade] starting env server on $ENV_PORT ..."
MAKEFUN_STATE_DIR="$STATE" MAKEFUN_ENV_HOST=127.0.0.1 MAKEFUN_ENV_PORT="$ENV_PORT" \
  MAKEFUN_SHOP_TOKEN="$TOKEN" \
  node "$KIT/program/env/server.mjs" > "$BASE/env.log" 2>&1 &
ENV_PID=$!
echo "[grade] env pid $ENV_PID"

# Wait for the env's ready file (it writes <state>/ready once bound).
for _ in $(seq 1 60); do
  [ -f "$STATE/ready" ] && break
  sleep 0.5
done
if [ ! -f "$STATE/ready" ]; then
  echo "[grade] env did not become ready; see $BASE/env.log"
  tail -20 "$BASE/env.log"
  kill $ENV_PID 2>/dev/null
  exit 1
fi
cat "$STATE/ready"
echo "[grade] env ready; starting forge ..."
MAKEFUN_ENV_URL="$ENV_URL" MAKEFUN_SHOP_TOKEN="$TOKEN" \
  MAKEFUN_TRIAL_NOW=1735689600 \
  python3 main.py > "$BASE/forge.log" 2>&1 &
FORGE_PID=$!
echo "[grade] forge pid $FORGE_PID"

# The forge exits 0 once the env answers /done = true (export written).
wait $FORGE_PID
FORGE_RC=$?
echo "[grade] forge exited $FORGE_RC"

# Give the env a moment, then stop it (SIGTERM rewrites the export only
# if it is not worse, and exits 0).
sleep 2
kill -TERM $ENV_PID 2>/dev/null
wait $ENV_PID 2>/dev/null
echo "[grade] env stopped"

ls -l "$STATE/export/" 2>/dev/null || echo "[grade] no export dir"
if [ -f "$STATE/export/state.json" ]; then
  echo "[grade] scoring ..."
  MAKEFUN_EXPORT_DIR="$STATE/export" MAKEFUN_SCORES_DIR="$BASE" \
    node "$KIT/program/scorer/score.mjs"
  echo "[grade] scores: $BASE/scores.json"
  cat "$BASE/scores.json" 2>/dev/null
fi
exit $FORGE_RC
