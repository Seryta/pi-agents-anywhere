#!/usr/bin/env bash
# Build the container used for tests. Never build or test on the host.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -f "${DIR}/pi-test-env.Dockerfile" -t pi-aa-test-env "$@" "${DIR}"
