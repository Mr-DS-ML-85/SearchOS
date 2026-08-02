from __future__ import annotations

import asyncio

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cache import get_cache
from app.config import get_settings
from app.extract.extractor import extract_page
from app.extract.fetcher import fetch_page
from app.logging import get_logger
from app.metrics import get_metrics
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
    from app.workers.ingest import normalize_url

    for url in urls:
        try:
            norm = normalize_url(url)
            async with session_factory() as session:
                existing = await session.scalar(
                    select(Document).where(Document.normalized_url == norm)
                )
                if existing and existing.content_text:
                    is_snippet = (existing.meta or {}).get(SNIPPET_META_FLAG) is True
                    if not is_snippet:
                        continue
            page = await fetch_page(url)
            extracted = extract_page(page)
            if not extracted.content.strip():
                continue
            async with session_factory() as session:
                doc = await session.scalar(
                    select(Document).where(Document.normalized_url == norm)
                )
                if doc is None:
                    doc = Document(url=url, normalized_url=norm, status="indexed")
                    session.add(doc)
                doc.title = extracted.title
                doc.content_text = extracted.content[:100000]
                doc.fetched_at = func.now()
                doc.status = "indexed"
                await session.commit()
            get_metrics().documents_indexed.inc()
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
