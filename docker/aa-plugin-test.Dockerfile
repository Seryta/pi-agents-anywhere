# DSH bridge plugin checks: the Node host plus the real Python Connector it manages.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy
# Mirrors the Node version the repository CI uses.
ENV NODE_VERSION=22.23.2

RUN apt-get update
RUN apt-get install -y --no-install-recommends ca-certificates curl git build-essential xz-utils
RUN rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz
RUN tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
RUN rm /tmp/node.tar.xz
RUN corepack enable
RUN pip install --no-cache-dir uv

WORKDIR /app