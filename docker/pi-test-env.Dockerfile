# Test environment for pi-agents-anywhere.
#
# Contains Python 3.12, Node.js, a real `pi` install, and the test tooling so
# integration tests can drive an actual Pi RPC process. Build with:
#
#   docker/build-test-env.sh
#
FROM python:3.12-slim

ARG NODE_VERSION=v22.14.0
ARG PI_VERSION=0.87.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" \
    && tar -xf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && npm config set registry https://registry.npmjs.org \
    && npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -i https://pypi.org/simple/ \
        pytest \
        pytest-asyncio \
        pydantic \
        loguru \
        jsonschema \
        ruff

WORKDIR /work
