from __future__ import annotations

import hashlib
import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_api_key
from app.cache import get_cache, cache_key
from app.config import get_settings
from app.db import get_session
from app.errors import AppError, to_http_error
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
        return ExtractResponse(**cached)

    get_metrics().cache_misses.inc()
    try:
        page = await fetch_page(req.url)
        extracted = extract_page(page)
    except AppError as e:
        get_metrics().extract_total.labels(status="error").inc()
        background_tasks.add_task(
            get_security_service().log_usage, api_key, "extract", e.http_status, int((time.monotonic() - start) * 1000)
        )
        raise to_http_error(e) from e

    content = extracted.content
    truncated = False
    if req.max_chars and len(content) > req.max_chars:
        content = content[: req.max_chars]
        truncated = True

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
    )

    await cache.set(key, response.model_dump(), ttl_seconds=settings.extract_cache_ttl_seconds)
    get_metrics().extract_total.labels(status="ok").inc()
    get_metrics().extract_duration.observe((time.monotonic() - start) / 1000)
    background_tasks.add_task(
        get_security_service().log_usage, api_key, "extract", 200, int((time.monotonic() - start) * 1000)
    )
    return response
