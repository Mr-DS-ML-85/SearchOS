from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_cache
from app.config import get_settings
from app.db import get_session, get_session_factory
from app.metrics import get_metrics, render_metrics
from app.models import ApiKey, Chunk, Document, IngestJob, IngestJobStatus
from app.schemas import HealthResponse, StatsResponse

router = APIRouter(tags=["ops"])


@router.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready", response_model=HealthResponse)
async def health_ready() -> HealthResponse:
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

    return HealthResponse(status="ok" if healthy else "degraded", services=services)


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return await health_ready()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> bytes:
    return render_metrics()


@router.get("/stats", response_model=StatsResponse)
async def stats(
    session: Annotated[AsyncSession, Depends(get_session)],
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
    return StatsResponse(
        documents=documents,
        chunks=chunks,
        api_keys=api_keys,
        ingest_pending=pending,
        uptime_seconds=0.0,
    )
