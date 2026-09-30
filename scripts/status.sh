#!/usr/bin/env bash
# Compact status snapshot for a systemd user service (default: the connector).
#
# Usage:
#   scripts/status.sh [unit-name]
#
# Every systemctl/journalctl call has a hard timeout and bounded output, so a
# slow user manager, a pager, or a large journal slice can never block the
# caller — the failure mode this script exists to avoid.
set -uo pipefail

UNIT="${1:-pi-aa-connector}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "unit:   ${UNIT}"
echo "active: $(timeout 5 systemctl --user is-active "${UNIT}" 2>&1 || true)"
state="$(
  timeout 5 systemctl --user show \
    -p ExecMainStartTimestamp -p MainPID -p NRestarts --value "${UNIT}" 2>&1 \
    | tr '\n' ' ' || true
)"
echo "state:  ${state}"

echo "--- warnings/errors (last 10 min) ---"
timeout 8 journalctl --user -u "${UNIT}" --since "-10 min" --no-pager -o cat 2>&1 \
  | grep -E "WARNING|ERROR" | tail -8 || true

echo "--- last log lines ---"
timeout 8 journalctl --user -u "${UNIT}" -n 6 --no-pager -o cat 2>&1 | cut -c1-140 || true
