#!/usr/bin/env bash
# Build the container used for tests. Never build or test on the host.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Mirrors for restricted networks can come from the environment
# (PI_AA_NODE_DIST / PI_AA_NPM_REGISTRY / PI_AA_PIP_INDEX_URL) or --build-arg.
docker build -f "${DIR}/pi-test-env.Dockerfile" -t pi-aa-test-env \
    --build-arg "NODE_DIST=${PI_AA_NODE_DIST:-https://nodejs.org/dist}" \
    --build-arg "NPM_REGISTRY=${PI_AA_NPM_REGISTRY:-https://registry.npmjs.org}" \
    --build-arg "PIP_INDEX_URL=${PI_AA_PIP_INDEX_URL:-https://pypi.org/simple}" \
    "$@" "${DIR}"
