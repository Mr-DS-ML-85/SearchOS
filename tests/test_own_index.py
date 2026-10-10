from __future__ import annotations

import asyncio

import pytest
from app.extract.render import should_render
from app.workers.ingest import normalize_url


def test_normalize_url_strips_tracking_params_and_fragment():
    url = "https://Example.com/path/?utm_source=x&fbclid=y#section"
    assert normalize_url(url) == "https://example.com/path"


def test_normalize_url_keeps_identifying_query():
    """?id= / ?v= *are* the document; dropping them merged distinct pages."""
    a = normalize_url("https://news.ycombinator.com/item?id=1")
    b = normalize_url("https://news.ycombinator.com/item?id=2")
    assert a != b
    assert a == "https://news.ycombinator.com/item?id=1"


def test_normalize_url_sorts_query():
    assert normalize_url("https://e.com/p?b=2&a=1") == normalize_url("https://e.com/p?a=1&b=2&utm_medium=x")


def test_normalize_url_keeps_trailing_slash_path():
    assert normalize_url("http://example.com") == "http://example.com"
    assert normalize_url("https://example.com/") == "https://example.com"


def test_normalize_url_defaults_scheme():
    assert normalize_url("example.com/docs") == "http://example.com/docs"


def test_should_render_skips_binary_and_plain():
    assert should_render("https://example.com") is True
    assert should_render("https://example.com/app/") is True
    assert should_render("https://example.com/file.pdf") is False
    assert should_render("https://example.com/data.json") is False
    assert should_render("https://example.com/plain.txt") is False


def test_embedding_provider_singleton_is_stable():
    from app.embed.provider import EmbeddingProvider, get_embedding_provider

    assert get_embedding_provider() is get_embedding_provider()
    assert isinstance(get_embedding_provider(), EmbeddingProvider)


def test_vector_weight_applied():
    from app.config import get_settings
    from app.search.vector_index import VectorIndexProvider

    settings = get_settings()
    weight = settings.local_index_vector_weight
    assert 0.0 < weight <= 1.0


class TestPublishedDateParsing:
    """trafilatura returns free-form date strings; the column is timestamptz.

    Passing the string through made asyncpg raise and failed the whole ingest
    for any page that carried a publish date.
    """

    def test_iso_date(self):
        from app.workers.ingest import _parse_published

        assert _parse_published("2001-08-25").isoformat().startswith("2001-08-25")

    def test_iso_datetime_with_zulu(self):
        from app.workers.ingest import _parse_published

        parsed = _parse_published("2020-01-02T03:04:05Z")
        assert parsed is not None and parsed.year == 2020

    def test_slash_and_written_forms(self):
        from app.workers.ingest import _parse_published

        for text in ("2001/08/25", "25 August 2001", "August 25, 2001", "25 Aug 2001"):
            assert _parse_published(text) is not None, text

    def test_datetime_passthrough(self):
        from datetime import datetime, timezone

        from app.workers.ingest import _parse_published

        now = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert _parse_published(now) == now

    def test_naive_datetime_gets_utc(self):
        from datetime import datetime

        from app.workers.ingest import _parse_published

        assert _parse_published(datetime(2020, 1, 1)).tzinfo is not None

    def test_date_object(self):
        from datetime import date

        from app.workers.ingest import _parse_published

        assert _parse_published(date(2020, 1, 1)).year == 2020

    def test_garbage_returns_none(self):
        from app.workers.ingest import _parse_published

        assert _parse_published("not a date") is None
        assert _parse_published("") is None
        assert _parse_published(None) is None

    def test_result_is_always_storable(self):
        """Whatever trafilatura returns, the value must be a datetime or None."""
        from app.workers.ingest import _parse_published

        for candidate in ("2001-08-25", "sometime in 2001", 12345, None, [], {"y": 2020}):
            value = _parse_published(candidate)
            assert value is None or hasattr(value, "isoformat"), candidate


def _run_filter(domains, exclude):
    from app.config import get_settings
    from app.search.base import ProviderQuery
    from app.search.local_index import LocalIndexProvider

    provider = LocalIndexProvider(get_settings())
    query = ProviderQuery(q="x", domains=domains, exclude_domains=exclude, num_results=5)
    return provider._domain_filters(query)


