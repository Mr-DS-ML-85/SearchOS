from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.workers.ingest import normalize_url

SNIPPET_META_FLAG = "searxng_snippet"

logger = get_logger("search.providers.searxng")


class SearXNGProvider(SearchProvider):
    # "searxng", not "web": the cascade owns the name "web", and a tier needs a
    # distinct identity so sources=searxng can address it on its own.
    name = "searxng"

    def __init__(self, session_factory: async_sessionmaker, url: str, timeout: float = 12.0) -> None:
        self.settings = get_settings()
        self.session_factory = session_factory
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)
        if query.skip_when_local_hit:
            try:
                local = await self._local_hits(query.q, limit=max(query.num_results, 10))
                if len(local) >= self._skip_threshold() and self._is_fresh(local):
                    result.raw = {"served_from_local": True, "local_hits": len(local)}
                    result.latency_ms = (time.monotonic() - start) * 1000
                    return result
            except Exception:
                pass
        params = {
            "q": query.q,
            "format": "json",
            "safesearch": 0,
            "categories": "general",
        }
        if query.language:
            params["language"] = query.language
        if query.time_range and query.time_range != "all":
            params["time_range"] = query.time_range
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(f"{self.url}/search", params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            result.error = f"searxng error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        raw_results = payload.get("results") or []
        for rank, item in enumerate(raw_results[: query.num_results], start=1):
            url = item.get("url") or ""
            if not url:
                continue
            title = item.get("title") or url
            snippet = (item.get("content") or "").strip()[:300]
            published = None
            pub = item.get("publishedDate") or item.get("published_date")
            if pub:
                try:
                    published = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).isoformat()
                except ValueError:
                    published = None
            result.results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source=self.name,
                    score=float(item.get("score") or 1.0) / max(rank, 1),
                    rank=rank,
                    published=published,
                    domain=self._domain(url),
                    raw={"engine": item.get("engine"), "score": item.get("score")},
                )
            )

        if result.results:
            try:
                await self._persist(raw_results[: query.num_results])
            except Exception as e:
                logger.warning("searxng persist failed: %s", e)
                get_metrics().provider_errors.labels(provider=self.name, kind="persist").inc()

        result.latency_ms = (time.monotonic() - start) * 1000
        get_metrics().search_duration.labels(provider=self.name).observe(result.latency_ms / 1000)
        return result

    async def _persist(self, raw_results: list[dict]) -> None:
        rows = []
        for item in raw_results:
            url = item.get("url") or ""
            if not url:
                continue
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "url": url,
                    "norm": normalize_url(url),
                    "title": item.get("title") or url,
                    "content": (item.get("content") or "")[:300],
                    "lang": item.get("language") or None,
                    "published": self._parse_dt(item.get("publishedDate") or item.get("published_date")),
                    "meta": json.dumps({"searxng_snippet": True, "engine": item.get("engine")}),
                }
            )
        if not rows:
            return
        sql = text(
            """
            INSERT INTO documents (id, url, normalized_url, title, content_text, lang, source,
                                   published_at, fetched_at, status, meta)
            VALUES (:id, :url, :norm, :title, :content, :lang, 'web', :published, now(), 'indexed', CAST(:meta AS jsonb))
            ON CONFLICT (normalized_url) DO UPDATE SET
                title = EXCLUDED.title,
                content_text = EXCLUDED.content_text,
                fetched_at = now(),
                meta = EXCLUDED.meta
            WHERE documents.meta->>'searxng_snippet' = 'true'
            """
        )
        async with self.session_factory() as session:
            result = await session.execute(sql, rows)
            await session.commit()
        # Only count rows that were actually inserted, not conflict-skipped ones.
        inserted = result.rowcount if result.rowcount and result.rowcount >= 0 else 0
        if inserted:
            get_metrics().documents_indexed.inc(inserted)

    async def _local_hits(self, q: str, limit: int) -> list:
        from app.search.local_index import LocalIndexProvider

        return await LocalIndexProvider(self.session_factory)._fts_search(
            ProviderQuery(q=q, num_results=limit), or_fallback=False
        )

    def _skip_threshold(self) -> int:
        return self.settings.searxng_local_min_hits

    def _is_fresh(self, local: list) -> bool:
        """True when a *majority* of local hits are within the TTL.

        The previous "any single fresh row" check let one fresh document
        suppress SearXNG even when every top result was stale.
        """
        if not local:
            return False
        ttl = self.settings.searxng_local_ttl_seconds
        now = time.time()
        fresh = 0
        for row in local:
            fetched_at = row.get("fetched_at")
            if isinstance(fetched_at, datetime):
                if now - fetched_at.timestamp() < ttl:
                    fresh += 1
        return fresh * 2 > len(local)

    @staticmethod
    def _parse_dt(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed
        except ValueError:
            return None

    @staticmethod
    def _domain(url: str) -> str:
        m = re.match(r"https?://([^/]+)", url)
        return m.group(1) if m else ""
