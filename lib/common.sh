#!/usr/bin/env bash
#
# lib/common.sh -- shared helpers for the opl-* operator toolkit.
#
# Sourced by every bin/opl-* script. Safe to source twice.
#
# Invariants (enforced by tests/test_static.py, do not weaken):
#   * Secret values are never printed. Use print_safe_env for diagnostics.
#   * Every wait goes through wait_until() so it stays bounded.
#   * bin/ scripts must not call sleep directly; only this file may.
#   * Loopback checks go through assert_loopback_override(), which ignores
#     comment lines so a comment can never trip (or bypass) the check.

if [ -n "${__OPL_COMMON_SOURCED:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
__OPL_COMMON_SOURCED=1

set -euo pipefail

if [ -z "${OPL_ROOT:-}" ]; then
  OPL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

: "${OPL_RUNTIME_DIR:=${OPL_ROOT}/.opl-runtime}"
: "${OPL_UPSTREAM_URL:=https://github.com/opf/openproject-docker-compose.git}"
: "${OPL_UPSTREAM_REF:=stable/17}"
: "${OPL_PROJECT:=opl-pilot}"
: "${OPL_PORT:=8080}"
: "${OPL_LOOPBACK:=127.0.0.1}"
: "${OPL_BACKUP_ROOT:=${OPL_ROOT}/backups}"
: "${OPL_SEED_TIMEOUT_SECS:=1200}"
: "${OPL_HEALTH_TIMEOUT_SECS:=600}"
: "${OPL_DB_TIMEOUT_SECS:=300}"
: "${OPL_DB_USER:=postgres}"
: "${OPL_DB_NAME:=openproject}"
: "${OPL_BACKUP_KEEP:=5}"

opl_log() { printf '[opl] %s\n' "$*"; }
opl_warn() { printf '[opl][warn] %s\n' "$*" >&2; }
opl_die() { printf '[opl][error] %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || opl_die "required command '$1' not found; see docs/RUNBOOK.md"
}

runtime_base_file() { printf '%s/docker-compose.yml' "$OPL_RUNTIME_DIR"; }
runtime_override_file() { printf '%s/docker-compose.override.yml' "$OPL_RUNTIME_DIR"; }
runtime_env_file() { printf '%s/.env' "$OPL_RUNTIME_DIR"; }
runtime_local_file() { printf '%s/docker-compose.local.yml' "$OPL_RUNTIME_DIR"; }

require_runtime() {
  [ -d "$OPL_RUNTIME_DIR" ] || opl_die "runtime dir missing: $OPL_RUNTIME_DIR (run bin/opl-setup first)"
  [ -f "$(runtime_base_file)" ] || opl_die "upstream bundle missing in $OPL_RUNTIME_DIR (run bin/opl-setup first)"
  [ -f "$(runtime_override_file)" ] || opl_die "override missing in $OPL_RUNTIME_DIR (run bin/opl-setup first)"
  [ -f "$(runtime_env_file)" ] || opl_die "runtime .env missing in $OPL_RUNTIME_DIR (run bin/opl-setup first)"
}

# compose -- thin wrapper pinning the pilot project name and the compose
# files (pristine upstream base + generated loopback override). An optional
# owner tuning file (lead-managed, see T0.16) joins as the third file.
compose() {
  if [ -f "$(runtime_local_file)" ]; then
    docker compose --project-name "$OPL_PROJECT" \
      --env-file "$(runtime_env_file)" \
      -f "$(runtime_base_file)" -f "$(runtime_override_file)" \
      -f "$(runtime_local_file)" "$@"
  else
    docker compose --project-name "$OPL_PROJECT" \
      --env-file "$(runtime_env_file)" \
      -f "$(runtime_base_file)" -f "$(runtime_override_file)" "$@"
  fi
}

# assert_tuning_only FILE -- die if an optional local override publishes
# ports or changes network mode (non-comment lines). It may only tune.
# Missing file is fine. Callers: setup, start, restore-test (dcompose).
assert_tuning_only() {
  local file="$1" code
  [ -f "$file" ] || return 0
  code="$(grep -v '^[[:space:]]*#' "$file" || true)"
  if printf '%s\n' "$code" | grep -qE 'ports:|network_mode:'; then
    opl_die "$file must only tune; refusing publish/network lines"
  fi
}

# wait_until TIMEOUT_SECS INTERVAL_SECS DESCRIPTION -- CMD [ARGS...]
# Runs CMD until it succeeds or TIMEOUT_SECS elapses. Always bounded.
wait_until() {
  local timeout_secs="$1" interval_secs="$2" description="$3"
  shift 3
  if [ "${1:-}" = "--" ]; then shift; fi
  local start now
  start="$SECONDS"
  while true; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    now="$SECONDS"
    if [ $((now - start)) -ge "$timeout_secs" ]; then
      opl_warn "timed out after ${timeout_secs}s waiting for: $description"
      return 1
    fi
    sleep "$interval_secs"
  done
}

# assert_loopback_override FILE -- die unless every effective (non-comment)
# published-port entry binds the loopback address. Comments are stripped
# first, so template documentation can never trip or bypass this check. A
# bare "8080:80" entry or a public wildcard entry counts as non-loopback
# (the prefix test rejects both, so no literal wildcard appears here), and
# a file with no entries at all fails closed.
# Callers: opl-setup (after install), opl-start (refuse).
assert_loopback_override() {
  local file="$1" line stripped entry found=0
  while IFS= read -r line || [ -n "$line" ]; do
    stripped="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//')"
    case "$stripped" in
      '' | \#*) continue ;;
    esac
    case "$stripped" in
      -*)
        entry="$(printf '%s' "$stripped" | sed -e 's/^-[[:space:]]*//' -e 's/^["'\'']//')"
        case "$entry" in
          127.0.0.1:*)
            found=$((found + 1))
            ;;
          *)
            opl_die "$file has a non-loopback publish entry: $line; refusing"
            ;;
        esac
        ;;
    esac
  done <"$file"
  if [ "$found" = "0" ]; then
    opl_die "$file publishes no loopback ports; refusing"
  fi
}

