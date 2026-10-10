"""Backfill vectors for content that predates the embedding provider.

Ingest writes embeddings as a side effect of fetching a page. Anything indexed
while the provider was unavailable — an image built without ``INSTALL_EMBEDDINGS``,
a worker running with torch missing, a corpus crawled before vectors existed —
keeps its chunks but never gains a vector. There is no re-fetch of those rows, so
they stay invisible to semantic search forever while the index reports itself
healthy, because ``search`` keeps answering them lexically.

This module closes that gap without re-crawling: it finds rows that are missing
vectors and fills them in place.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.embed.chunker import chunk_text, estimate_tokens
from app.embed.provider import get_embedding_provider
from app.logging import get_logger
from app.metrics import get_metrics
from app.models import Chunk, Document

logger = get_logger("workers.backfill")


async def _embed_all(embedding, texts: list[str]) -> list[list[float]] | None:
    """Embed ``texts`` in batches, returning None if the provider is unusable."""
    if not texts:
        return []
    if not embedding.available:
        return None
    settings = get_settings()
    vectors: list[list[float]] = []
    step = max(settings.embedding_batch_size, 1)
    for start in range(0, len(texts), step):
        batch = texts[start : start + step]
        try:
            vectors.extend(await embedding.embed(batch))
        except Exception as exc:
            get_metrics().embedding_failures.inc()
            logger.warning("backfill embedding failed mid-batch (%d/%d): %s", start, len(texts), exc)
            return None
    return vectors


async def backfill_document_chunks(session_factory: async_sessionmaker, *, limit: int) -> int:
    """Chunk and embed indexed documents that have no chunk rows at all.

    These come from the search-time save path, which writes the document but
    leaves chunking to the ingest worker.
    """
    settings = get_settings()
    embedding = get_embedding_provider()
    if not embedding.available:
        return 0

    async with session_factory() as session:
        docs = list(
            (
                await session.scalars(
                    select(Document)
                    .where(Document.status == "indexed")
                    .where(Document.content_text.is_not(None))
                    .where(~Document.chunks.any())
                    .order_by(Document.fetched_at.asc())
                    .limit(limit)
                )
            ).all()
        )

    pending: list[tuple[Document, list[str]]] = []
    for doc in docs:
        text = (doc.content_text or "").strip()
        if not text:
            continue
        pending.append(
            (
                doc,
                chunk_text(
                    text,
                    max_tokens=settings.chunk_max_tokens,
                    overlap_tokens=settings.chunk_overlap_tokens,
                ),
            )
        )
    if not pending:
        return 0

    vectors = await _embed_all(embedding, [c for _, chunks in pending for c in chunks])
    if vectors is None:
        return 0

    written = 0
    cursor = 0
    async with session_factory() as session:
        for doc, chunks in pending:
            # Re-check under the write session: the ingest worker may have
            # chunked this document between our read and this commit, and
            # duplicating chunks would corrupt the document's ordering.
            existing = await session.scalar(select(Chunk.id).where(Chunk.document_id == doc.id).limit(1))
            if existing is not None:
                continue
            for index, content in enumerate(chunks):
                session.add(
                    Chunk(
                        document_id=doc.id,
                        index=index,
                        content=content,
                        token_count=estimate_tokens(content),
                        embedding=vectors[cursor + index],
                    )
                )
                written += 1
            cursor += len(chunks)
        await session.commit()

    if written:
        get_metrics().chunks_indexed.inc(written)
        logger.info("backfilled %d chunks across %d documents", written, len(pending))
    return written


async def backfill_missing_vectors(session_factory: async_sessionmaker, *, limit: int) -> int:
    """Embed chunk rows that exist but carry no vector."""
    embedding = get_embedding_provider()
    if not embedding.available:
        return 0

    async with session_factory() as session:
        chunks = list(
            (
                await session.scalars(
                    select(Chunk)
                    .where(Chunk.embedding.is_(None))
                    .order_by(Chunk.document_id.asc(), Chunk.index.asc())
                    .limit(limit)
                )
            ).all()
        )
    if not chunks:
        return 0

    vectors = await _embed_all(embedding, [c.content for c in chunks])
    if vectors is None:
        return 0

    async with session_factory() as session:
        for chunk, vector in zip(chunks, vectors, strict=True):
            await session.execute(update(Chunk).where(Chunk.id == chunk.id).values(embedding=vector))
        await session.commit()

    logger.info("backfilled %d chunk vectors", len(chunks))
    return len(chunks)


async def run_backfill(session_factory: async_sessionmaker) -> dict[str, int]:
    """One full pass. Returns counts so callers can log or report progress."""
    settings = get_settings()
    embedding = get_embedding_provider()
    if not embedding.available:
        return {"chunks_created": 0, "vectors_filled": 0}

    created = await backfill_document_chunks(session_factory, limit=settings.backfill_batch_size)
    filled = await backfill_missing_vectors(session_factory, limit=settings.backfill_batch_size)
    return {"chunks_created": created, "vectors_filled": filled}


async def backfill_loop(session_factory: async_sessionmaker) -> None:
    """Drain missing vectors at startup and then on an interval.

    Startup matters: the common case is an operator enabling embeddings on an
    existing corpus, and waiting an hour for the first pass would look like the
    rebuild did nothing.

    While work remains the loop keeps cycling rather than sleeping the full
    interval, because the alternative was clearing 363 chunk-less documents at
    50 per hour — roughly seven hours before vector search covered the corpus.
    """
    settings = get_settings()
    while True:
        try:
            result = await run_backfill(session_factory)
            worked = any(result.values())
            if worked:
                logger.info("backfill pass: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("backfill pass failed: %s: %s", exc.__class__.__name__, exc)
            worked = False
        if worked:
            await asyncio.sleep(settings.backfill_drain_interval_seconds)
        else:
            await asyncio.sleep(settings.backfill_interval_seconds)
