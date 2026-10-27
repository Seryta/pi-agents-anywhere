#!/usr/bin/env bash
# Install a built release of this integration and point the shared service at it.
#
# Usage: deploy/install-release.sh [ARTIFACTS_DIR] [RELEASE_NAME]
#   ARTIFACTS_DIR  directory holding the two wheels, the DSH tgz and constraints
#                  (default: <repo>/dist/shared-connector)
#   RELEASE_NAME   release directory name (default: the Pi wheel version)
#
# The running service keeps its loaded modules; restart it (and DSH) separately.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS="${1:-${REPO}/dist/shared-connector}"
BASE="${AA_BASE:-$HOME/.local/share/agents-anywhere}"

PI_VERSION="$(python3 -c 'import glob,sys; print(glob.glob(sys.argv[1] + "/pi_agents_anywhere-*.whl")[0].split("-")[1])' "$ARTIFACTS")"
RELEASE_NAME="${2:-$PI_VERSION}"
RELEASE="$BASE/releases/$RELEASE_NAME"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "== artifacts -> $RELEASE/artifacts"
mkdir -p "$RELEASE/artifacts"
cp -a "$ARTIFACTS/." "$RELEASE/artifacts/"
(cd "$RELEASE/artifacts" && sha256sum --check --quiet SHA256SUMS)

echo "== venv"
if [ ! -x "$RELEASE/venv/bin/python" ]; then
    python3 -m venv "$RELEASE/venv"
fi
"$RELEASE/venv/bin/pip" install --quiet --no-build --upgrade --timeout 120 --retries 5 \
    --index-url "${PI_AA_PIP_INDEX_URL:-https://pypi.org/simple}" \
    -c "$RELEASE/artifacts/constraints.txt" \
    "$RELEASE"/artifacts/anywhere_cli-*.whl "$RELEASE"/artifacts/pi_agents_anywhere-*.whl
"$RELEASE/venv/bin/python" -c 'from importlib.metadata import version; print("anywhere-cli", version("anywhere-cli")); print("pi-agents-anywhere", version("pi-agents-anywhere"))'

echo "== switch $BASE/venv"
if [ -L "$BASE/venv" ]; then
    mv "$BASE/venv" "$BASE/venv.bak-$STAMP"
elif [ -e "$BASE/venv" ]; then
    mv "$BASE/venv" "$BASE/venv.bak-$STAMP.dir"
fi
ln -s "$RELEASE/venv" "$BASE/venv"
ls -l "$BASE/venv"
"$BASE/venv/bin/python" -m connector.cli --help >/dev/null

echo "== DSH profile"
PROFILE="${DSH_PROFILE_DIR:-$HOME/.dsh/profiles/web}"
if [ -f "$PROFILE/cordis.patch.yml" ]; then
    cp -a "$PROFILE/cordis.patch.yml" "$PROFILE/cordis.patch.yml.bak-$STAMP"
    python3 - "$PROFILE/cordis.patch.yml" <<'PYTHON'
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
updated = text.replace("connectorSocketPath:", "connectorServicePath:")
updated = updated.replace("connector.sock", "connector-service")
open(path, "w", encoding="utf-8").write(updated)
print("connector config keys:", [line.strip() for line in updated.splitlines() if "connectorServicePath" in line])
PYTHON
fi
DSH="${DSH_BIN:-$(command -v dsh || true)}"
if [ -n "$DSH" ] && [ -f "$RELEASE/artifacts/dsh-bridge-next.tgz" ]; then
    "$DSH" plugin --profile "${DSH_PROFILE:-web}" add --ignore-scripts --save-exact "$RELEASE/artifacts/dsh-bridge-next.tgz"
fi

echo "== done: $RELEASE"
echo "Next: restart the shared service (this also restarts the DSH host it owns):"
echo "  systemd-run --user --collect --unit=aa-restart-\$RANDOM --on-active=8 /bin/bash -c 'systemctl --user restart agents-anywhere-connector.service; sleep 20; bash $REPO/deploy/verify-after-restart.sh'"
