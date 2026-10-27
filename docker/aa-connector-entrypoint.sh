#!/usr/bin/env bash
# Refresh the writable project copy, resolve dependencies once, then run the given command.
set -euo pipefail
rm -rf /app && mkdir -p /app
cp -a /src/connector/. /app/
# A virtualenv built for another path keeps its absolute interpreter; never reuse it.
rm -rf /app/.venv
if [ -d /src/contracts ]; then rm -rf /contracts && cp -a /src/contracts /contracts; fi
cd /app
uv sync --quiet
exec "$@"