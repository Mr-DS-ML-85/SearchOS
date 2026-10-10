"""ColBERT indexing and late-interaction reranking."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.logging import get_logger
from app.models import ChunkToken

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
    candidates: list[dict],
) -> list[dict]:
    """Rescore candidates with MaxSim and reorder them.

    Operates on the already-retrieved top-N rather than the whole index:
    MaxSim is a Python matrix product per chunk, and rescoring 7000 chunks to
    reorder 50 would cost far more than the ordering is worth.
    """
    settings = get_settings()
    if not settings.colbert_enabled or not candidates:
        return candidates

    from app.search import colbert

    q = await colbert.encode_query(query)
    if not q:
        return candidates

    # MaxSim is a Python matrix product per chunk; the tail of a long candidate
    # list costs more than its contribution to the final ordering.
    candidates = candidates[: settings.colbert_top_n]
    chunk_ids = [c.get("chunk_id") for c in candidates if c.get("chunk_id")]
    if not chunk_ids:
        return candidates

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ChunkToken.chunk_id, ChunkToken.embedding).where(ChunkToken.chunk_id.in_(chunk_ids))
            )
        ).all()

    by_chunk: dict[object, list[list[float]]] = {}
    for chunk_id, vector in rows:
        by_chunk.setdefault(chunk_id, []).append(vector)

    for candidate in candidates:
        tokens = by_chunk.get(candidate.get("chunk_id"))
        candidate["colbert_score"] = colbert.maxsim(q, tokens) if tokens else None

    # Chunks with no ColBERT vectors keep their existing order behind the scored
    # ones rather than being dropped: a missing vector is an indexing gap, not
    # evidence the passage is irrelevant.
    scored = [c for c in candidates if c.get("colbert_score") is not None]
    unscored = [c for c in candidates if c.get("colbert_score") is None]
    scored.sort(key=lambda c: c["colbert_score"], reverse=True)
    return scored + unscored