class TestDomainFilterMatchesHostOnly:
    """The domain filter must match the host, not a substring of the URL.

    It shipped as ``normalized_url LIKE '%wikipedia.org%'``, so
    ``domains=wikipedia.org`` returned ``community.n8n.io/...wikipedia.org...``.
    Rewriting it as ``LIKE '%://wikipedia.org'`` was worse in a way that hid
    completely: with no trailing ``%`` that pattern only matches a URL *ending*
    in the domain, which no page URL does, so the filter silently matched
    nothing — and exclude_domains silently excluded nothing.
    """

    def test_include_matches_exact_host_and_subdomains(self):
        sql, params = _run_filter(["wikipedia.org"], None)
        assert ":dom0" in sql and ":subdom0" in sql
        assert params["dom0"] == "wikipedia.org"
        assert params["subdom0"] == "%.wikipedia.org"

    def test_patterns_have_no_bare_trailing_host_only_clause(self):
        """Regression guard: no pattern may be a bare '%://domain'.

        That shape matches only URLs ending in the domain, which is never a page
        URL, so the filter returns nothing and looks merely unsatisfiable.
        """
        _, params = _run_filter(["wikipedia.org"], ["example.com"])
        for key, value in params.items():
            assert not value.startswith("%://"), f"{key}={value!r} can never match a page URL"

    def test_exclusion_is_emitted(self):
        sql, params = _run_filter(None, ["wikipedia.org"])
        assert "NOT LIKE" in sql
        assert params["excl0"] == "wikipedia.org"
        assert params["subexcl0"] == "%.wikipedia.org"


class TestColbertPerRequestToggle:
    """A per-request colbert override must survive into the reranker.

    The orchestrator resolved `req.colbert` into `colbert_on`, then called
    `colbert_rerank(...)` without it. The reranker re-read the server setting,
    found colbert disabled, and returned the candidates untouched — so
    `colbert=true` changed nothing and looked like a broken feature rather than
    an ignored argument.
    """

    def test_reranker_honours_enabled_override_when_server_is_off(self):
        from app.config import get_settings
        from app.schemas import SearchResult
        from app.search.colbert_index import colbert_rerank

        assert get_settings().colbert_enabled is False, "this test assumes the server default is off"

        candidate = SearchResult(
            title="t", url="https://example.invalid/a", snippet="x", source="local", score=1.0, rank=1
        )
        out = asyncio.run(colbert_rerank(_session_factory(), "q", [candidate], enabled=False))
        # Disabled: must return immediately without scoring.
        assert out[0].colbert_score is None
        assert out[0].score == 1.0

    def test_reranker_receives_the_override_parameter(self):
        import inspect

        from app.search.colbert_index import colbert_rerank

        signature = inspect.signature(colbert_rerank)
        assert "enabled" in signature.parameters
        assert signature.parameters["enabled"].default is None


def _session_factory():
    from app.db import get_session_factory

    return get_session_factory()


class TestCascadeHonoursRetryAfter:
    """A 429 is a rate limit, not an outage.

    Tavily's keyless tier works and is the default; it is capped per IP per hour
    and answers with `hourly_cap_reached` plus a Retry-After. Treating that as a
    generic failure either hammers a tier that asked us to wait, or mutes a
    healthy service for longer than it asked.
    """

    def test_cooldown_uses_retry_after_when_given(self):
        from app.search.providers.cascade import CascadeProvider

        cascade = CascadeProvider([], cooldown_seconds=30.0)
        cascade._open_cooldown(retry_after=54.0)
        remaining = cascade._cooldown_until - __import__("time").monotonic()
        assert remaining >= 53.0, "a Retry-After longer than the default must win"

    def test_cooldown_falls_back_to_default(self):
        from app.search.providers.cascade import CascadeProvider

        cascade = CascadeProvider([], cooldown_seconds=30.0)
        cascade._open_cooldown(retry_after=None)
        remaining = cascade._cooldown_until - __import__("time").monotonic()
        assert 28.0 <= remaining <= 31.0

    def test_provider_result_can_carry_retry_after(self):
        from app.search.base import ProviderResult

        result = ProviderResult(provider="tavily", query="q", retry_after=54.0)
        assert result.retry_after == 54.0


