#!/usr/bin/env bash
#
# tests/live_docker.sh -- [live] optional checks against a real Docker daemon.
#
# Clearly labelled LIVE: skipped unless RUN_LIVE_DOCKER=1 AND a daemon answers.
# Never touches a real deployment: it only renders "docker compose config"
# for a synthetic fixture runtime created by the offline tests. Static and
# synthetic suites (tests/test_static.py) run without this file.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/../lib/common.sh"

if [ "${RUN_LIVE_DOCKER:-}" != "1" ]; then
  echo "[live] SKIP: set RUN_LIVE_DOCKER=1 to run live Docker checks"
  exit 0
fi
need_cmd docker
if ! docker info >/dev/null 2>&1; then
  echo "[live] SKIP: no Docker daemon reachable"
  exit 0
fi

TMPD="$(mktemp -d)"
trap 'rc=$?; rm -rf "$TMPD"; exit $rc' EXIT
export OPL_RUNTIME_DIR="$TMPD/runtime"
export OPL_BACKUP_ROOT="$TMPD/backups"
export OPL_PROJECT="opl-live-check"
mkdir -p "$OPL_RUNTIME_DIR"
cp "$OPL_ROOT/tests/fixtures/upstream/docker-compose.yml" "$OPL_RUNTIME_DIR/"
sed -e "s/__OPL_LOOPBACK__/127.0.0.1/g" -e "s/__OPL_PORT__/8123/g" \
  "$OPL_ROOT/compose/docker-compose.override.yml" >"$OPL_RUNTIME_DIR/docker-compose.override.yml"
printf 'PORT=127.0.0.1:8123\n' >"$OPL_RUNTIME_DIR/.env"

echo "[live] rendering compose config for synthetic fixture runtime"
if compose config >/dev/null; then
  echo "[live] PASS: compose config renders"
else
  echo "[live] FAIL: compose config did not render" >&2
  exit 1
fi
