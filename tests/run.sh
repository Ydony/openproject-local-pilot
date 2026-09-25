#!/usr/bin/env bash
#
# tests/run.sh -- run the full suite with unittest discovery.
# No Docker daemon, network, OpenProject or GitHub needed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# The code needs Python 3.11+. Prefer python3 (CI/WSL); on Windows dev
# machines fall back to the newest interpreter available (py launcher,
# then plain python) and refuse anything older.
pick_python() {
  local c
  for c in python3 "py -3" python; do
    # shellcheck disable=SC2086
    if $c -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}
PYTHON="$(pick_python)" || {
  echo "tests/run.sh: need Python 3.11+ (tried python3, py -3, python)" >&2
  exit 1
}
cd "$ROOT" || exit 1
# Relative start dir: test modules import as top level, no package needed.
# shellcheck disable=SC2086
exec $PYTHON -m unittest discover -s tests -v "$@"
