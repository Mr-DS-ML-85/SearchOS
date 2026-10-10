from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from html import unescape as html_unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.embed.chunker import chunk_text, estimate_tokens
from app.embed.provider import get_embedding_provider
from app.extract.extractor import extract_page
from app.extract.fetcher import fetch_page
from app.extract.robots import RobotsChecker
from app.logging import get_logger
from app.metrics import get_metrics
from app.models import Chunk, Document, IngestJob, IngestJobStatus, IngestKind
from app.search.langdetect import detect as detect_language
from app.search.politeness import wait_for_origin
from app.search.simhash import simhash
from app.tsvector import build_language_tsv

logger = get_logger("workers.ingest")

_LINK_RE = re.compile(r'<a[^>]+href=["\']\s*([^"\' >\s]+)["\']', re.IGNORECASE)


# Query parameters that identify a click, not a document. Everything else in
# the query string is kept: it often *is* the document (?id=, ?v=, ?p=).
_TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "ref_src",
    }
)


def normalize_url(url: str) -> str:
    """Canonical key for dedupe, shared by the index and search-time fusion.

    - host lowercased, ``www.`` and default ports dropped;
    - scheme kept: an http-only site may differ from its https twin, so they
      are deliberately not merged;
    - trailing slash and fragment dropped;
    - query string **kept**, minus tracking parameters, sorted.

    The query used to be dropped entirely, so every
    ``help.salesforce.com/s/articleView?id=...`` article, every Hacker News
    ``item?id=`` and every YouTube ``watch?v=`` collapsed into one document — the
    second overwrote the first in the index, and fusion merged them into a single
    search result.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit

    parsed = urlsplit(url.strip())
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = (parsed.path or "").rstrip("/")
    params = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    key = f"{scheme}://{host}{path}"
    if params:
        key += "?" + urlencode(params)
    return key


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y/%m/%d",
    "%d %B %Y",
    "%B %d, %Y",
    "%d %b %Y",
)


def _parse_published(value: object) -> datetime | None:
    """Coerce trafilatura's ``published`` metadata into a datetime.

    trafilatura hands back free-form strings ("2001-08-25", "25 August 2001"),
    but ``documents.published_at`` is timestamptz. Passing the string straight
    through made asyncpg raise and failed the whole ingest — which is how a
    previously hardcoded ``published_at=None`` (that silently discarded the date)
    came to be.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)

    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    logger.debug("unrecognised published date %r; storing NULL", text)
    return None


async def process_ingest_job(session_factory: async_sessionmaker, job: IngestJob) -> None:
    if job.kind == IngestKind.sitemap:
        await _process_sitemap(session_factory, job)
        return

    try:
        await wait_for_origin(job.url)
        page = await fetch_page(job.url)
        extracted = extract_page(page)
        content = extracted.content
        if not content.strip():
            raise ValueError("empty extracted content")

        await index_document(
            session_factory,
            url=job.url,
            final_url=page.final_url,
            extracted=extracted,
            source="ingest",
            job_id=job.id,
        )

        await _index_page_images(session_factory, job, page, content)

        await _discover_and_enqueue(session_factory, job, page)

        # The corpus just changed, so any cached search result is now stale.
        # Without this, a re-crawl was invisible for up to
        # search_cache_ttl_seconds and the only symptom was empty results served
        # with `cache_hit: true, took_ms: 0` — a stale answer that looks healthy.
        try:
            from app.cache import get_cache

            await get_cache().invalidate("search:*")
        except Exception:
            pass
    except Exception as e:
        logger.warning("ingest failed for %s: %s", job.url, e)
        await _mark_job_failed(session_factory, job, str(e))


