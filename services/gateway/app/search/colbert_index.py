"""ColBERT indexing and late-interaction reranking."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.logging import get_logger
from app.models import Chunk, ChunkToken, Document
from app.schemas import SearchResult

logger = get_logger("search.colbert.index")


async def index_chunk_tokens(
    session_factory: async_sessionmaker,
    *,
    chunk_id,
    text: str,
) -> int:
    """Encode a chunk's tokens and replace any vectors already stored for it."""
    from app.search import colbert

    if not get_settings().colbert_enabled or not colbert.available():
        return 0

    vectors = await colbert.encode_documents([text])
    tokens = vectors[0] if vectors else []
    if not tokens:
        return 0

    async with session_factory() as session:
        # Primary key is (chunk_id, position), so a re-index without this delete
        # would collide on any chunk that shrank after retokenising.
        await session.execute(delete(ChunkToken).where(ChunkToken.chunk_id == chunk_id))
        session.add_all(ChunkToken(chunk_id=chunk_id, position=i, embedding=vec) for i, vec in enumerate(tokens))
        await session.commit()
    return len(tokens)


async def colbert_rerank(
    session_factory: async_sessionmaker,
    query: str,
    candidates: list[SearchResult],
    *,
    enabled: bool | None = None,
) -> list[SearchResult]:
    """Rescore document-level candidates with MaxSim and reorder them.

    The local index searches at *document* level, so candidates arrive as URLs
    with no chunk attached. Rather than restructure retrieval to carry chunk ids
    through fusion, each candidate's chunks are scored and the best one stands
    in for the document — a long page usually has one passage that answers the
    query, and that is the one MaxSim should see.

    Only the top ``colbert_top_n`` are rescored: MaxSim is a Python matrix
    product per chunk, and rescoring the whole corpus to reorder a page of
    results would cost far more than the ordering is worth.
    """
    settings = get_settings()
    # `enabled` is the caller's per-request override. Without this parameter the
    # function re-read the server setting and returned immediately, so a
    # per-request colbert=true was computed by the orchestrator and then thrown
    # away — the toggle silently did nothing.
    if (enabled if enabled is not None else settings.colbert_enabled) is False:
        return candidates
    if not candidates:
        return candidates

    from app.search import colbert

    q = await colbert.encode_query(query)
    if not q:
        return candidates

    head = candidates[: settings.colbert_top_n]
    tail = candidates[settings.colbert_top_n :]

    urls = [r.url for r in head]
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ChunkToken.chunk_id, ChunkToken.embedding).where(
                    ChunkToken.chunk_id.in_(
                        select(Chunk.id).join(Document, Document.id == Chunk.document_id).where(Document.url.in_(urls))
                    )
                )
            )
        ).all()
        owners = dict(
            (
                await session.execute(
                    select(Chunk.id, Document.url)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(Document.url.in_(urls))
                )
            ).all()
        )

    by_url: dict[str, list[list[float]]] = {}
    for chunk_id, vector in rows:
        url = owners.get(chunk_id)
        if url:
            by_url.setdefault(url, []).append(vector)

    rescored = []
    for result in head:
        # Best-matching chunk stands in for the document. Pooling the whole
        # document's tokens into one MaxSim would reward length, which is the
        # opposite of what token-level matching is for.
        tokens = by_url.get(result.url)
        score = colbert.maxsim(q, tokens) if tokens else None
        result.colbert_score = score
        rescored.append(result)

    scored = [r for r in rescored if r.colbert_score is not None]
    unscored = [r for r in rescored if r.colbert_score is None]
    # Blend rather than replace, and then sort on the *blended* value. Sorting
    # on colbert_score while reporting a blended score produced results whose
    # order contradicted their own scores — the top hit carrying the lowest
    # score, which breaks any caller that filters or thresholds on score.
    for result in scored:
        result.score = result.score + settings.colbert_weight * result.colbert_score
    scored.sort(key=lambda r: r.score, reverse=True)
    # One list, sorted on the final reported score. An earlier version put
    # unscored items after scored ones to avoid "demoting" them, which produced
    # rank=2 with score 7.64 above rank=3 with score 7.92 — the response
    # contradicted its own ordering, and any caller filtering or thresholding on
    # score got a different answer than the ranking implies. Unscored items are
    # never dropped, they simply sort on their unchanged cross-encoder score.
    out = scored + unscored + tail
    out.sort(key=lambda r: r.score, reverse=True)
    for position, result in enumerate(out, start=1):
        result.rank = position
    return out