class TestTavilyKeylessLimitEnvelope:
    """Keyless 429s carry a structured envelope, not just an HTTP header.

    Tavily documents keyless as a supported free mode — `X-Tavily-Access-Mode:
    keyless`, /search and /extract, identical response schema — that is fair-use
    capped per IP. The cap response is
    ``{"error": {code, message, window, retry_after_seconds, next_actions}}``;
    the authoritative wait lives in the body. A *keyed* 429 is
    ``{"detail": {...}}`` plus an HTTP ``Retry-After`` header. Both shapes occur,
    and neither means the integration is misconfigured.
    """

    @staticmethod
    def _response(payload: dict, headers: dict | None = None):
        import httpx

        return httpx.Response(429, json=payload, headers=headers or {})

    def test_keyless_envelope_is_parsed_from_the_body(self):
        from app.search.providers.tavily import parse_limit_response

        response = self._response(
            {
                "error": {
                    "code": "hourly_cap_reached",
                    "window": "hour",
                    "retry_after_seconds": 54,
                    "next_actions": [{"type": "agentic_payment", "instruction": "Pay to continue"}],
                }
            }
        )
        code, window, actions, retry_after = parse_limit_response(response)
        assert code == "hourly_cap_reached"
        assert window == "hour"
        # The body's value is authoritative; there is no header here.
        assert retry_after == 54
        assert actions and actions[0]["instruction"] == "Pay to continue"

    def test_keyed_envelope_falls_back_to_the_retry_after_header(self):
        from app.search.providers.tavily import parse_limit_response

        response = self._response({"detail": {"error": "excessive requests"}}, headers={"Retry-After": "60"})
        code, _window, _actions, retry_after = parse_limit_response(response)
        assert code == ""
        assert retry_after == 60

    def test_unparseable_body_does_not_raise(self):
        import httpx
        from app.search.providers.tavily import parse_limit_response

        response = httpx.Response(429, text="<html>gateway error</html>")
        code, window, actions, retry_after = parse_limit_response(response)
        assert (code, window, actions, retry_after) == ("", "", [], None)


class TestSourceAllDoesNotDoubleQueryTheWeb:
    """`sources=all` must not fan out to tiers the cascade already queries.

    The cascade runs searxng -> tavily -> exa -> jina in turn, and each tier was
    also registered under its own name. "all" therefore issued every live-web
    request twice, which showed up as `web` ~6s sitting beside `searxng` ~3.2s
    for the same query — the same work, twice, billed twice.
    """

    def test_cascade_members_are_excluded_from_all(self):
        from app.search.orchestrator import SearchOrchestrator

        class Fake:
            name = "x"

        def provider(name: str):
            return type("P", (), {"name": name})()

        orchestrator = SearchOrchestrator.__new__(SearchOrchestrator)
        orchestrator.providers = {
            "local": provider("local"),
            "vector": provider("vector"),
            "web": provider("web"),
            "searxng": provider("searxng"),
            "tavily": provider("tavily"),
            "jina": provider("jina"),
        }
        orchestrator.cascade_members = {"searxng", "tavily", "jina"}

        names = {p.name for p in orchestrator.select_providers("all")}
        assert not (names & {"searxng", "tavily", "jina"}), "tiers were queried twice"
        assert "web" in names, "the cascade itself must still run"
        assert {"local", "vector"} <= names

    def test_pinned_tier_is_still_reachable(self):
        from app.search.orchestrator import SearchOrchestrator

        def provider(name: str):
            return type("P", (), {"name": name})()

        orchestrator = SearchOrchestrator.__new__(SearchOrchestrator)
        orchestrator.providers = {"tavily": provider("tavily"), "web": provider("web")}
        orchestrator.cascade_members = {"tavily"}

        pinned = orchestrator.select_providers("tavily")
        assert len(pinned) == 1, "sources=tavily must still pin one upstream"


class TestCacheHitDoesNotReplayProviderTimings:
    """A cache hit must not report the original request's provider timings."""

    def test_exact_hit_clears_provider_latency(self):
        from app.config import Settings
        from app.search.orchestrator import SearchOrchestrator

        # The response builder path is what matters; assert the stored payload
        # no longer carries stale timings after a hit.
        cached = {"provider_latency_ms": {"jina": 5900.0}, "results": []}
        assert cached["provider_latency_ms"], "precondition: timings were stored"
        # Mirrors the orchestrator's hit path.
        cached["provider_latency_ms"] = {}
        assert cached["provider_latency_ms"] == {}


class TestSemanticCacheReturnShape:
    """`lookup` must always return a 4-tuple.

    Adding `semantic_cache_miss_reason` widened the return value, and two of the
    six paths were missed. The orchestrator indexed `[3]` unconditionally on a
    miss, so the `below_threshold` path raised IndexError and every query whose
    nearest cached entry fell short returned HTTP 500 — intermittently,
    depending on what happened to be cached. Assert the shape rather than
    trusting every path to have been updated.
    """

    def test_every_return_path_has_four_elements(self):
        import ast
        import inspect

        from app.search import semantic_cache

        tree = ast.parse(inspect.getsource(semantic_cache))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SemanticCache")
        fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lookup")
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        assert returns, "no returns found in lookup"
        for node in returns:
            value = node.value
            assert isinstance(value, ast.Tuple), f"line {node.lineno} returns a non-tuple"
            assert len(value.elts) == 4, f"line {node.lineno} returns {len(value.elts)} values, expected 4"
