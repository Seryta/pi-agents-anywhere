#!/usr/bin/env bash
# Host: source archive and Docker orchestration only. All packaging is isolated.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-${REPO}/dist/shared-connector}"
UPSTREAM="${PI_AA_UPSTREAM_SOURCE:-${REPO}/../Agents-Anywhere}"
COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "${REPO}/integrations/agents-anywhere/upstream.json")"
SCRATCH="$(mktemp -d)"
trap 'rm -rf -- "$SCRATCH"' EXIT
if ! git -C "$UPSTREAM" rev-parse --git-dir >/dev/null 2>&1; then
    UPSTREAM="${SCRATCH}/upstream"
    git init -q "$UPSTREAM"
    git -C "$UPSTREAM" remote add origin https://github.com/anywhere-labs/Agents-Anywhere.git
    git -C "$UPSTREAM" fetch --depth 1 origin "$COMMIT"
fi
# An archive of the fixed commit deliberately excludes checkout-local modifications.
git -C "$UPSTREAM" archive "$COMMIT" > "${SCRATCH}/upstream.tar"
docker build --file "${REPO}/docker/shared-connector.Dockerfile" \
    --build-context "upstream_source=${SCRATCH}" \
    --build-arg "PIP_INDEX_URL=${PI_AA_PIP_INDEX_URL:-https://pypi.org/simple}" \
    --output "type=local,dest=${OUTPUT}" "$REPO"
