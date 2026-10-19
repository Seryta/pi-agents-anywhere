#!/usr/bin/env bash
# Build the container used for tests. Never build or test on the host.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AA_CONNECTOR="${PI_AA_CONNECTOR_SOURCE:-${DIR}/../../Agents-Anywhere/connector}"
if [[ ! -f "${AA_CONNECTOR}/pyproject.toml" ]]; then
    echo "Set PI_AA_CONNECTOR_SOURCE to the reviewed upstream Connector" >&2
    exit 2
fi
# Mirrors for restricted networks can come from the environment
# (PI_AA_NODE_DIST / PI_AA_NPM_REGISTRY / PI_AA_PIP_INDEX_URL) or --build-arg.
docker build -f "${DIR}/pi-test-env.Dockerfile" -t "${PI_AA_TEST_IMAGE:-pi-aa-test-env}" \
    --build-context "connector_source=${AA_CONNECTOR}" \
    --build-context "integration_config=${DIR}/../integrations/agents-anywhere" \
    --build-arg "NODE_DIST=${PI_AA_NODE_DIST:-https://nodejs.org/dist}" \
    --build-arg "NPM_REGISTRY=${PI_AA_NPM_REGISTRY:-https://registry.npmjs.org}" \
    --build-arg "PIP_INDEX_URL=${PI_AA_PIP_INDEX_URL:-https://pypi.org/simple}" \
    "$@" "${DIR}"
