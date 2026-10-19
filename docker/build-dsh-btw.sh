#!/usr/bin/env bash
# Host orchestrates Docker; dependency install, tests and packaging are isolated.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-${REPO}/dist/dsh-btw}"
docker build --file "${REPO}/plugins/dsh-btw/Dockerfile" \
    --output "type=local,dest=${OUTPUT}" "${REPO}/plugins/dsh-btw"
