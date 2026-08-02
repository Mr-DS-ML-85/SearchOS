from __future__ import annotations

import re
import time

from sqlalchemy import text

from app.config import get_settings
from app.metrics import get_metrics
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.schemas import SearchResult

class LocalIndexProvider(SearchProvider):
    name = "local"

    def __init__(self, session_factory) -> None:
        self.settings = get_settings()
        self.session_factory = session_factory

    def _domain_filters(self, query: ProviderQuery) -> tuple[str, dict]:
        sql = ""
        params: dict = {}
        if query.domains:
            clauses = " OR ".join(f"d.normalized_url LIKE :dom{i}" for i in range(len(query.domains)))
            sql += f" AND ({clauses})"
            for i, d in enumerate(query.domains):
                params[f"dom{i}"] = f"%{d}%"
        if query.exclude_domains:
            for i, d in enumerate(query.exclude_domains):
                sql += f" AND d.normalized_url NOT LIKE :excl{i}"
                params[f"excl{i}"] = f"%{d}%"
        return sql, params

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)
        try:
            rows = await self._fts_search(query)
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="fts").inc()
            result.error = f"local index error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        for rank, row in enumerate(rows, start=1):
            fts_score = float(row["fts_score"] or 0.0)
            content = row["content_text"] or ""
            result.results.append(
                SearchResult(
                    title=row["title"] or row["url"],
                    url=row["url"],
                    snippet=content[:300] if content else "",
                    source=self.name,
                    score=fts_score * self.settings.local_index_fts_weight,
                    rank=rank,
                    published=row["published_at"].isoformat() if row["published_at"] else None,
                    domain=row["source"],
                )
            )

        result.latency_ms = (time.monotonic() - start) * 1000
        get_metrics().search_duration.labels(provider=self.name).observe(result.latency_ms / 1000)
        return result

    async def _fts_search(self, query: ProviderQuery, *, or_fallback: bool = True) -> list:
        domain_sql, domain_params = self._domain_filters(query)
        base = """
            SELECT
                d.url, d.title, d.content_text, d.published_at, d.source, d.fetched_at,
                ts_rank_cd(
                    to_tsvector('english', coalesce(d.title,'') || ' ' || coalesce(d.content_text,'')),
                    to_tsquery('english', :tsq)
                ) AS fts_score
            FROM documents d
            WHERE d.status = 'indexed'
              AND to_tsvector('english', coalesce(d.title,'') || ' ' || coalesce(d.content_text,''))
                  @@ to_tsquery('english', :tsq)
            """
        order = """
            ORDER BY fts_score DESC, d.fetched_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
            """
        params = {"limit": query.num_results, "offset": query.offset}
        params.update(domain_params)
        tsq = self._build_tsquery(query.q, "AND")
        if not tsq:
            return []
        async with self.session_factory() as session:
            modes = ("AND", "OR") if or_fallback else ("AND",)
            for mode in modes:
                tsq = self._build_tsquery(query.q, mode)
                sql = text(base + domain_sql + order)
                rows = (
                    await session.execute(sql, {**params, "tsq": tsq})
                ).mappings().all()
                if rows:
                    break
        return rows

    def _build_tsquery(self, q: str, mode: str) -> str:
        terms = [t for t in re.findall(r"[a-zA-Z0-9_]+", q.lower()) if len(t) > 1]
        if not terms:
            return ""
        return f" {'&' if mode == 'AND' else '|'} ".join(terms)
