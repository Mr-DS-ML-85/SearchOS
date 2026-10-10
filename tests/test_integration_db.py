"""Integration tests against a real PostgreSQL + pgvector database.

Skipped automatically when ``TEST_DATABASE_URL`` is not set, so the unit suite
still runs with no infrastructure. Enable with:

    docker run -d --name searchos-test-pg \\
      -e POSTGRES_USER=searchos -e POSTGRES_PASSWORD=searchos \\
      -e POSTGRES_DB=searchos -p 15432:5432 pgvector/pgvector:pg16
    export TEST_DATABASE_URL=postgresql+asyncpg://searchos:searchos@127.0.0.1:15432/searchos
    pytest tests/test_integration_db.py -q
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set; skipping database integration tests",
)


@pytest.fixture
async def session_factory():
    """A session factory against the test database, truncated per test."""
    from app import models  # noqa: F401  register metadata
    from app.db import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                __import__("sqlalchemy").text("TRUNCATE documents, chunks, ingest_jobs, api_keys, usage_logs CASCADE")
            )
        await engine.dispose()


def _doc(normalized: str, **kwargs):
    from app.models import Document

    defaults = {
        "id": uuid.uuid4(),
        "url": normalized,
        "normalized_url": normalized,
        "title": kwargs.pop("title", "Test Document"),
        "content_text": kwargs.pop("content_text", "content"),
        "lang": "en",
        "source": "ingest",
        "status": "indexed",
        "fetched_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return Document(**defaults)


class TestLocalIndexSQL:
    async def test_fts_matches_stemmed_terms(self, session_factory):
        """'running' must match 'run' via the english stemmer."""
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        async with session_factory() as s:
            s.add(_doc("https://a.com/run", title="Run Guide", content_text="A guide to running fast."))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        rows = await provider._fts_search(ProviderQuery(q="running", num_results=10))
        assert len(rows) == 1
        assert rows[0]["url"] == "https://a.com/run"

        # "gpus" is NOT stemmed to "gpu" by Postgres' english dictionary, so we
        # must not claim otherwise — but an exact term must still match.
        exact = await provider._fts_search(ProviderQuery(q="guide", num_results=10))
        assert len(exact) == 1

    @pytest.mark.parametrize(
        "query",
        [
            "cats and dogs",  # tsquery operators must not be injectable
            "cats & dogs",
            "!cats",
            "(cats)",
            "cats | dogs",
            "cats:*",
            "'; DROP TABLE documents; --",
        ],
    )
    async def test_operator_injection_does_not_error(self, session_factory, query):
        """A query containing tsquery syntax must not raise a Postgres error.

        The previous builder interpolated raw terms, so "cats and dogs" produced
        a syntax error and surfaced as a 500.
        """
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        async with session_factory() as s:
            s.add(_doc("https://a.com/c", title="Cats and Dogs", content_text="cats and dogs"))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        result = await provider.search(ProviderQuery(q=query, num_results=10))
        assert result.error is None, result.error

    async def test_injection_attempt_leaves_table_intact(self, session_factory):
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider
        from sqlalchemy import text

        async with session_factory() as s:
            s.add(_doc("https://a.com/keep", content_text="keepme"))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        await provider.search(ProviderQuery(q="keepme'; DROP TABLE documents; --"))

        async with session_factory() as s:
            count = (await s.execute(text("SELECT count(*) FROM documents"))).scalar()
        assert count == 1, "documents table was dropped by an injection attempt"

    async def test_fts_and_mode_returns_nothing_then_or_fallback(self, session_factory):
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        async with session_factory() as s:
            s.add(_doc("https://a.com/1", content_text="alpha beta"))
            s.add(_doc("https://a.com/2", content_text="gamma delta"))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        # Strict AND matches nothing (no doc has both terms) ...
        strict = await provider._fts_search(ProviderQuery(q="alpha gamma", num_results=10), or_fallback=False)
        assert len(strict) == 0

        # ... so the default path falls back to OR and returns both.
        fallback = await provider._fts_search(ProviderQuery(q="alpha gamma", num_results=10))
        assert len(fallback) == 2

    async def test_and_mode_used_when_all_terms_present(self, session_factory):
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        async with session_factory() as s:
            s.add(_doc("https://a.com/both", content_text="alpha gamma together"))
            s.add(_doc("https://a.com/only", content_text="alpha alone"))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        rows = await provider._fts_search(ProviderQuery(q="alpha gamma", num_results=10), or_fallback=False)
        assert [r["url"] for r in rows] == ["https://a.com/both"]

    async def test_non_ascii_query_produces_terms(self, session_factory):
        """A CJK query previously produced an empty tsquery and zero results."""
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        provider = LocalIndexProvider(session_factory)
        tsq = provider._build_tsquery("日本語の検索", "AND")
        assert tsq
        assert "日" in tsq

    async def test_accented_query_keeps_whole_word(self, session_factory):
        from app.search.local_index import LocalIndexProvider

        provider = LocalIndexProvider(session_factory)
        # The whole word is preserved (previously split to "ller") and quoted so
        # tsquery operators cannot leak through.
        assert provider._build_tsquery("müller", "AND") == "'müller'"

    async def test_empty_tsquery_is_not_queried(self, session_factory):
        """Stopword-only or symbol-only input must not raise a Postgres error."""
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        provider = LocalIndexProvider(session_factory)
        assert provider._build_tsquery("!!! ???", "AND") == ""
        assert await provider._fts_search(ProviderQuery(q="!!!", num_results=10)) == []

    async def test_domain_filter_excludes_other_domains(self, session_factory):
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        async with session_factory() as s:
            s.add(_doc("https://allowed.com/p", content_text="shared keyword"))
            s.add(_doc("https://blocked.com/p", content_text="shared keyword"))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        rows = await provider._fts_search(ProviderQuery(q="shared", num_results=10, domains=["allowed.com"]))
        assert [r["url"] for r in rows] == ["https://allowed.com/p"]

        rows = await provider._fts_search(ProviderQuery(q="shared", num_results=10, exclude_domains=["blocked.com"]))
        assert [r["url"] for r in rows] == ["https://allowed.com/p"]

    async def test_domain_filter_escapes_like_wildcards(self, session_factory):
        """domains=['%'] must not match every document."""
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        async with session_factory() as s:
            s.add(_doc("https://a.com/p", content_text="wildcardtest"))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        rows = await provider._fts_search(ProviderQuery(q="wildcardtest", num_results=10, domains=["%"]))
        assert rows == []

    async def test_time_range_filters_recent_only(self, session_factory):
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        old = datetime.now(UTC) - timedelta(days=400)
        async with session_factory() as s:
            s.add(_doc("https://a.com/old", content_text="temporalmarker", published_at=old))
            s.add(
                _doc(
                    "https://a.com/new",
                    content_text="temporalmarker",
                    published_at=datetime.now(UTC),
                )
            )
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        rows = await provider._fts_search(ProviderQuery(q="temporalmarker", num_results=10, time_range="year"))
        urls = {r["url"] for r in rows}
        assert "https://a.com/new" in urls
        assert "https://a.com/old" not in urls

    async def test_domain_field_is_hostname_not_source(self, session_factory):
        """docs advertised domain=en.wikipedia.org for a local result."""
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        async with session_factory() as s:
            s.add(_doc("https://en.wikipedia.org/wiki/x", content_text="wikimarker", source="crawler"))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        result = await provider.search(ProviderQuery(q="wikimarker", num_results=10))
        assert len(result.results) == 1
        assert result.results[0].domain == "en.wikipedia.org"

    async def test_snippet_is_truncated_to_300(self, session_factory):
        from app.search.base import ProviderQuery
        from app.search.local_index import LocalIndexProvider

        long_text = "sniplong " * 200
        async with session_factory() as s:
            s.add(_doc("https://a.com/long", content_text=long_text))
            await s.commit()

        provider = LocalIndexProvider(session_factory)
        result = await provider.search(ProviderQuery(q="sniplong", num_results=10))
        assert len(result.results[0].snippet) <= 300

    async def test_uses_gin_index(self, session_factory):
        """The WHERE expression must match the indexed expression verbatim."""
        from sqlalchemy import text

        async with session_factory() as s:
            s.add(_doc("https://a.com/i", content_text="indexprobe unique token"))
            await s.commit()

        async with session_factory() as s:
            plan = (
                (
                    await s.execute(
                        text(
                            "EXPLAIN SELECT 1 FROM documents d WHERE "
                            "to_tsvector('english', coalesce(d.title,'') || ' ' || coalesce(d.content_text,'')) "
                            "@@ to_tsquery('english', 'indexprobe')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        plan_text = " ".join(str(p) for p in plan)
        assert "ix_documents_fts" in plan_text, plan_text


class TestVectorIndexSQL:
    async def _seed_chunk(self, session_factory, url: str, chunks: list[str], vectors: list[list[float]]):
        from app.models import Chunk, Document

        async with session_factory() as s:
            doc = _doc(url, content_text=" ".join(chunks))
            s.add(doc)
            await s.flush()
            for i, (text, vec) in enumerate(zip(chunks, vectors, strict=True)):
                s.add(
                    Chunk(
                        id=uuid.uuid4(),
                        document_id=doc.id,
                        index=i,
                        content=text,
                        token_count=len(text) // 4,
                        embedding=vec,
                    )
                )
            await s.commit()

    async def test_returns_one_row_per_document(self, session_factory, settings):
        """A 5-chunk document must not occupy 5 of the LIMIT slots."""
        from app.embed.provider import EmbeddingProvider
        from app.search.base import ProviderQuery
        from app.search.vector_index import VectorIndexProvider

        # Unit vectors along axis 0 make distances predictable.
        def vec(x: float) -> list[float]:
            v = [0.0] * 384
            v[0] = x
            return v

        url = "https://multi.com/page"
        await self._seed_chunk(
            session_factory, url, [f"chunk {i}" for i in range(5)], [vec(0.9 - i * 0.01) for i in range(5)]
        )
        await self._seed_chunk(session_factory, "https://single.com/a", ["only"], [vec(0.8)])

        class FakeEmbedding:
            available = True

            async def embed_one(self, text):
                return vec(1.0)

        provider = VectorIndexProvider(session_factory, FakeEmbedding())
        result = await provider.search(ProviderQuery(q="query", num_results=10))
        urls = [r.url for r in result.results]
        assert len(urls) == 2
        assert len(set(urls)) == 2, f"same document returned twice: {urls}"
        assert url in urls

    async def test_snippet_is_the_matched_chunk(self, session_factory):
        from app.search.base import ProviderQuery
        from app.search.vector_index import VectorIndexProvider

        def vec(x: float, y: float = 0.0) -> list[float]:
            # Non-collinear vectors: cosine distance is scale-invariant, so two
            # vectors along one axis would tie at distance 0.
            v = [0.0] * 384
            v[0] = x
            v[1] = y
            return v

        await self._seed_chunk(
            session_factory,
            "https://c.com/p",
            ["intro filler filler", "THE MATCHED CHUNK MARKER"],
            [vec(0.2, 0.9), vec(0.99, 0.01)],
        )

        class FakeEmbedding:
            available = True

            async def embed_one(self, text):
                return vec(1.0, 0.0)

        provider = VectorIndexProvider(session_factory, FakeEmbedding())
        result = await provider.search(ProviderQuery(q="q", num_results=5))
        assert "MATCHED CHUNK MARKER" in result.results[0].snippet, result.results[0].snippet

    async def test_similarity_floor_filters_weak_matches(self, session_factory, settings):
        from app.search.base import ProviderQuery
        from app.search.vector_index import VectorIndexProvider

        def vec(x: float) -> list[float]:
            v = [0.0] * 384
            v[0] = x
            return v

        await self._seed_chunk(session_factory, "https://weak.com/p", ["weak"], [vec(-0.9)])

        class FakeEmbedding:
            available = True

            async def embed_one(self, text):
                return vec(1.0)

        from app.config import get_settings

        get_settings().vector_min_similarity = 0.5
        try:
            provider = VectorIndexProvider(session_factory, FakeEmbedding())
            result = await provider.search(ProviderQuery(q="q", num_results=5))
            assert result.results == []
        finally:
            get_settings().vector_min_similarity = 0.0

    async def test_null_embeddings_are_skipped(self, session_factory):
        from app.models import Chunk
        from app.search.base import ProviderQuery
        from app.search.vector_index import VectorIndexProvider

        async with session_factory() as s:
            doc = _doc("https://n.com/p")
            s.add(doc)
            await s.flush()
            s.add(
                Chunk(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    index=0,
                    content="no embedding",
                    token_count=2,
                    embedding=None,
                )
            )
            await s.commit()

        class FakeEmbedding:
            available = True

            async def embed_one(self, text):
                return [0.0] * 384

        provider = VectorIndexProvider(session_factory, FakeEmbedding())
        result = await provider.search(ProviderQuery(q="q", num_results=5))
        assert result.results == []
        assert result.error is None


class TestSearXNGPersistence:
    async def test_does_not_clobber_fully_crawled_document(self, session_factory):
        """A snippet upsert must not overwrite a real crawled body."""
        from app.search.providers.searxng import SearXNGProvider
        from sqlalchemy import text

        provider = SearXNGProvider(session_factory, "http://searxng.test", 5.0)
        long_body = "REAL CRAWLED CONTENT " * 500

        async with session_factory() as s:
            s.add(
                _doc(
                    "https://site.com/p",
                    content_text=long_body,
                    source="ingest",
                    meta={"kind": "crawled"},
                )
            )
            await s.commit()

        await provider._persist([{"url": "https://site.com/p", "title": "Snippet title", "content": "short snippet"}])

        async with session_factory() as s:
            row = (
                (
                    await s.execute(
                        text("SELECT content_text, title, meta FROM documents WHERE normalized_url = :n"),
                        {"n": "https://site.com/p"},
                    )
                )
                .mappings()
                .first()
            )

        assert row["content_text"] == long_body, "crawled body was overwritten by a snippet"
        assert row["title"] != "Snippet title"

    async def test_updates_a_previous_snippet_stub(self, session_factory):
        from app.search.providers.searxng import SearXNGProvider
        from sqlalchemy import text

        provider = SearXNGProvider(session_factory, "http://searxng.test", 5.0)

        async with session_factory() as s:
            s.add(
                _doc(
                    "https://site.com/q",
                    content_text="old snippet",
                    meta={"searxng_snippet": True},
                )
            )
            await s.commit()

        await provider._persist([{"url": "https://site.com/q", "title": "New title", "content": "new snippet"}])

        async with session_factory() as s:
            row = (
                (
                    await s.execute(
                        text("SELECT content_text FROM documents WHERE normalized_url = :n"),
                        {"n": "https://site.com/q"},
                    )
                )
                .mappings()
                .first()
            )
        assert row["content_text"] == "new snippet"

    async def test_inserts_when_absent(self, session_factory):
        from app.search.providers.searxng import SearXNGProvider
        from sqlalchemy import text

        provider = SearXNGProvider(session_factory, "http://searxng.test", 5.0)
        await provider._persist([{"url": "https://new.com/a", "title": "T", "content": "C"}])

        async with session_factory() as s:
            count = (
                await s.execute(
                    text("SELECT count(*) FROM documents WHERE normalized_url = :n"), {"n": "https://new.com/a"}
                )
            ).scalar()
        assert count == 1


class TestOrchestratorAgainstDB:
    async def test_end_to_end_local_search(self, session_factory, cache, settings):
        from app.schemas import SearchRequest
        from app.search.local_index import LocalIndexProvider
        from app.search.orchestrator import SearchOrchestrator

        async with session_factory() as s:
            for i in range(1, 26):
                s.add(_doc(f"https://e2e.com/{i}", title=f"Page {i}", content_text=f"e2etoken number {i}"))
            await s.commit()

        orch = SearchOrchestrator(providers={"local": LocalIndexProvider(session_factory)}, cache=cache)
        resp = await orch.search(SearchRequest(q="e2etoken", num_results=10, cache=False))
        assert len(resp.results) == 10
        assert resp.total == 25
        assert [r.rank for r in resp.results] == list(range(1, 11))

    async def test_pagination_is_gapless_against_real_sql(self, session_factory, cache):
        """Regression: offset was applied in SQL and again in the orchestrator."""
        from app.schemas import SearchRequest
        from app.search.local_index import LocalIndexProvider
        from app.search.orchestrator import SearchOrchestrator

        async with session_factory() as s:
            for i in range(1, 31):
                s.add(_doc(f"https://pg.com/{i}", content_text=f"pgtoken doc {i}"))
            await s.commit()

        orch = SearchOrchestrator(providers={"local": LocalIndexProvider(session_factory)}, cache=cache)

        seen: list[str] = []
        for offset in (0, 10, 20):
            resp = await orch.search(SearchRequest(q="pgtoken", num_results=10, offset=offset, cache=False))
            seen.extend(r.url for r in resp.results)

        assert len(seen) == 30
        assert len(set(seen)) == 30, "duplicate or skipped results across pages"

        past_end = await orch.search(SearchRequest(q="pgtoken", num_results=10, offset=30, cache=False))
        assert past_end.results == []
        assert past_end.total == 30

    async def test_no_match_returns_empty_not_error(self, session_factory, cache):
        from app.schemas import SearchRequest
        from app.search.local_index import LocalIndexProvider
        from app.search.orchestrator import SearchOrchestrator

        async with session_factory() as s:
            s.add(_doc("https://x.com/a", content_text="alpha"))
            await s.commit()

        orch = SearchOrchestrator(providers={"local": LocalIndexProvider(session_factory)}, cache=cache)
        resp = await orch.search(SearchRequest(q="zzzznomatch", cache=False))
        assert resp.results == []
        assert resp.total == 0


class TestDeduplication:
    def test_www_and_port_variants_collapse(self):
        from app.workers.ingest import normalize_url

        canonical = normalize_url("https://example.com/path")
        assert normalize_url("https://www.example.com/path") == canonical
        assert normalize_url("https://EXAMPLE.com/path/") == canonical
        assert normalize_url("https://example.com:443/path") == canonical
        assert normalize_url("http://example.com:80/path") == normalize_url("http://example.com/path")

    def test_query_and_fragment_are_dropped(self):
        from app.workers.ingest import normalize_url

        assert normalize_url("https://example.com/p?a=1#x") == "https://example.com/p"

    def test_distinct_paths_stay_distinct(self):
        from app.workers.ingest import normalize_url

        assert normalize_url("https://example.com/a") != normalize_url("https://example.com/b")

    def test_schemes_are_distinct_preserved_for_http_only_sites(self):
        """http and https are different origins; only ports 80/443 are collapsed."""
        from app.workers.ingest import normalize_url

        # A site that only serves http must not collide with its https twin,
        # because merging them would hide a genuine content difference.
        assert normalize_url("http://example.com/p") != normalize_url("https://example.com/p")


class TestSchemaMetadataMatchesMigrations:
    """Base.metadata must describe the functional indexes.

    The GIN and HNSW indexes used to exist only as raw SQL in migration 0001, so
    any schema built by ``create_all()`` (the integration tests, or anyone
    autogenerating against a fresh DB) silently had neither index and both
    providers degraded to sequential scans with no error.
    """

    def test_fts_and_hnsw_indexes_are_declared_in_metadata(self):
        from app import models  # noqa: F401  registers the indexes
        from app.db import Base

        names = {i.name for t in Base.metadata.sorted_tables for i in t.indexes}
        assert "ix_documents_fts" in names
        assert "ix_chunks_embedding" in names

    def test_functional_indexes_render_expected_ddl(self):
        from app import models  # noqa: F401
        from app.db import Base
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateIndex

        rendered = {}
        for table in Base.metadata.sorted_tables:
            for idx in table.indexes:
                if idx.name in ("ix_documents_fts", "ix_chunks_embedding"):
                    rendered[idx.name] = str(CreateIndex(idx).compile(dialect=postgresql.dialect()))
        assert "USING gin" in rendered["ix_documents_fts"]
        assert "to_tsvector" in rendered["ix_documents_fts"]
        # Cast is required; without it SQLAlchemy binds 'english' untyped.
        assert "REGCONFIG" in rendered["ix_documents_fts"]
        assert "USING hnsw" in rendered["ix_chunks_embedding"]
        assert "vector_cosine_ops" in rendered["ix_chunks_embedding"]

    async def test_create_all_produces_a_queryable_fts_schema(self, session_factory):
        """End of the chain: a create_all schema must be served by the index."""
        from sqlalchemy import text

        async with session_factory() as s:
            plan = (
                (
                    await s.execute(
                        text(
                            "EXPLAIN SELECT 1 FROM documents d WHERE "
                            "to_tsvector('english', coalesce(d.title,'') || ' ' || coalesce(d.content_text,'')) "
                            "@@ to_tsquery('english', 'anything')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert "ix_documents_fts" in " ".join(str(p) for p in plan)

    async def test_recrawl_and_claim_indexes_exist(self, session_factory):
        from sqlalchemy import text

        expected = {
            "ix_documents_fetched_at",
            "ix_documents_status",
            "ix_ingest_jobs_created_at",
            "ix_ingest_jobs_next_attempt_at",
            "ix_usage_logs_created_at",
        }
        async with session_factory() as s:
            rows = (
                (await s.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))).scalars().all()
            )
        missing = expected - set(rows)
        assert not missing, f"missing production indexes: {missing}"
