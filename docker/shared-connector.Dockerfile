# syntax=docker/dockerfile:1
FROM python:3.12-slim AS python-build
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends git ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=upstream_source /upstream.tar /tmp/upstream.tar
COPY integrations/agents-anywhere /integration
RUN python -c 'import hashlib,json,pathlib; p=pathlib.Path("/integration"); m=json.loads((p/"upstream.json").read_text()); assert hashlib.sha256((p/"upstream.patch").read_bytes()).hexdigest()==m["patchSha256"], "Integration patch hash mismatch"' \
    && mkdir /upstream && tar -xf /tmp/upstream.tar -C /upstream \
    && git -C /upstream apply --check /integration/upstream.patch \
    && git -C /upstream apply /integration/upstream.patch
COPY pyproject.toml README.md LICENSE /extension/
COPY src /extension/src
RUN python -m pip wheel --no-deps --no-cache-dir --wheel-dir /artifacts /upstream/connector /extension \
    && python -m pip install --no-cache-dir build==1.3.0 \
    && python -m build --sdist --outdir /artifacts /extension

FROM node:22-bookworm-slim AS plugin-build
ENV YARN_ENABLE_IMMUTABLE_INSTALLS=false YARN_ENABLE_SCRIPTS=false
COPY --from=python-build /upstream /upstream
WORKDIR /upstream/dsh-bridge-next
RUN corepack yarn install && corepack yarn typecheck && corepack yarn build && corepack yarn check:build \
    && mkdir /artifacts && npm pack --ignore-scripts --pack-destination /artifacts \
    && mv /artifacts/*.tgz /artifacts/dsh-bridge-next.tgz

FROM python-build AS integration-check
# The runtime dependencies are large wheels; a mirror keeps a slow link from
# failing the gate. Defaults to the official index, like the other build scripts.
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY --from=plugin-build /usr/local/bin/node /usr/local/bin/node
COPY --from=plugin-build /upstream /upstream
RUN python -m pip install --no-cache-dir --timeout 120 --retries 5 --index-url "${PIP_INDEX_URL}" --constraint /integration/constraints.txt /artifacts/*.whl
WORKDIR /upstream/dsh-bridge-next
RUN AA_TEST_PYTHON=/usr/local/bin/python3.12 node node_modules/tsx/dist/cli.mjs --test --test-concurrency=1 tests/unit/interaction-stream.test.ts tests/integration/external-connector.test.ts > /artifacts/shared-integration.tap \
    && python -c 'import json,pathlib; pathlib.Path("/artifacts/integration-verified.json").write_text(json.dumps({"ok":True,"scope":"Node client to packaged Python shared service; reload, logout, reset, restart, permissions, pre-auth ownership, Peer compatibility and capability retry transitions"})+"\n")'

FROM python:3.12-slim AS manifest
COPY --from=python-build /artifacts /artifacts
COPY --from=plugin-build /artifacts /artifacts
COPY --from=integration-check /artifacts/integration-verified.json /artifacts/integration-verified.json
COPY --from=integration-check /artifacts/shared-integration.tap /artifacts/shared-integration.tap
COPY integrations/agents-anywhere/upstream.json /artifacts/upstream.json
COPY integrations/agents-anywhere/constraints.txt /artifacts/constraints.txt
COPY deploy/systemd/agents-anywhere-connector.service /artifacts/agents-anywhere-connector.service
RUN python - <<'PYTHON'
import hashlib
import json
from pathlib import Path
import tarfile

artifacts = Path("/artifacts")
files = sorted(artifacts.iterdir())
checksums = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
(artifacts / "SHA256SUMS.json").write_text(json.dumps(checksums, indent=2) + "\n")
version = next(artifacts.glob("pi_agents_anywhere-*.whl")).name.split("-")[1]
files = sorted(artifacts.iterdir())
with tarfile.open(artifacts / f"pi-agents-anywhere-{version}-shared-connector.tar.gz", "w:gz") as bundle:
    for file in files:
        bundle.add(file, arcname=file.name)
checksums = [f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}"
             for f in sorted(artifacts.iterdir())]
(artifacts / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
PYTHON

FROM scratch AS artifacts
COPY --from=manifest /artifacts /
