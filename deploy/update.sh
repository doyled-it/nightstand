#!/usr/bin/env bash
#
# nightstand self-update: fetch main, apply it, verify, and fall back on failure.
#
# The systemd timer runs this every 15 minutes. It keeps pi-bedroom on the newest
# commit of main that passes `nightstand selftest` here, and never gets stuck on
# a broken commit:
#
#   git fetch origin main      (on failure: write status, change nothing, exit 0)
#   target = origin/main
#   if target is the running commit, or a commit already known to fail here:
#       write status, exit 0
#   check out target; uv sync; nightstand selftest
#   pass: target becomes last_good, restart the service, write status
#   fail: record target as failed, check out last_good, uv sync, restart, write status
#
# State lives in NIGHTSTAND_STATE as two lines: last_good then failed (either may
# be empty). The published status file (NIGHTSTAND_STATUS) follows the pinned
# cross-repo schema so the room picker and the frame's own health marker see this
# machine's health. NIGHTSTAND_REPO / _STATE / _STATUS / _SERVICE / _HOST are
# overridable so the tests can drive the whole algorithm against a temp repo.
#
# This is the one place that writes status.json: no em dashes, no identifying
# data; host is this machine's hostname, written on the device at runtime.

set -euo pipefail

repo="${NIGHTSTAND_REPO:-/opt/nightstand}"
state="${NIGHTSTAND_STATE:-/var/lib/nightstand/state/update.state}"
status="${NIGHTSTAND_STATUS:-/var/lib/nightstand/public/status.json}"
service="${NIGHTSTAND_SERVICE:-nightstand}"
host="${NIGHTSTAND_HOST:-$(hostname)}"

cd "$repo"

last_good=""
failed=""
if [ -f "$state" ]; then
  last_good="$(sed -n '1p' "$state")"
  failed="$(sed -n '2p' "$state")"
fi

main_commit=""

write_state() {
  mkdir -p "$(dirname "$state")"
  printf '%s\n%s\n' "$last_good" "$failed" >"$state.tmp"
  mv "$state.tmp" "$state"
}

write_status() {
  local verify="$1"
  local commit checked_at
  commit="$(git rev-parse HEAD)"
  checked_at="$(date +%Y-%m-%dT%H:%M:%S%z)"
  mkdir -p "$(dirname "$status")"
  printf '{\n  "host": "%s",\n  "repo": "nightstand",\n  "commit": "%s",\n  "main_commit": "%s",\n  "last_good": "%s",\n  "verify": "%s",\n  "checked_at": "%s"\n}\n' \
    "$host" "$commit" "$main_commit" "$last_good" "$verify" "$checked_at" >"$status.tmp"
  mv "$status.tmp" "$status"
}

# Run the on-device self-test; echo "pass" or "fail" by its exit status.
verify_word() {
  if nightstand selftest >/dev/null 2>&1; then
    echo pass
  else
    echo fail
  fi
}

# A failed fetch (no network, origin unreachable) must change nothing. Report the
# commit already running, using the last origin/main we have on hand.
if ! git fetch origin main >/dev/null 2>&1; then
  main_commit="$(git rev-parse origin/main 2>/dev/null || git rev-parse HEAD)"
  write_status "$(verify_word)"
  exit 0
fi

main_commit="$(git rev-parse origin/main)"
head="$(git rev-parse HEAD)"

# Nothing to do when main is already the running commit, or a commit we have
# already seen fail here (do not retry it until main moves).
if [ "$main_commit" = "$head" ] || { [ -n "$failed" ] && [ "$main_commit" = "$failed" ]; }; then
  write_status "$(verify_word)"
  exit 0
fi

# Try the target: check it out, sync deps, verify.
git checkout --quiet "$main_commit"
if uv sync --extra pi --frozen && nightstand selftest >/dev/null 2>&1; then
  last_good="$main_commit"
  failed=""
  write_state
  systemctl restart "$service"
  write_status pass
  exit 0
fi

# The target is bad. Record it so it is not retried until main moves.
failed="$main_commit"
write_state

# With no earlier good commit there is nothing to fall back to: stay on the
# target and report the failure.
if [ -z "$last_good" ]; then
  write_status fail
  exit 1
fi

# Fall back to the last good commit, sync, and restart. Exit non-zero only if
# even the fallback fails to verify, so the timer leaves a visible failure.
git checkout --quiet "$last_good"
uv sync --extra pi --frozen || true
systemctl restart "$service"
recovered="$(verify_word)"
write_status "$recovered"
[ "$recovered" = pass ]
