# Test environment for pi-agents-anywhere.
#
# Contains Python 3.12, Node.js, a real `pi` install, and the test tooling so
# integration tests can drive an actual Pi RPC process. Build with:
#
#   docker/build-test-env.sh
#
FROM python:3.12-slim

ARG NODE_VERSION=v22.19.0
ARG PI_VERSION=0.87.1
# Registry endpoints default to the upstream projects; override them for
# restricted networks, e.g. --build-arg PIP_INDEX_URL=<mirror>/pypi/simple.
ARG NODE_DIST=https://nodejs.org/dist
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG PIP_INDEX_URL=https://pypi.org/simple

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git xz-utils \
    && curl -fsSL -o /tmp/node.tar.xz "${NODE_DIST}/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" \
    && tar -xf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && npm config set registry "${NPM_REGISTRY}" \
    && npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY test-requirements.txt /tmp/test-requirements.txt
COPY --from=connector_source /pyproject.toml /connector-source/pyproject.toml
COPY --from=connector_source /README.md /connector-source/README.md
COPY --from=connector_source /connector /connector-source/connector
COPY --from=integration_config /constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -c /tmp/constraints.txt -r /tmp/test-requirements.txt /connector-source

WORKDIR /work