# assert_loopback_env FILE -- die unless the runtime env publishes the pilot
# on loopback too: upstream publishes ${PORT:-8080}:80 and our override only
# ADDS an entry, so a public PORT would still expose the pilot. PORT must
# look like 127.0.0.1:<port>.
assert_loopback_env() {
  local file="$1" port
  port="$(env_get PORT "$file")"
  case "$port" in
    127.0.0.1:*)
      case "${port##*:}" in
        '' | *[!0-9]*) opl_die "$file has a bad PORT value; refusing" ;;
      esac
      ;;
    *) opl_die "$file does not bind PORT to loopback; refusing" ;;
  esac
}

# env_get KEY FILE -- print the value of the last non-comment KEY= line.
# Prints nothing (exit 0) when absent. Surrounding quotes are stripped.
env_get() {
  local key="$1" file="$2" line
  line="$(grep -E "^[[:space:]]*$key=" "$file" 2>/dev/null | grep -v '^[[:space:]]*#' | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# env_ensure KEY VALUE FILE -- append KEY=VALUE only when KEY is absent.
# Existing values are never touched. Prints the resulting value. If the
# file lacks a trailing newline (common after hand-editing on Windows),
# one is added first so the new line never glues onto the last one.
env_ensure() {
  local key="$1" value="$2" file="$3" existing
  existing="$(env_get "$key" "$file")"
  if [ -n "$existing" ]; then
    printf '%s' "$existing"
    return 0
  fi
  if [ -s "$file" ] && [ -n "$(tail -c 1 "$file")" ]; then
    printf '\n' >>"$file"
  fi
  printf '%s=%s\n' "$key" "$value" >>"$file"
  printf '%s' "$value"
}

# urlencode -- percent-encode a string for use in DATABASE_URL.
# URL-safe characters pass through; python3 is preferred, sed fallback
# covers the common specials. Hex secrets from gen_secret need no encoding.
urlencode() {
  # The value (a password) travels over stdin, never as an argument: argv is
  # visible to every process on the machine. printf is a bash builtin, so it
  # spawns no process of its own.
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$1" | python3 -c 'import sys,urllib.parse; sys.stdout.write(urllib.parse.quote(sys.stdin.read(), safe=""))'
    return 0
  fi
  printf '%s' "$1" | sed -e 's/%/%25/g' -e 's/ /%20/g' -e 's/@/%40/g' \
    -e 's/:/%3A/g' -e 's#/#%2F#g' -e 's/?/%3F/g' -e 's/#/%23/g' -e 's/&/%26/g'
}

# redact_line -- replace KEY=secret values with KEY=*** for safe diagnostics.
redact_line() {
  sed -E -e 's/((DATABASE_URL|SECRET|PASSWORD|TOKEN|KEY_BASE)[A-Za-z_]*=).*/\1***/' <<<"$1"
}

# print_safe_env FILE -- print an env file with secret values redacted.
print_safe_env() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    redact_line "$line"
  done <"$1"
}

# refuse_active_project NAME -- die if NAME is the live pilot project.
# Restore and cleanup paths must call this before touching Docker.
refuse_active_project() {
  local name="${1:-}"
  if [ -z "$name" ]; then
    opl_die "refusing to act on an empty project name"
  fi
  if [ "$name" = "$OPL_PROJECT" ]; then
    opl_die "refusing: '$name' is the ACTIVE pilot project ($OPL_PROJECT)"
  fi
}

# project_container_ids [SERVICE] -- ids of this pilot's containers, found
# by the compose project label (never by bare name regex), optionally
# narrowed to one service within the project.
project_container_ids() {
  if [ -n "${1:-}" ]; then
    docker ps -aq --filter "label=com.docker.compose.project=${OPL_PROJECT}" \
      --filter "name=${OPL_PROJECT}[-_]$1[-_]" 2>/dev/null || true
  else
    docker ps -aq --filter "label=com.docker.compose.project=${OPL_PROJECT}" \
      2>/dev/null || true
  fi
}

# latest_exit SERVICE -- print the exit code of the newest container of a
# service in this compose project. Fails when there is none. The "|| true"
# guards the pipefail/SIGPIPE race when the producer outlives "head".
latest_exit() {
  local id
  id="$(project_container_ids "$1" 2>/dev/null | head -n 1 || true)"
  if [ -z "$id" ]; then
    return 1
  fi
  docker inspect -f '{{.State.ExitCode}}' "$id" 2>/dev/null
}

# container_running SERVICE -- true when the newest matching container runs.
container_running() {
  local id state
  id="$(project_container_ids "$1" 2>/dev/null | head -n 1 || true)"
  if [ -z "$id" ]; then
    return 1
  fi
  state="$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)"
  [ "$state" = "true" ]
}

# seeder_finished -- true once the seeder container has exited (any code).
seeder_finished() {
  local id state
  id="$(project_container_ids seeder 2>/dev/null | head -n 1 || true)"
  if [ -z "$id" ]; then
    return 1
  fi
  state="$(docker inspect -f '{{.State.Status}}' "$id" 2>/dev/null)"
  if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
    return 0
  fi
  return 1
}

health_url() {
  printf 'http://%s:%s/health_checks/default' "$OPL_LOOPBACK" "$OPL_PORT"
}

app_url() {
  printf 'http://%s:%s' "$OPL_LOOPBACK" "$OPL_PORT"
}