async def index_document(
    session_factory: async_sessionmaker,
    *,
    url: str,
    final_url: str,
    extracted,
    source: str,
    job_id: uuid.UUID | None = None,
    max_chars: int | None = None,
) -> int:
    """Write one extracted page as a fully indexed document. Returns chunk count.

    The only place a document's content and everything derived from it (lang,
    tsv, simhash, hash, chunks, vectors) is written together. The snippet
    enricher used to replace content_text on its own and derive nothing, so a
    page grew from a 160-char snippet to 30k chars while its chunks, and with
    them vector search, still described the snippet — permanently, because the
    backfill only chunks documents that have no chunks at all.
    """
    from app.search.providers.searxng import SNIPPET_META_FLAG

    settings = get_settings()
    content = extracted.content if max_chars is None else extracted.content[:max_chars]
    doc = Document(
        id=uuid.uuid4(),
        url=url,
        normalized_url=normalize_url(final_url),
        title=extracted.title,
        content_text=content,
        content_html=None,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        source=source,
        author=extracted.author,
        # Previously hardcoded to None, so extracted publish dates were
        # parsed and discarded and the reranker's freshness boost never
        # fired for crawled documents.
        published_at=_parse_published(extracted.published),
        fetched_at=datetime.now(UTC),
        status="indexed",
    )

    # trafilatura reports a language for PDFs but returns None for most
    # HTML, so every document was landing with lang=NULL and being stemmed
    # with `english` regardless of what it was written in.
    language = extracted.language or detect_language(f"{extracted.title or ''} {content[:4000]}")
    doc.lang = language
    doc.tsv = await build_language_tsv(language, f"{extracted.title or ''} {content}")
    doc.simhash = simhash(content)

    chunks = chunk_text(
        content,
        max_tokens=settings.chunk_max_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )
    vectors: list[list[float]] | None = None
    embedding = get_embedding_provider()
    embedding_skipped_reason: str | None = None
    if embedding.available:
        try:
            vectors = await embedding.embed(chunks)
        except Exception as e:
            # Non-fatal: the document is still FTS-searchable. Count it so a
            # silent embedding outage is visible, and re-queue a backfill.
            embedding_skipped_reason = "error"
            logger.warning("embedding failed for %s: %s", url, e)
            get_metrics().embedding_failures.inc()
    else:
        # The embedder is not installed / not loaded. Previously this was a
        # completely silent branch: documents were written with
        # status='indexed' and zero vectors, the vector provider reported
        # itself unavailable, and nothing anywhere said so. A search index
        # with no vectors still answers queries — just lexically — which is
        # exactly the kind of degradation nobody notices.
        embedding_skipped_reason = "unavailable"
        get_metrics().embedding_skipped.labels(reason="unavailable").inc()
        logger.warning(
            "indexing %s without embeddings: the embedding provider is unavailable "
            "(is sentence-transformers installed? set INSTALL_EMBEDDINGS=1 to build it). "
            "Retrieval will fall back to keyword-only for this and all other documents.",
            url,
        )

    async with session_factory() as session:
        existing = await session.scalar(
            select(Document).where(Document.normalized_url == doc.normalized_url).options(selectinload(Document.chunks))
        )
        if existing:
            for chunk in existing.chunks:
                await session.delete(chunk)
            existing.title = doc.title
            existing.content_text = doc.content_text
            existing.content_hash = doc.content_hash
            existing.lang = doc.lang
            existing.tsv = doc.tsv
            existing.simhash = doc.simhash
            existing.status = "indexed"
            existing.fetched_at = doc.fetched_at
            existing.published_at = doc.published_at or existing.published_at
            existing.author = doc.author or existing.author
            # The SearXNG saver overwrites any row still flagged as a snippet
            # (ON CONFLICT ... WHERE searxng_snippet), so a fetched page that
            # kept the flag was reverted to a 300-char snippet by the next
            # search that returned it.
            meta = dict(existing.meta or {})
            if meta.pop(SNIPPET_META_FLAG, None) is not None:
                existing.meta = meta
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
        if job_id is not None:
            await session.execute(
                update(IngestJob)
                .where(IngestJob.id == job_id)
                .values(status=IngestJobStatus.done, finished_at=datetime.now(UTC))
            )
        await session.commit()

    get_metrics().documents_indexed.inc()
    get_metrics().chunks_indexed.inc(len(chunks))
    if embedding_skipped_reason:
        get_metrics().documents_without_vectors.inc()
    if vectors is None and embedding_skipped_reason is None:
        get_metrics().embedding_skipped.labels(reason="no_vectors").inc()
    logger.info(
        "indexed %s (%d chunks, %s)",
        url,
        len(chunks),
        f"{len(vectors)} vectors" if vectors else "no vectors (keyword-only)",
    )
    return len(chunks)


def _is_permanent_error(error: str) -> bool:
    """Decide whether re-trying could ever succeed.

    Every failure used to consume all ``worker_max_attempts`` with exponential
    backoff, so a plain 404 sat in ``pending`` for a minute before finally
    failing. The symptom is exactly the one reported in the bug report: a job
    that is never going to succeed keeps looking like it might, and ``pending``
    becomes indistinguishable from a genuinely slow origin.

    Retry is pointless when the origin has definitively answered.
    """
    text = (error or "").lower()

    if "http 404" in text or "http 410" in text or "http 401" in text or "http 403" in text:
        return True
    if "page not found" in text or "blocked by robots" in text:
        return True
    if "unsupported content type" in text or "cannot extract content" in text:
        return True
    if "refused by egress policy" in text:
        return True
    if "invalid url" in text or "no address associated" in text:
        return True
    return False


