# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/searchos

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# The base image is FTS-only. `sentence-transformers` (and the ~3 GB of torch +
# CUDA wheels it drags in) is opt-in:
#
#   docker build --build-arg INSTALL_EMBEDDINGS=1 ...
#
# Without it the `vector` provider reports itself unavailable and search falls
# back to Postgres FTS + SearXNG, which is the documented degraded mode. The
# worker container is the one that benefits, since it is what writes embeddings.
ARG INSTALL_EMBEDDINGS=0
RUN pip install --upgrade pip \
    && if [ "$INSTALL_EMBEDDINGS" = "1" ]; then \
         pip install "sentence-transformers>=2.7"; \
       fi \
    && pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY worker.py worker_entrypoint.py ./

# /tmp/prometheus is the shared PROMETHEUS_MULTIPROC_DIR: with --workers 2 each
# process has its own registry, so a scrape would otherwise report only one
# worker's numbers and roughly half the traffic would be invisible.
RUN useradd -m -u 1001 searchos \
    && mkdir -p /tmp/prometheus \
    && chown -R searchos:searchos /srv/searchos /tmp/prometheus
USER searchos

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]