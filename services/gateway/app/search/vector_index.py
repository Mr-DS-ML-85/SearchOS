from __future__ import annotations

import time

from sqlalchemy import text

from app.config import get_settings
from app.embed.provider import get_embedding_provider
from app.metrics import get_metrics
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.schemas import SearchResult


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

        sql = text(
            """
            SELECT
                d.url, d.title, d.content_text, d.published_at, d.source,
                1 - (c.embedding <=> CAST(:vec AS vector)) AS similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL AND d.status = 'indexed'
            ORDER BY c.embedding <=> CAST(:vec AS vector)
            LIMIT :limit
            """
        )
        params = {"vec": str(vector), "limit": query.num_results}
        try:
            async with self.session_factory() as session:
                rows = (await session.execute(sql, params)).mappings().all()
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="query").inc()
            result.error = f"vector query failed: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        for rank, row in enumerate(rows, start=1):
            content = row["content_text"] or ""
            result.results.append(
                SearchResult(
                    title=row["title"] or row["url"],
                    url=row["url"],
                    snippet=content[:300] if content else "",
                    source=self.name,
                    score=float(row["similarity"] or 0.0) * self.settings.local_index_vector_weight,
                    rank=rank,
                    published=row["published_at"].isoformat() if row["published_at"] else None,
                    domain=row["source"],
                )
            )

        result.latency_ms = (time.monotonic() - start) * 1000
        return result
