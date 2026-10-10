from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cache import get_cache
from app.config import get_settings
from app.extract.extractor import extract_page
from app.extract.fetcher import fetch_page
from app.logging import get_logger
from app.models import Document

logger = get_logger("workers.snippet")

SNIPPET_QUEUE_KEY = "queue:snippets"
SNIPPET_SEEN_KEY = "set:snippets:seen"


async def enqueue_snippets(urls: list[str]) -> None:
    cache = get_cache()
    for url in urls:
        if await cache.add_to_set(SNIPPET_SEEN_KEY, url):
            await cache.rpush(SNIPPET_QUEUE_KEY, url)


async def enrich_snippets(session_factory: async_sessionmaker, urls: list[str]) -> None:
    from app.search.providers.searxng import SNIPPET_META_FLAG
    from app.workers.ingest import index_document, normalize_url

    for url in urls:
        try:
            norm = normalize_url(url)
            async with session_factory() as session:
                existing = await session.scalar(select(Document).where(Document.normalized_url == norm))
                if existing and existing.content_text:
                    is_snippet = (existing.meta or {}).get(SNIPPET_META_FLAG) is True
                    if not is_snippet:
                        continue
            page = await fetch_page(url)
            extracted = extract_page(page)
            if not extracted.content.strip():
                continue
            # The full indexing path, not a bare content swap: this used to set
            # content_text and nothing else, leaving tsv, lang, simhash, chunks
            # and vectors describing the snippet, and the snippet flag set — so
            # the next search that returned this URL reverted it.
            await index_document(
                session_factory,
                url=url,
                final_url=url,
                extracted=extracted,
                source="crawler",
                max_chars=100000,
            )
        except Exception as e:
            logger.debug("snippet enrichment failed for %s: %s", url, e)


async def snippet_worker_loop(session_factory: async_sessionmaker) -> None:
    settings = get_settings()
    cache = get_cache()
    while True:
        try:
            urls = []
            for _ in range(settings.worker_snippet_batch):
                url = await cache.lpop(SNIPPET_QUEUE_KEY)
                if url is None:
                    break
                urls.append(url)
            if urls:
                await enrich_snippets(session_factory, urls)
            else:
                await asyncio.sleep(settings.worker_snippet_poll_interval_seconds)
        except Exception as e:
            logger.warning("snippet loop error: %s", e)
            await asyncio.sleep(settings.worker_snippet_poll_interval_seconds)
