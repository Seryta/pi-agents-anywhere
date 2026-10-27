#!/usr/bin/env bash
# Verify the shared Connector service after a restart, without the agent session.
#
# Called from the delayed restart unit, so it still runs after the session that
# requested the restart is torn down. Exit status: 0 healthy, 1 unhealthy.
set -uo pipefail
BASE="${AA_BASE:-$HOME/.local/share/agents-anywhere}"
RENDEZVOUS="${AA_RENDEZVOUS:-$HOME/.agents-anywhere/connector-service}"
OUT="${AA_VERIFY_OUT:-$HOME/.local/state/aa-restart-verify.log}"
mkdir -p "$(dirname "$OUT")"
exec >>"$OUT" 2>&1
echo "===== $(date -Is) restart verification"

fail=0
for attempt in 1 2 3 4 5 6; do
    if systemctl --user is-active --quiet agents-anywhere-connector.service; then break; fi
    echo "service not active yet (attempt $attempt)"
    sleep 5
done
if ! systemctl --user is-active --quiet agents-anywhere-connector.service; then
    echo "FAIL service is not active"
    systemctl --user status agents-anywhere-connector.service --no-pager | tail -20
    exit 1
fi
echo "service: active"

for attempt in 1 2 3 4 5 6; do
    if [[ -s "$RENDEZVOUS" ]]; then break; fi
    sleep 2
done
if [[ ! -s "$RENDEZVOUS" ]]; then
    echo "FAIL no shared service rendezvous at $RENDEZVOUS"
    fail=1
else
    mode=$(stat -c %a "$RENDEZVOUS")
    echo "rendezvous: $RENDEZVOUS mode=$mode"
    [[ "$mode" == "600" ]] || { echo "FAIL rendezvous must be 0600"; fail=1; }
fi

doctor=$("$BASE/venv/bin/python" -m connector.cli doctor --expect-runtime pi --expect-runtime dsh 2>&1)
rc=$?
echo "doctor rc=$rc"
echo "$doctor"
[[ $rc -eq 0 ]] || fail=1

versions=$("$BASE/venv/bin/python" -c 'from importlib.metadata import version; print("anywhere-cli", version("anywhere-cli")); print("pi-agents-anywhere", version("pi-agents-anywhere"))' 2>&1)
echo "versions:"
echo "$versions"

echo "recent service log:"
journalctl --user -u agents-anywhere-connector.service --since "3 min ago" --no-pager -o short-iso | tail -25
echo "===== $(date -Is) verification done fail=$fail"
exit "$fail"