async def _mark_job_failed(session_factory: async_sessionmaker, job: IngestJob, error: str) -> None:
    """Requeue with exponential backoff, or fail permanently.

    Previously a failed job returned to ``pending`` with its original
    ``created_at``, so it was immediately re-claimed (still the oldest row) and
    all three attempts burned within milliseconds. Retry-After from the origin
    is not modelled, but the backoff prevents a hot retry loop against a flaky
    origin. Permanent failures skip the retries entirely.
    """
    settings = get_settings()
    permanent = _is_permanent_error(error)
    async with session_factory() as session:
        if permanent:
            status = IngestJobStatus.failed
            delay = 0
        elif job.attempts < settings.worker_max_attempts:
            status = IngestJobStatus.pending
            delay = settings.worker_retry_base_seconds * (2 ** (max(job.attempts, 1) - 1))
        else:
            status = IngestJobStatus.failed
            delay = 0
        values = {"status": status, "last_error": error[:2000]}
        if status == IngestJobStatus.pending:
            # finished_at is only meaningful for a terminal state.
            values["next_attempt_at"] = datetime.now(UTC) + timedelta(seconds=delay)
        else:
            values["finished_at"] = datetime.now(UTC)
        await session.execute(update(IngestJob).where(IngestJob.id == job.id).values(**values))
        await session.commit()
    if status == IngestJobStatus.pending:
        logger.info(
            "requeued %s (attempt %d/%d, retry in %ds)",
            job.url,
            job.attempts,
            settings.worker_max_attempts,
            delay,
        )
    else:
        if permanent:
            logger.info(
                "permanent failure for %s on attempt %d, not retrying: %s",
                job.url,
                job.attempts,
                error,
            )
        else:
            logger.warning("giving up on %s after %d attempts: %s", job.url, job.attempts, error)


def _parse_sitemap(payload: bytes) -> list[str]:
    """Extract <loc> values from a sitemap.

    Uses defusedxml when available: ``xml.etree.ElementTree`` is vulnerable to
    entity-expansion ("billion laughs") and external-entity attacks, and a
    sitemap is attacker-supplied input. Falls back to ElementTree with entity
    resolution disabled if defusedxml is not installed.
    """
    try:
        from defusedxml.ElementTree import fromstring as safe_fromstring

        root = safe_fromstring(payload)
    except ImportError:  # pragma: no cover - defusedxml is a hard dependency
        import xml.etree.ElementTree as ET

        root = ET.fromstring(payload)  # noqa: S314 - defusedxml is a hard dependency
    return [loc.text.strip() for loc in root.iter("{*}loc") if loc.text and loc.text.strip()]


