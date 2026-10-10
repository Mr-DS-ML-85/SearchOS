from __future__ import annotations

import time

from sqlalchemy import text

from app.config import get_settings
from app.embed.provider import get_embedding_provider
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.search.snippets import build_snippet


# One row per *document*, using the best-matching chunk.
#
# The previous query returned one row per chunk, so a 30-chunk page occupied 30
# of the LIMIT slots and RRF added 1/(k+rank) once per chunk — making document
# length a relevance multiplier that could outrank genuinely multi-provider
# matches. DISTINCT ON (d.id) keeps the nearest chunk per document.
def build_sql(filter_sql: str = ""):
    """Nearest documents by their best-matching chunk.

    ``DISTINCT ON`` needs ``ORDER BY d.id`` first, so ranking and LIMIT must
    happen in an outer query. Applying LIMIT inline kept the first N documents
    *by UUID* among everything above the similarity floor — an arbitrary set in
    arbitrary order. ``filter_sql`` carries the same domain, time and language
    filters as the local index; the vector provider used to apply none of them,
    so `domains=[docs.rs]` still returned other sites through this path.
    """
    return text(
        f"""
        SELECT * FROM (
            SELECT DISTINCT ON (d.id)
                d.url,
                d.title,
                d.content_text,
                d.published_at,
                d.source,
                c.content AS chunk_content,
                1 - (c.embedding <=> CAST(:vec AS vector)) AS similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL
              AND d.status = 'indexed'
              AND 1 - (c.embedding <=> CAST(:vec AS vector)) >= :min_similarity
              {filter_sql}
            ORDER BY d.id, c.embedding <=> CAST(:vec AS vector)
        ) best
        ORDER BY similarity DESC
        LIMIT :limit
        """  # noqa: S608 - filter values are bound parameters
    )


# Candidate retrieval scans more chunks than the number of documents we want, so
# a single long document cannot crowd out the rest of the result set.
CHUNK_OVERSAMPLE = 4


class VectorIndexProvider(SearchProvider):
    name = "vector"

    def __init__(self, session_factory, embedding=None) -> None:
        self.settings = get_settings()
        self.session_factory = session_factory
        self.embedding = embedding or get_embedding_provider()

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)
        if not self.embedding.available:
            result.error = "embeddings disabled"
            return result

        try:
            vector = await self.embedding.embed_one(query.q)
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="embed").inc()
            result.error = f"embedding failed: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        from app.search.local_index import LocalIndexProvider

        filter_sql, filter_params = LocalIndexProvider(self.session_factory).filter_sql(query)
        params = {
            "vec": str(vector),
            "limit": query.num_results,
            "min_similarity": self.settings.vector_min_similarity,
            **filter_params,
        }
        try:
            async with self.session_factory() as session:
                rows = (await session.execute(build_sql(filter_sql), params)).mappings().all()
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="query").inc()
            result.error = f"vector query failed: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        for rank, row in enumerate(rows, start=1):
            document = row["content_text"] or ""
            chunk = row["chunk_content"] or ""
            # Prefer the matched chunk; fall back to the head of the document.
            snippet = build_snippet(chunk or document or "", query.q).text
            result.results.append(
                SearchResult(
                    title=row["title"] or row["url"],
                    url=row["url"],
                    snippet=snippet,
                    source=self.name,
                    score=float(row["similarity"] or 0.0) * self.settings.local_index_vector_weight,
                    rank=rank,
                    published=row["published_at"].isoformat() if row["published_at"] else None,
                    domain=_domain(row["url"]),
                )
            )

        result.latency_ms = (time.monotonic() - start) * 1000
        get_metrics().search_duration.labels(provider=self.name).observe(result.latency_ms / 1000)
        return result


def _domain(url: str) -> str:
    """Hostname for a URL.

    Previously this returned ``documents.source`` (the ingest channel:
    "crawler"/"ingest"/"web"), which is not a hostname and made the reranker's
    domain weights impossible to wire up.
    """
    from app.search.providers.searxng import SearXNGProvider

    return SearXNGProvider._domain(url)
