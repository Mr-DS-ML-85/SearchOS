from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

from app.api.deps import current_api_key
from app.cache import get_cache
from app.config import get_settings
from app.db import get_session_factory
from app.errors import AppError, to_http_error
from app.models import ApiKey
from app.schemas import SearchRequest, SearchResponse
from app.security import get_security_service

router = APIRouter(tags=["search"])


def build_orchestrator():
    from app.search.orchestrator import SearchOrchestrator

    settings = get_settings()
    orchestrator = SearchOrchestrator()
    if not settings.local_index_enabled:
        return orchestrator

    from app.search.local_index import LocalIndexProvider
    from app.search.vector_index import VectorIndexProvider

    session_factory = get_session_factory()
    orchestrator.providers["local"] = LocalIndexProvider(session_factory)

    from app.embed.provider import get_embedding_provider

    embedding = get_embedding_provider()
    if embedding.available:
        orchestrator.providers["vector"] = VectorIndexProvider(session_factory, embedding)

    if settings.searxng_url:
        from app.search.providers.searxng import SearXNGProvider

        orchestrator.providers["web"] = SearXNGProvider(
            session_factory, settings.searxng_url, settings.searxng_timeout_seconds
        )
    return orchestrator


@router.get("/v1/search", response_model=SearchResponse)
async def search(
    req: Annotated[SearchRequest, Depends()],
    api_key: Annotated[ApiKey, Depends(current_api_key)],
    background_tasks: BackgroundTasks,
) -> SearchResponse:
    start = time.monotonic()
    orchestrator = build_orchestrator()

    try:
        response = await orchestrator.search(req)
        background_tasks.add_task(
            get_security_service().log_usage,
            api_key,
            "search",
            200,
            int((time.monotonic() - start) * 1000),
        )
        return response
    except AppError as e:
        raise to_http_error(e) from e
