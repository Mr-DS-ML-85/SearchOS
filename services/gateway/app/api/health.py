from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_cache
from app.config import get_settings
from app.db import get_session, get_session_factory
from app.errors import AuthError
from app.metrics import get_metrics, render_metrics
from app.models import ApiKey, Chunk, Document, IngestJob, IngestJobStatus
from app.schemas import HealthResponse, StatsResponse

router = APIRouter(tags=["ops"])

_STARTED_AT = time.monotonic()

# Readiness probes (Docker HEALTHCHECK, k8s) hit this on every interval; each
# call previously opened an outbound HTTP request to the renderer.
_ready_cache: dict[str, Any] = {"at": 0.0, "value": None}
_ready_ttl = 5.0


async def _require_stats_auth(
    x_api_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Gate /stats and /metrics behind an API key when auth is enabled.

    /stats runs four unbounded COUNT(*) over tables that can hold millions of
    rows; it was open to anyone who could reach the port.
    """
    settings = get_settings()
    if not settings.auth_enabled or not settings.rate_limit_health_endpoints:
        return
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise AuthError("Missing API key for ops endpoint.")
    from app.db import get_session_factory
    from app.models import ApiKey as ApiKeyModel
    from app.security import hash_api_key

    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(select(ApiKeyModel).where(ApiKeyModel.key_hash == hash_api_key(token)))
    if row is None or not row.active:
        raise AuthError("Invalid API key.")


@router.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready", response_model=HealthResponse)
async def health_ready() -> HealthResponse:
    now = time.monotonic()
    cached = _ready_cache["value"]
    if cached is not None and now - _ready_cache["at"] < _ready_ttl:
        return HealthResponse(**cached)

    settings = get_settings()
    services: dict = {"postgres": "unknown", "redis": "unknown"}
    healthy = True

    if settings.renderer_url:
        services["renderer"] = "unknown"
        try:
            from app.http import get_http_client

            resp = await get_http_client().get(f"{settings.renderer_url}/healthz", timeout=5.0)
            services["renderer"] = "ok" if resp.status_code < 500 else "degraded"
            if resp.status_code >= 500:
                healthy = False
        except Exception:
            services["renderer"] = "down"
            healthy = False

    try:
        if await get_cache().ping():
            services["redis"] = "ok"
        else:
            services["redis"] = "down"
            healthy = False
    except Exception:
        services["redis"] = "down"
        healthy = False

    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(select(1))
        services["postgres"] = "ok"
    except Exception:
        services["postgres"] = "down"
        healthy = False

    response = HealthResponse(status="ok" if healthy else "degraded", services=services)
    _ready_cache["at"] = now
    _ready_cache["value"] = response.model_dump()
    return response


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return await health_ready()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(_: Annotated[None, Depends(_require_stats_auth)]) -> bytes:
    if not get_settings().prometheus_enabled:
        return b""
    return render_metrics()


@router.get("/stats", response_model=StatsResponse)
async def stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(_require_stats_auth)] = None,
) -> StatsResponse:
    documents = await session.scalar(select(func.count()).select_from(Document)) or 0
    chunks = await session.scalar(select(func.count()).select_from(Chunk)) or 0
    api_keys = await session.scalar(select(func.count()).select_from(ApiKey)) or 0
    pending = (
        await session.scalar(
            select(func.count()).select_from(IngestJob).where(IngestJob.status == IngestJobStatus.pending)
        )
        or 0
    )
    get_metrics().queue_depth.set(pending)

    # Index-completeness signals (BUG: `documents` grew 87x while `chunks` stayed
    # frozen at 42, so a dead vector index looked healthy on a dashboard).
    without_chunks = (
        await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.status == "indexed",
                ~Document.chunks.any(),
            )
        )
        or 0
    )
    with_embedding = (
        await session.scalar(select(func.count()).select_from(Chunk).where(Chunk.embedding.is_not(None))) or 0
    )
    with_vector = (
        await session.scalar(
            select(func.count()).select_from(
                select(Chunk.document_id).where(Chunk.embedding.is_not(None)).distinct().subquery()
            )
        )
        or 0
    )

    try:
        from app.embed.provider import get_embedding_provider

        vector_available = bool(get_embedding_provider().available)
    except Exception:
        vector_available = False

    return StatsResponse(
        documents=documents,
        chunks=chunks,
        api_keys=api_keys,
        ingest_pending=pending,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
        documents_without_chunks=without_chunks,
        chunks_with_embedding=with_embedding,
        chunks_without_embedding=max(chunks - with_embedding, 0),
        embedding_coverage=round(with_vector / documents, 4) if documents else 0.0,
        vector_search_available=vector_available,
    )
