from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.api.deps import current_api_key
from app.config import get_settings
from app.db import get_session_factory
from app.errors import AppError, ValidationError
from app.models import ApiKey
from app.schemas import SearchRequest, SearchResponse
from app.security import get_security_service

router = APIRouter(tags=["search"])


def build_orchestrator():
    """Build an orchestrator with whatever providers the config enables.

    Note: LOCAL_INDEX_ENABLED only gates the *local/vector* providers. Gating
    it before provider registration meant the flag also removed SearXNG, so
    sources=web returned a 404 "no providers" error.
    """
    from app.search.orchestrator import SearchOrchestrator

    settings = get_settings()
    orchestrator = SearchOrchestrator()

    if settings.local_index_enabled:
        from app.embed.provider import get_embedding_provider
        from app.search.local_index import LocalIndexProvider
        from app.search.vector_index import VectorIndexProvider

        session_factory = get_session_factory()
        orchestrator.providers["local"] = LocalIndexProvider(session_factory)

        embedding = get_embedding_provider()
        if embedding.available:
            orchestrator.providers["vector"] = VectorIndexProvider(session_factory, embedding)

    if settings.searxng_url or settings.search_cascade_enabled:
        from app.search.providers.cascade import build_cascade

        cascade = build_cascade(settings)
        if cascade is not None:
            orchestrator.providers["web"] = cascade
        elif settings.searxng_url:
            from app.search.providers.searxng import SearXNGProvider

            searxng = SearXNGProvider(get_session_factory(), settings.searxng_url, settings.searxng_timeout_seconds)
            orchestrator.providers["web"] = searxng
            orchestrator.providers["searxng"] = searxng

        # Also register each tier on its own name so sources=tavily / exa /
        # jina / searxng can pin one upstream. Without this those values were
        # rejected by the enum, which made it impossible to tell whether a tier
        # was dead, misconfigured, or simply never reached.
        for tier in getattr(cascade, "tiers", []):
            orchestrator.providers.setdefault(tier.name, tier)
    if settings.federated_peers:
        from app.search.providers.federated import build_federated

        federated = build_federated(settings)
        if federated is not None:
            orchestrator.providers["federated"] = federated

    return orchestrator


@router.get("/v1/search", response_model=SearchResponse)
async def search(
    req: Annotated[SearchRequest, Depends()],
    api_key: Annotated[ApiKey, Depends(current_api_key)],
    background_tasks: BackgroundTasks,
    # Sequence fields on a Depends()-bound Pydantic model are not registered as
    # query parameters by FastAPI (they are classified as body params), so
    # `?domains=x` was silently ignored and the local provider's domain filter
    # was unreachable. Declared explicitly here so they arrive on a GET.
    domains: Annotated[list[str] | None, Query()] = None,
    exclude_domains: Annotated[list[str] | None, Query()] = None,
) -> SearchResponse:
    start = time.monotonic()

    merged = req.model_copy()
    merged.domains = _split_domains(domains) or _split_domains(req.domains)
    merged.exclude_domains = _split_domains(exclude_domains) or _split_domains(req.exclude_domains)

    # The model validator cannot catch this: domains/exclude_domains arrive as
    # explicit route params (see above), so they are never present in the
    # constructor input the validator inspects. Include-all-then-exclude-all can
    # never match, and returning an empty page reads as "no such content exists".
    clash = {d.lower().strip(".") for d in merged.domains or []} & {
        d.lower().strip(".") for d in merged.exclude_domains or []
    }
    if clash:
        raise ValidationError(
            "domains and exclude_domains both contain " + ", ".join(sorted(clash)) + " — no document could ever match"
        )

    orchestrator = build_orchestrator()

    try:
        response = await orchestrator.search(merged)
        background_tasks.add_task(
            get_security_service().log_usage,
            api_key,
            "search",
            200,
            int((time.monotonic() - start) * 1000),
        )
        return response
    except AppError as e:
        raise e


def _split_domains(values: list[str] | None) -> list[str] | None:
    """Accept repeated params and comma-joined values alike."""
    if not values:
        return None
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in str(value).split(",") if part.strip())
    return out or None
