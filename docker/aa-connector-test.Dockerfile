# Agents Anywhere Connector checks, isolated from the host.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

RUN apt-get update
RUN apt-get install -y --no-install-recommends ca-certificates curl git build-essential
RUN rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

COPY aa-connector-entrypoint.sh /usr/local/bin/aa-connector-entrypoint.sh
RUN chmod +x /usr/local/bin/aa-connector-entrypoint.sh

WORKDIR /app
ENTRYPOINT ["/usr/local/bin/aa-connector-entrypoint.sh"]