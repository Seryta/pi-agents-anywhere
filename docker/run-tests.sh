#!/usr/bin/env bash
# Run the test suite in the container. Never build or test on the host.
#
# Usage:
#   docker/run-tests.sh                     # unit tests, fake pi
#   PI_AA_TRUE_PI=1 docker/run-tests.sh     # also run real-pi integration tests
#   PI_AA_TRUE_PI_MODEL=1 docker/run-tests.sh  # opt-in: one real model turn
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AA_CONNECTOR="${PI_AA_CONNECTOR_SOURCE:-${REPO}/../Agents-Anywhere/connector}"

if [[ ! -d "${AA_CONNECTOR}/connector" ]]; then
    echo "Agents Anywhere connector source not found at ${AA_CONNECTOR}" >&2
    echo "Set PI_AA_CONNECTOR_SOURCE to the connector source directory." >&2
    exit 2
fi

IMAGE="${PI_AA_TEST_IMAGE:-pi-aa-test-env}"
ARGS=(--rm
    -e "PI_AA_CONNECTOR_SOURCE=/aa/connector"
    -e "PI_AA_TRUE_PI=${PI_AA_TRUE_PI:-0}"
    -e "PI_AA_TRUE_PI_MODEL=${PI_AA_TRUE_PI_MODEL:-0}"
    -v "${REPO}:/work"
    -v "${AA_CONNECTOR}:/aa/connector:ro"
    -w /work)

# A real model turn needs pi's account/config state, and pi must be able to
# write locks and session files, so use a throwaway writable copy.
TMP_AGENT=""
if [[ "${PI_AA_TRUE_PI_MODEL:-0}" == "1" && -d "${HOME}/.pi/agent" ]]; then
    TMP_AGENT="$(mktemp -d)"
    cp -r "${HOME}/.pi/agent/." "${TMP_AGENT}/"
    ARGS+=(-v "${TMP_AGENT}:/root/.pi/agent")
fi
cleanup() {
    [[ -n "${TMP_AGENT}" ]] && rm -rf "${TMP_AGENT}"
}
trap cleanup EXIT

COMMAND="${*:-python -m pytest -q}"
docker run "${ARGS[@]}" "${IMAGE}" bash -lc "${COMMAND}"
