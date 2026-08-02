from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from html import unescape as html_unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.embed.chunker import chunk_text, estimate_tokens
from app.embed.provider import get_embedding_provider
from app.extract.extractor import extract_page
from app.extract.fetcher import fetch_page
from app.extract.robots import RobotsChecker
from app.logging import get_logger
from app.metrics import get_metrics
from app.models import Chunk, Document, IngestJob, IngestJobStatus, IngestKind

logger = get_logger("workers.ingest")

_LINK_RE = re.compile(r'<a[^>]+href=["\']\s*([^"\' >\s]+)["\']', re.IGNORECASE)


def normalize_url(url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    scheme = (parsed.scheme or "http").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    return f"{scheme}://{netloc}{path}".rstrip("/")


async def process_ingest_job(session_factory: async_sessionmaker, job: IngestJob) -> None:
    settings = get_settings()
    if job.kind == IngestKind.sitemap:
        await _process_sitemap(session_factory, job)
        return

    try:
        page = await fetch_page(job.url)
        extracted = extract_page(page)
        content = extracted.content
        if not content.strip():
            raise ValueError("empty extracted content")

        doc_id = uuid.uuid4()
        doc = Document(
            id=doc_id,
            url=job.url,
            normalized_url=normalize_url(page.final_url),
            title=extracted.title,
            content_text=content,
            content_html=None,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            lang=extracted.language,
            source="ingest",
            author=extracted.author,
            published_at=None,
            fetched_at=datetime.now(timezone.utc),
            status="indexed",
        )

        chunks = chunk_text(content)
        vectors: list[list[float]] | None = None
        embedding = get_embedding_provider()
        if embedding.available:
            try:
                vectors = await embedding.embed(chunks)
            except Exception as e:
                logger.warning("embedding failed for %s: %s", job.url, e)

        async with session_factory() as session:
            existing = await session.scalar(
                select(Document).where(Document.normalized_url == doc.normalized_url)
            )
            if existing:
                for chunk in existing.chunks:
                    await session.delete(chunk)
                existing.title = doc.title
                existing.content_text = doc.content_text
                existing.content_hash = doc.content_hash
                existing.lang = doc.lang
                existing.status = "indexed"
                doc = existing
            else:
                session.add(doc)

            for i, chunk_content in enumerate(chunks):
                session.add(
                    Chunk(
                        document_id=doc.id,
                        index=i,
                        content=chunk_content,
                        token_count=estimate_tokens(chunk_content),
                        embedding=vectors[i] if vectors else None,
                    )
                )
            await session.execute(
                update(IngestJob)
                .where(IngestJob.id == job.id)
                .values(status=IngestJobStatus.done, finished_at=datetime.now(timezone.utc))
            )
            await session.commit()

        get_metrics().documents_indexed.inc()
        get_metrics().chunks_indexed.inc(len(chunks))
        logger.info("indexed %s (%d chunks)", job.url, len(chunks))

        await _discover_and_enqueue(session_factory, job, page)
    except Exception as e:
        logger.warning("ingest failed for %s: %s", job.url, e)
        await _mark_job_failed(session_factory, job, str(e))


async def _mark_job_failed(session_factory: async_sessionmaker, job: IngestJob, error: str) -> None:
    settings = get_settings()
    async with session_factory() as session:
        if job.attempts < settings.worker_max_attempts:
            status = IngestJobStatus.pending
        else:
            status = IngestJobStatus.failed
        await session.execute(
            update(IngestJob)
            .where(IngestJob.id == job.id)
            .values(status=status, last_error=error, finished_at=datetime.now(timezone.utc))
        )
        await session.commit()
    if status == IngestJobStatus.pending:
        logger.info("requeued %s (attempt %d/%d)", job.url, job.attempts, settings.worker_max_attempts)


async def _process_sitemap(session_factory: async_sessionmaker, job: IngestJob) -> None:
    import xml.etree.ElementTree as ET

    from app.http import get_http_client

    try:
        resp = await get_http_client().get(job.url, timeout=30.0)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        urls = [loc.text for loc in root.iter("{*}loc") if loc.text]
        async with session_factory() as session:
            session.add_all(
                [
                    IngestJob(url=url, kind=IngestKind.url, status=IngestJobStatus.pending)
                    for url in urls[:500]
                ]
            )
            await session.execute(
                update(IngestJob)
                .where(IngestJob.id == job.id)
                .values(status=IngestJobStatus.done, finished_at=datetime.now(timezone.utc))
            )
            await session.commit()
        logger.info("enqueued %d urls from sitemap %s", min(len(urls), 500), job.url)
    except Exception as e:
        logger.warning("sitemap failed for %s: %s", job.url, e)
        await _mark_job_failed(session_factory, job, str(e))


async def ingest_worker_loop(session_factory: async_sessionmaker) -> None:
    settings = get_settings()
    while True:
        job = await _claim_job(session_factory)
        if job:
            await process_ingest_job(session_factory, job)
        else:
            await asyncio.sleep(settings.worker_ingest_poll_interval_seconds)


async def _claim_job(session_factory: async_sessionmaker) -> IngestJob | None:
    """Atomically claim one job (SKIP LOCKED) so multiple workers don't collide.

    Claims pending jobs, and re-claims ``running`` jobs a crashed worker left behind
    (``started_at`` older than ``worker_stale_job_seconds``).
    """
    settings = get_settings()
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.worker_stale_job_seconds)
    async with session_factory() as session:
        job = await session.scalar(
            select(IngestJob)
            .where(IngestJob.attempts < settings.worker_max_attempts)
            .where(
                or_(
                    IngestJob.status == IngestJobStatus.pending,
                    and_(
                        IngestJob.status == IngestJobStatus.running,
                        IngestJob.started_at < stale_cutoff,
                    ),
                )
            )
            .order_by(IngestJob.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.status = IngestJobStatus.running
        job.attempts += 1
        job.started_at = datetime.now(timezone.utc)
        await session.commit()
        return job


def extract_outbound_links(html: str, base_url: str) -> list[str]:
    """Extract absolute http(s) URLs from <a href> links in raw HTML."""
    links = []
    for m in _LINK_RE.finditer(html):
        href = html_unescape(m.group(1)).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        links.append(urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", "")))
    return links


def _hostname(url: str) -> str:
    return (urlsplit(url).netloc or "").lower()


def _allowed_by_domain_policy(policy: str, target_host: str, seed_host: str) -> bool:
    if policy == "all":
        return True
    if policy == "none":
        return False
    if policy == "same":
        return target_host == seed_host or target_host.endswith("." + seed_host)
    return False


async def _discover_and_enqueue(session_factory: async_sessionmaker, job: IngestJob, page) -> None:
    """Follow links found on a freshly indexed page (nested crawling)."""
    settings = get_settings()
    if not settings.crawler_follow_links or job.depth >= settings.crawler_max_depth:
        return
    try:
        html = page.content.decode("utf-8", errors="ignore")
    except Exception:
        return

    seed_host = _hostname(job.url)
    links = extract_outbound_links(html, page.final_url or job.url)
    if settings.crawler_follow_domains != "all":
        links = [
            u for u in links
            if _allowed_by_domain_policy(settings.crawler_follow_domains, _hostname(u), seed_host)
        ]

    robots = RobotsChecker()
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for u in links:
        if not await robots.allowed(u):
            continue
        norm = normalize_url(u)
        if norm in seen:
            continue
        seen.add(norm)
        candidates.append((u, norm))
        if len(candidates) >= settings.crawler_batch_size:
            break
    if not candidates:
        return

    norms = [n for _, n in candidates]
    new_jobs: list[IngestJob] = []
    async with session_factory() as session:
        indexed = set(
            (await session.scalars(select(Document.normalized_url).where(Document.normalized_url.in_(norms)))).all()
        )
        active = set(
            (await session.scalars(
                select(IngestJob.url).where(IngestJob.status.in_([IngestJobStatus.pending, IngestJobStatus.running]))
            )).all()
        )
        active_norms = {normalize_url(u) for u in active}
        new_jobs = [
            IngestJob(url=u, kind=IngestKind.url, depth=job.depth + 1, status=IngestJobStatus.pending)
            for u, n in candidates
            if n not in indexed and n not in active_norms
        ]
        session.add_all(new_jobs)
        await session.commit()
    if new_jobs:
        logger.info("crawler enqueued %d nested links from %s", len(new_jobs), job.url)


async def recrawl_worker_loop(session_factory: async_sessionmaker) -> None:
    """Continuously re-enqueue stale indexed documents for re-crawling."""
    settings = get_settings()
    while True:
        if settings.crawler_continuous_enabled:
            try:
                await _enqueue_stale_documents(session_factory)
            except Exception as e:
                logger.warning("recrawl scan failed: %s", e)
        await asyncio.sleep(settings.crawler_continuous_interval_seconds)


async def _enqueue_stale_documents(session_factory: async_sessionmaker) -> None:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.crawler_recrawl_after_seconds)
    async with session_factory() as session:
        stale = list(
            (
                await session.execute(
                    select(Document.url)
                    .where(Document.status == "indexed", Document.fetched_at < cutoff)
                    .limit(settings.crawler_recrawl_batch)
                )
            ).scalars().all()
        )
        if not stale:
            return
        active = set(
            (await session.scalars(
                select(IngestJob.url).where(IngestJob.status.in_([IngestJobStatus.pending, IngestJobStatus.running]))
            )).all()
        )
        active_norms = {normalize_url(u) for u in active}
        new_jobs = [
            IngestJob(url=u, kind=IngestKind.url, depth=0, status=IngestJobStatus.pending)
            for u in stale
            if u not in active and normalize_url(u) not in active_norms
        ]
        session.add_all(new_jobs)
        await session.commit()
    if new_jobs:
        logger.info("recrawler enqueued %d stale documents for refresh", len(new_jobs))
