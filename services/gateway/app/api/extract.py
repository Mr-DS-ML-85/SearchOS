from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_api_key
from app.cache import cache_key, get_cache
from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.extract.extractor import extract_page
from app.extract.fetcher import fetch_page
from app.metrics import get_metrics
from app.models import ApiKey
from app.schemas import ExtractRequest, ExtractResponse
from app.security import get_security_service

router = APIRouter(tags=["extract"])


@router.post("/v1/extract", response_model=ExtractResponse)
async def extract(
    req: ExtractRequest,
    request: Request,
    api_key: Annotated[ApiKey, Depends(current_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
    background_tasks: BackgroundTasks,
) -> ExtractResponse:
    start = time.monotonic()
    settings = get_settings()
    cache = get_cache()
    url_hash = hashlib.sha256(req.url.encode()).hexdigest()
    key = cache_key("extract", url_hash, req.format, req.max_chars or "full")

    cached = await cache.get(key)
    if cached:
        get_metrics().cache_hits.inc()
        cached["cached"] = True
        # took_ms/fetched_at were baked in when the entry was written; a 2ms cache
        # hit was reporting the original 4s latency.
        cached["took_ms"] = int((time.monotonic() - start) * 1000)
        return ExtractResponse(**cached)

    get_metrics().cache_misses.inc()
    try:
        page = await fetch_page(req.url)
        # trafilatura/pypdf are CPU-bound and synchronous; running them on the
        # event loop stalled every concurrent request in the worker.
        extracted = await asyncio.to_thread(extract_page, page, fmt=req.format)
    except AppError as e:
        get_metrics().extract_total.labels(status="error").inc()
        background_tasks.add_task(
            get_security_service().log_usage, api_key, "extract", e.http_status, int((time.monotonic() - start) * 1000)
        )
        raise e

    content = extracted.content
    truncated = False
    if req.max_chars and len(content) > req.max_chars:
        content = content[: req.max_chars]
        truncated = True

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata = None
    if req.format == "json":
        # Keep the structured metadata alongside the JSON body instead of
        # burying it inside a string the caller has to re-parse.
        metadata = {
            "title": extracted.title,
            "language": extracted.language,
            "author": extracted.author,
            "published": extracted.published,
            "source_content_type": extracted.content_type,
        }

    response = ExtractResponse(
        url=req.url,
        final_url=page.final_url,
        title=extracted.title,
        content=content,
        content_type=extracted.content_type,
        fetched_at=now,
        cached=False,
        truncated=truncated,
        took_ms=int((time.monotonic() - start) * 1000),
        format=req.format,
        metadata=metadata,
        fetched_via=page.via,
    )

    await cache.set(key, response.model_dump(), ttl_seconds=settings.extract_cache_ttl_seconds)
    # Invalidate stale search results for this URL so a fresh extraction is not
    # masked by cached search responses for up to search_cache_ttl_seconds.
    try:
        await cache.invalidate("search:*")
    except Exception:
        pass
    get_metrics().extract_total.labels(status="ok").inc()
    # monotonic() is already in seconds; dividing by 1000 recorded every
    # extraction as 1000x faster than it was.
    get_metrics().extract_duration.observe(time.monotonic() - start)
    background_tasks.add_task(
        get_security_service().log_usage, api_key, "extract", 200, int((time.monotonic() - start) * 1000)
    )
    return response
