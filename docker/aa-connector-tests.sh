#!/usr/bin/env bash
# Run the Agents Anywhere Connector checks in a container. Never on the host.
#
# Usage:
#   docker/aa-connector-tests.sh                                  # ruff + pytest
#   docker/aa-connector-tests.sh python -m pytest -q tests/test_shared_service.py
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AA_CONNECTOR="${PI_AA_CONNECTOR_SOURCE:-${REPO}/../Agents-Anywhere/connector}"
AA_ROOT="$(cd "${AA_CONNECTOR}/.." && pwd)"

if [[ ! -f "${AA_CONNECTOR}/pyproject.toml" ]]; then
    echo "Set PI_AA_CONNECTOR_SOURCE to the Connector source directory" >&2
    exit 2
fi

IMAGE="${PI_AA_AA_TEST_IMAGE:-pi-aa-connector-test}"
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    docker build -f "${REPO}/docker/aa-connector-test.Dockerfile" -t "${IMAGE}" "${REPO}/docker"
fi

if [[ $# -eq 0 ]]; then
    set -- bash -c "uv run ruff check connector tests && uv run pytest -q"
fi

ARGS=(--rm -e UV_CACHE_DIR=/uv-cache -v "${AA_ROOT}/contracts:/src/contracts:ro" -v "${AA_CONNECTOR}:/src/connector:ro" -v pi-aa-uv-cache:/uv-cache)
# Contract fixtures live beside the connector; the Connector tests read them from /contracts.
docker run "${ARGS[@]}" "${IMAGE}" "$@"