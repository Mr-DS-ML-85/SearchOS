# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/mcp

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY searchos_mcp.py .

RUN useradd -m -u 1001 mcp && chown -R mcp:mcp /srv/mcp
USER mcp

EXPOSE 8099

# Loopback-only publish and a required bearer token: the container binds 0.0.0.0
# so docker can reach it, which the server treats as a non-loopback bind and
# refuses to start without MCP_HTTP_TOKEN. That is the intended behaviour — an
# unauthenticated /mcp on a published port hands anyone the ingest admin key.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8099/healthz', timeout=3).status==200 else 1)"

CMD ["python", "searchos_mcp.py", "--transport", "http", "--host", "0.0.0.0", "--port", "8099"]