async def _process_sitemap(session_factory: async_sessionmaker, job: IngestJob) -> None:
    from app.http import get_http_client
    from app.netguard import BlockedURLError, validate_url

    try:
        # Sitemaps used to be fetched with the raw shared client, bypassing
        # robots, the content-type check, the size limit and the SSRF guard.
        validate_url(job.url)
        await wait_for_origin(job.url)
        resp = await get_http_client().get(job.url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        if len(resp.content) > get_settings().extract_max_bytes * 4:
            raise ValueError("sitemap exceeds the configured size limit")
        urls = _parse_sitemap(resp.content)
        limit = get_settings().crawler_max_urls_per_seed
        urls = urls[:limit]
        kept: list[str] = []
        for u in urls:
            try:
                validate_url(u)
            except BlockedURLError:
                continue
            kept.append(u)
        async with session_factory() as session:
            session.add_all([IngestJob(url=url, kind=IngestKind.url, status=IngestJobStatus.pending) for url in kept])
            await session.execute(
                update(IngestJob)
                .where(IngestJob.id == job.id)
                .values(status=IngestJobStatus.done, finished_at=datetime.now(UTC))
            )
            await session.commit()
        logger.info("enqueued %d urls from sitemap %s", len(kept), job.url)
    except Exception as e:
        logger.warning("sitemap failed for %s: %s", job.url, e)
        await _mark_job_failed(session_factory, job, str(e))


async def ingest_worker_loop(session_factory: async_sessionmaker) -> None:
    """Serial ingest loop.

    A DB error here is caught and logged: an unhandled exception would propagate
    out of asyncio.gather in the runner and kill *all three* worker loops, not
    just ingest.
    """
    settings = get_settings()
    while True:
        try:
            job = await _claim_job(session_factory)
            if job:
                await process_ingest_job(session_factory, job)
            else:
                await asyncio.sleep(settings.worker_ingest_poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("ingest loop error: %s: %s", e.__class__.__name__, e)
            await asyncio.sleep(settings.worker_ingest_poll_interval_seconds)


async def _claim_job(session_factory: async_sessionmaker) -> IngestJob | None:
    """Atomically claim one job (SKIP LOCKED) so multiple workers don't collide.

    Claims pending jobs, and re-claims ``running`` jobs a crashed worker left behind
    (``started_at`` older than ``worker_stale_job_seconds``).
    """
    settings = get_settings()
    stale_cutoff = datetime.now(UTC) - timedelta(seconds=settings.worker_stale_job_seconds)
    due_cutoff = datetime.now(UTC)
    async with session_factory() as session:
        job = await session.scalar(
            select(IngestJob)
            .where(IngestJob.attempts < settings.worker_max_attempts)
            .where(
                or_(
                    and_(
                        IngestJob.status == IngestJobStatus.pending,
                        or_(
                            IngestJob.next_attempt_at.is_(None),
                            IngestJob.next_attempt_at <= due_cutoff,
                        ),
                    ),
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
        job.started_at = datetime.now(UTC)
        await session.commit()
        return job


def extract_outbound_links(html: str, base_url: str) -> list[str]:
    """Extract absolute http(s) URLs from real ``<a href>`` attributes.

    The previous regex scanned raw HTML, so it also matched links inside HTML
    comments and inside <script> string literals. We now drop comment, script,
    style and noscript regions before matching.
    """
    links = []
    for href in _iter_hrefs(html):
        href = html_unescape(href).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        links.append(urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", "")))
    return links


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)


def _strip_non_dom(html: str) -> str:
    html = _COMMENT_RE.sub(" ", html)
    return _SCRIPT_RE.sub(" ", html)


def _iter_hrefs(html: str) -> list[str]:
    for m in _HREF_RE.finditer(_strip_non_dom(html)):
        yield next((g for g in m.groups() if g is not None), "")


def _egress_allowed(url: str) -> bool:
    """Sync egress check for discovered links (no DNS lookup)."""
    from app.netguard import BlockedURLError, validate_url

    try:
        validate_url(url, check_dns=False)
    except BlockedURLError:
        return False
    return True


def _hostname(url: str) -> str:
    """Hostname without port, lowercased, for domain-policy comparisons."""
    return (urlsplit(url).hostname or "").lower()


def _allowed_by_domain_policy(policy: str, target_host: str, seed_host: str) -> bool:
    if policy == "all":
        return True
    if policy == "none":
        return False
    if policy == "same":
        return target_host == seed_host or target_host.endswith("." + seed_host)
    return False


async def _index_page_images(session_factory, job, page, content: str) -> None:
    """Embed the images on a freshly indexed page. Best effort, never fatal."""
    settings = get_settings()
    if not settings.images_enabled:
        return
    try:
        html = page.content.decode("utf-8", errors="ignore")
    except Exception:
        return
    try:
        async with session_factory() as session:
            from app.models import Document

            doc_id = await session.scalar(select(Document.id).where(Document.url == job.url))
    except Exception:
        return
    if not doc_id:
        return
    try:
        from app.search.image_index import index_images_for_page

        await index_images_for_page(
            session_factory,
            document_id=doc_id,
            source_url=job.url,
            html=html,
        )
    except Exception as exc:
        logger.debug("image indexing failed for %s: %s", job.url, exc)


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
            u for u in links if _allowed_by_domain_policy(settings.crawler_follow_domains, _hostname(u), seed_host)
        ]
    # A discovered link is attacker-controlled input; refuse internal targets
    # before they ever become a job.
    links = [u for u in links if _egress_allowed(u)]
    if not links:
        return

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
            (
                await session.scalars(
                    select(IngestJob.url).where(
                        IngestJob.status.in_([IngestJobStatus.pending, IngestJobStatus.running])
                    )
                )
            ).all()
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
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.crawler_recrawl_after_seconds)
    async with session_factory() as session:
        stale = list(
            (
                await session.execute(
                    select(Document.url)
                    .where(Document.status == "indexed", Document.fetched_at < cutoff)
                    .order_by(Document.fetched_at.asc())
                    .limit(settings.crawler_recrawl_batch)
                )
            )
            .scalars()
            .all()
        )
        if not stale:
            return
        active = set(
            (
                await session.scalars(
                    select(IngestJob.url).where(
                        IngestJob.status.in_([IngestJobStatus.pending, IngestJobStatus.running])
                    )
                )
            ).all()
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
