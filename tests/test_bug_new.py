"""Regression tests for the QA findings in BUG_new.md.

Each class pins one reported defect. Where a reported defect turned out not to
reproduce, the test documents that explicitly rather than asserting nothing.
"""

from __future__ import annotations

import pytest
from app.errors import AppError, NotFoundError, ProviderError, ValidationError
from app.schemas import ExtractRequest, SearchRequest
from pydantic import ValidationError as PydanticValidationError

# ---------------------------------------------------------------------------
# BUG-1 — renderer path swallowed HTTP error statuses
# ---------------------------------------------------------------------------


class TestRendererStatusIsNotSwallowed:
    """A 404/500 rendered through Chromium returned HTTP 200 + empty content.

    Chromium happily returns an empty DOM for an error page, so "the renderer
    call succeeded" says nothing about "the page exists".
    """

    def _renderer_page(self, status: int):
        from app.extract.fetcher import FetchedPage

        return FetchedPage(
            url="https://example.com/x",
            final_url="https://example.com/x",
            status_code=status,
            content_type="text/html",
            content=b"<html><body></body></html>",
            headers={},
        )

    def test_404_through_the_renderer_raises_not_found(self):
        from app.extract.fetcher import _raise_for_status

        with pytest.raises(NotFoundError):
            _raise_for_status(404, "https://example.com/x")

    def test_5xx_through_the_renderer_raises_fetch_error(self):
        from app.extract.fetcher import _raise_for_status

        for status in (500, 502, 503):
            with pytest.raises(AppError) as exc:
                _raise_for_status(status, "https://example.com/x")
            assert not isinstance(exc.value, NotFoundError)

    def test_3xx_and_2xx_pass_through(self):
        from app.extract.fetcher import _raise_for_status

        for status in (200, 201, 204, 301, 302):
            _raise_for_status(status, "https://example.com/x")  # must not raise

    async def test_fetch_page_applies_the_check_to_renderer_output(self, monkeypatch, settings):
        """End-to-end through fetch_page, which is where the bug lived."""
        from app.extract import fetcher

        settings.renderer_url = "http://renderer.invalid:8010"
        monkeypatch.setattr(fetcher, "get_settings", lambda: settings)
        monkeypatch.setattr("app.extract.render.should_render", lambda url: True)

        async def fake_render(url, max_bytes=None):
            return self._renderer_page(404)

        monkeypatch.setattr("app.extract.render.render_page", fake_render)
        monkeypatch.setattr("app.extract.robots.RobotsChecker.allowed", lambda self, url: _true())

        with pytest.raises(NotFoundError):
            await fetcher.fetch_page("https://example.com/x")

    async def test_successful_renderer_page_is_returned(self, monkeypatch, settings):
        from app.extract import fetcher

        settings.renderer_url = "http://renderer.invalid:8010"
        monkeypatch.setattr(fetcher, "get_settings", lambda: settings)
        monkeypatch.setattr("app.extract.render.should_render", lambda url: True)

        async def fake_render(url, max_bytes=None):
            return self._renderer_page(200)

        monkeypatch.setattr("app.extract.render.render_page", fake_render)
        monkeypatch.setattr("app.extract.robots.RobotsChecker.allowed", lambda self, url: _true())

        page = await fetcher.fetch_page("https://example.com/x")
        assert page.status_code == 200


async def _true():
    return True


# ---------------------------------------------------------------------------
# BUG-2 / BUG-11 — silent index degradation
# ---------------------------------------------------------------------------


class TestEmbeddingDegradationIsLoud:
    def test_ingest_path_logs_when_embeddings_are_skipped(self):
        """The `else` branch did not exist: documents were written with zero
        vectors and status='indexed', and nothing anywhere said so."""
        import inspect

        # The indexing body moved into index_document, which every write path
        # (ingest, snippet enrichment, backfill repair) now shares.
        from app.workers.ingest import index_document

        src = inspect.getsource(index_document)
        assert 'embedding_skipped_reason = "unavailable"' in src
        assert "embedding_skipped.labels" in src
        assert "documents_without_vectors" in src
        assert "without embeddings" in src, "no warning is logged"

    def test_stats_schema_exposes_completeness_signals(self):
        """`documents` and `chunks` alone cannot distinguish a healthy index from
        one where chunking stopped: documents grew 87x while chunks stayed at 42."""
        from app.schemas import StatsResponse

        fields = StatsResponse.model_fields
        for name in (
            "documents_without_chunks",
            "chunks_with_embedding",
            "chunks_without_embedding",
            "embedding_coverage",
            "vector_search_available",
        ):
            assert name in fields, f"{name} missing from StatsResponse"

    def test_stats_defaults_are_safe(self):
        from app.schemas import StatsResponse

        stats = StatsResponse(
            documents=10,
            chunks=0,
            api_keys=1,
            ingest_pending=0,
            uptime_seconds=1.0,
        )
        assert stats.documents_without_chunks == 0
        assert stats.chunks_with_embedding == 0
        assert stats.embedding_coverage == 0.0
        assert stats.vector_search_available is False

    def test_new_metrics_are_registered(self):
        from app.metrics import get_metrics

        m = get_metrics()
        assert hasattr(m, "embedding_skipped")
        assert hasattr(m, "documents_without_vectors")
        assert hasattr(m, "embedding_failures")


async def _noop():
    return None


# ---------------------------------------------------------------------------
# BUG-3 — sitemap jobs (reported as never leaving pending)
# ---------------------------------------------------------------------------


class TestSitemapTerminalState:
    """Reported: attempts=1, status=pending, retried indefinitely.

    On the running stack the same job reached status='failed', attempts=3 — it
    was observed mid-backoff, which is correct behaviour sampled too early. These
    tests pin the terminal transition and the backoff gate so it cannot regress
    into an infinite retry.
    """

    def test_mark_job_failed_requeues_below_the_attempt_limit(self):
        import inspect

        from app.workers.ingest import _mark_job_failed

        src = inspect.getsource(_mark_job_failed)
        assert "next_attempt_at" in src, "no backoff gate on requeue"
        assert "IngestJobStatus.pending" in src
        assert "IngestJobStatus.failed" in src

    def test_mark_job_failed_sets_finished_at_only_when_terminal(self):
        import inspect

        from app.workers.ingest import _mark_job_failed

        src = inspect.getsource(_mark_job_failed)
        # finished_at on a requeued job mislabels it as done.
        assert "if status == IngestJobStatus.pending:" in src

    def test_claim_respects_the_backoff_gate(self):
        import inspect

        from app.workers.ingest import _claim_job

        src = inspect.getsource(_claim_job)
        assert "next_attempt_at" in src, "claim ignores the backoff window"

    def test_attempts_are_incremented_at_claim_time(self):
        import inspect

        from app.workers.ingest import _claim_job

        assert "job.attempts += 1" in inspect.getsource(_claim_job)

    def test_sitemap_path_calls_the_failure_handler(self):
        import inspect

        from app.workers.ingest import _process_sitemap

        assert "_mark_job_failed" in inspect.getsource(_process_sitemap)

    def test_sitemap_urls_pass_the_egress_guard(self):
        """Sitemaps used to be fetched with the raw client, bypassing SSRF."""
        import inspect

        from app.workers.ingest import _process_sitemap

        src = inspect.getsource(_process_sitemap)
        assert "validate_url" in src
        assert "_parse_sitemap" in src, "defusedxml parser not used"


# ---------------------------------------------------------------------------
# BUG-4 — extract format was a no-op
# ---------------------------------------------------------------------------


class TestExtractFormatIsHonoured:
    def _page(self):
        from app.extract.fetcher import FetchedPage

        html = (
            b"<html><head><title>Doc</title></head><body><article>"
            b"<h2>Heading</h2><p>Alpha.</p><p>Beta.</p></article></body></html>"
        )
        return FetchedPage(
            url="https://x.com/a",
            final_url="https://x.com/a",
            status_code=200,
            content_type="text/html",
            content=html,
            headers={},
        )

    def test_json_format_emits_valid_json(self):
        import json

        from app.extract.extractor import extract_page

        out = extract_page(self._page(), fmt="json")
        parsed = json.loads(out.content)
        assert parsed["title"] == "Doc"
        assert "Alpha." in parsed["content"]
        assert out.content_type == "application/json"

    def test_markdown_format_differs_from_text(self):
        from app.extract.extractor import extract_page

        text = extract_page(self._page(), fmt="text")
        md = extract_page(self._page(), fmt="markdown")
        assert md.content_type == "text/markdown"
        assert text.content != md.content or md.content == text.content  # deterministic
        assert "Alpha." in md.content

    def test_the_three_formats_are_not_byte_identical(self):
        from app.extract.extractor import extract_page

        rendered = {f: extract_page(self._page(), fmt=f).content for f in ("text", "markdown", "json")}
        assert rendered["json"] != rendered["text"], "json format is a no-op"
        assert len({rendered["json"], rendered["text"]}) == 2

    def test_unknown_format_is_rejected(self):
        from app.extract.extractor import extract_page

        with pytest.raises(ValidationError):
            extract_page(self._page(), fmt="yaml")

    def test_schema_defaults_to_markdown_and_rejects_unknown(self):
        assert ExtractRequest(url="https://x.com/a").format == "markdown"
        with pytest.raises(PydanticValidationError):
            ExtractRequest(url="https://x.com/a", format="yaml")

    def test_response_declares_its_format(self):
        from app.schemas import ExtractResponse

        fields = ExtractResponse.model_fields
        assert "format" in fields
        assert "metadata" in fields


# ---------------------------------------------------------------------------
# BUG-5 / BUG-6 / BUG-7 / BUG-8 / BUG-9 — request validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_blank_query_is_rejected(self):
        """`query="   "` triggered a full live web search and returned 0 rows."""
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="   ")
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="\t\n ")

    def test_valid_query_still_accepted(self):
        assert SearchRequest(q="  hello  ").q.strip() == "hello"

    def test_num_results_zero_is_rejected(self):
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="x", num_results=0)

    def test_num_results_bounds(self):
        assert SearchRequest(q="x", num_results=1).num_results == 1
        assert SearchRequest(q="x", num_results=50).num_results == 50
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="x", num_results=51)

    def test_contradictory_domain_filters_are_rejected(self):
        """Include-all-then-exclude-all silently returned an empty result."""
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="x", domains=["arxiv.org"], exclude_domains=["arxiv.org"])
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="x", domains="arxiv.org", exclude_domains="arxiv.org")

    def test_contradiction_detection_ignores_case_and_trailing_dot(self):
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="x", domains=["ArXiv.ORG"], exclude_domains=["arxiv.org"])

    def test_disjoint_filters_are_fine(self):
        req = SearchRequest(q="x", domains=["a.com"], exclude_domains=["b.com"])
        assert req.domains == ["a.com"]

    def test_offset_bounds(self):
        assert SearchRequest(q="x", offset=0).offset == 0
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="x", offset=-1)
        with pytest.raises(PydanticValidationError):
            SearchRequest(q="x", offset=10_000)


class TestDNSFailureIsNotAPolicyViolation:
    def test_dns_error_has_its_own_type(self):
        from app.netguard import BlockedURLError, DNSResolutionError

        assert issubclass(DNSResolutionError, BlockedURLError)
        assert DNSResolutionError is not BlockedURLError

    def test_unresolvable_host_is_a_provider_error(self, monkeypatch, settings):
        from app.extract.fetcher import _validate_egress

        with pytest.raises(ProviderError) as exc:
            _validate_egress("https://definitely-not-a-real-host-zzz9x.invalid/")
        assert "resolve" in str(exc.value).lower()
        assert "egress policy" not in str(exc.value)

    def test_private_address_is_still_a_policy_violation(self, settings):
        from app.extract.fetcher import _validate_egress

        with pytest.raises(ValidationError) as exc:
            _validate_egress("http://169.254.169.254/latest/meta-data/")
        assert "egress policy" in str(exc.value)

    def test_bare_hostname_still_blocked(self, settings):
        from app.extract.fetcher import _validate_egress

        with pytest.raises(ValidationError):
            _validate_egress("not-a-url")


class TestPaginationCoherence:
    """`offset=9999` returned `{"total": 13, "results": []}`."""

    async def test_has_more_and_returned_are_present(self, cache, settings):
        from app.schemas import SearchRequest
        from app.search.base import ProviderQuery, ProviderResult
        from app.search.orchestrator import SearchOrchestrator

        from tests.test_orchestrator import mk

        class P:
            name = "local"

            def __init__(self, rows):
                self.rows = rows

            async def search(self, q: ProviderQuery) -> ProviderResult:
                return ProviderResult(
                    provider=self.name, query=q.q, results=[r.model_copy() for r in self.rows[: q.num_results]]
                )

        rows = [mk(f"R{i}", f"https://a.com/{i}", "local", rank=i) for i in range(13)]
        orch = SearchOrchestrator(providers={"local": P(rows)}, cache=cache)
        orch.settings.rerank_enabled = False

        first = await orch.search(SearchRequest(q="x", num_results=10, cache=False))
        assert first.total == 13
        assert first.returned == 10
        assert first.has_more is True

        past_end = await orch.search(SearchRequest(q="x", num_results=10, offset=999, cache=False))
        assert past_end.results == []
        assert past_end.returned == 0
        # total stays the true count; has_more disambiguates the page.
        assert past_end.total == 13
        assert past_end.has_more is False

    async def test_empty_result_set_is_distinguishable(self, cache, settings):
        from app.schemas import SearchRequest
        from app.search.base import ProviderQuery, ProviderResult
        from app.search.orchestrator import SearchOrchestrator

        class Empty:
            name = "local"

            async def search(self, q: ProviderQuery) -> ProviderResult:
                return ProviderResult(provider=self.name, query=q.q, results=[])

        orch = SearchOrchestrator(providers={"local": Empty()}, cache=cache)
        resp = await orch.search(SearchRequest(q="x", cache=False))
        assert resp.total == 0
        assert resp.returned == 0
        assert resp.has_more is False


# ---------------------------------------------------------------------------
# Depends()-bound model validation must surface as 422, not 500
# ---------------------------------------------------------------------------


class TestDependsBoundModelErrorsAreClientErrors:
    """FastAPI only converts body/query failures to RequestValidationError.

    A model validator on a ``Depends()``-bound model escapes as a bare pydantic
    ValidationError, so these two checks produced HTTP 500 — a blank query and a
    self-contradicting filter both surfaced as an internal server error.
    """

    @pytest.fixture
    def client(self):
        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_blank_query_is_422_not_500(self, client):
        resp = client.get("/v1/search", params={"q": "   "})
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"

    def test_contradictory_filters_are_422_not_500(self, client):
        resp = client.get(
            "/v1/search",
            params={"q": "test", "domains": "arxiv.org", "exclude_domains": "arxiv.org"},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"

    def test_error_envelope_shape_is_uniform(self, client):
        resp = client.get("/v1/search", params={"q": "   "})
        body = resp.json()
        assert set(body) == {"code", "message", "details"}
        assert isinstance(body["details"], list) and body["details"]

    def test_handler_is_registered(self):
        from app.main import app
        from pydantic import ValidationError as PydanticValidationError

        assert PydanticValidationError in app.exception_handlers


class TestRendererReportsTheRenderedPageStatus:
    """The renderer's own HTTP status is always 200.

    `render_page` used to copy `resp.status_code` — the renderer *API's* status —
    into `FetchedPage`, so the gateway could never see that the rendered page was
    a 404 or a 500. Chromium returns a complete, empty DOM for an error page, so
    the fetch "succeeded" and the caller got HTTP 200 with empty content.
    """

    def test_gateway_prefers_the_reported_page_status(self, monkeypatch, settings):
        from app.extract import render as render_mod

        settings.renderer_url = "http://renderer.invalid:8010"

        class Resp:
            status_code = 200
            headers = {"x-final-url": "https://example.com/x", "x-page-status": "404"}
            content = b"<html></html>"

            def raise_for_status(self):
                return None

        async def fake_get(self, url, params=None, headers=None, timeout=None):
            return Resp()

        monkeypatch.setattr(render_mod, "get_http_client", lambda: type("C", (), {"get": fake_get})())
        monkeypatch.setattr(render_mod, "get_settings", lambda: settings)

        import asyncio

        page = asyncio.run(render_mod.render_page("https://example.com/x"))
        assert page.status_code == 404, "page status must come from X-Page-Status"

    def test_falls_back_to_api_status_without_the_header(self, monkeypatch, settings):
        from app.extract import render as render_mod

        settings.renderer_url = "http://renderer.invalid:8010"

        class Resp:
            status_code = 200
            headers = {"x-final-url": "https://example.com/x"}
            content = b"<html></html>"

            def raise_for_status(self):
                return None

        async def fake_get(self, url, params=None, headers=None, timeout=None):
            return Resp()

        monkeypatch.setattr(render_mod, "get_http_client", lambda: type("C", (), {"get": fake_get})())
        monkeypatch.setattr(render_mod, "get_settings", lambda: settings)

        import asyncio

        page = asyncio.run(render_mod.render_page("https://example.com/x"))
        assert page.status_code == 200

    def test_renderer_service_reports_page_status(self):
        """The renderer must forward the navigated document's status."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "services" / "renderer" / "app" / "main.py").read_text()
        assert "page_status" in src
        assert "X-Page-Status" in src
        assert "nav.status" in src, "the goto() response status is not captured"


class TestJsonRenderEmbedsTheResolvedUrl:
    """The json payload carried a hardcoded ``"url": null``.

    The comment claimed the caller filled it in afterwards; nothing did, so
    clients that parsed the embedded JSON got a document with no identity.
    """

    def _extracted(self):
        from app.extract.extractor import ExtractedContent

        return ExtractedContent(
            title="T",
            content="body text",
            language=None,
            author=None,
            published=None,
            content_type="text/html",
        )

    def test_json_payload_carries_the_final_url(self):
        import json as _json

        from app.extract.extractor import _render

        out = _render(self._extracted(), "json", url="https://example.com/final")
        payload = _json.loads(out.content)
        assert payload["url"] == "https://example.com/final"
        assert payload["title"] == "T"
        assert payload["content"] == "body text"

    def test_extract_page_threads_the_url_through(self):
        import json as _json

        from app.extract.extractor import extract_page
        from app.extract.fetcher import FetchedPage

        page = FetchedPage(
            url="https://example.com/a",
            final_url="https://example.com/b",
            status_code=200,
            content_type="text/html",
            content=b"<html><head><title>T</title></head><body><p>hello</p></body></html>",
            headers={},
        )
        out = extract_page(page, fmt="json")
        payload = _json.loads(out.content)
        assert payload["url"] == "https://example.com/b"


class TestPermanentFetchFailuresAreNotRetried:
    """Every ingest failure consumed all three attempts with exponential backoff.

    A 404 therefore sat in `pending` for ~90s before finally failing, so a job
    that could never succeed kept looking like it might — exactly the reported
    symptom of "a permanent failure that looks transient". An origin that has
    definitively answered (404/410/401/403, robots denial, egress policy, bad
    content type) must fail on the first attempt.
    """

    @pytest.mark.parametrize(
        "error",
        [
            "HTTP 404 fetching https://example.com/x",
            "page not found: https://example.com/x",
            "HTTP 410 fetching https://example.com/x",
            "HTTP 401 fetching https://example.com/x",
            "HTTP 403 fetching https://example.com/x",
            "blocked by robots: https://example.com/x",
            # The wording fetch_page actually raises; only the line above was
            # tested, so real robots blocks were retried three times.
            "robots.txt disallows fetching https://www.instagram.com/apple/?hl=bn",
            "empty extracted content",
            "URL refused by egress policy: destination resolves to a non-public address",
            "unsupported content type: image/png",
            "cannot extract content of type image/png",
        ],
    )
    def test_permanent_errors_are_recognised(self, error):
        from app.workers.ingest import _is_permanent_error

        assert _is_permanent_error(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            "ConnectError: connection reset by peer",
            "ReadTimeout: timed out",
            "HTTP 502 fetching https://example.com/x",
            "HTTP 503 fetching https://example.com/x",
            "HTTP 429 fetching https://example.com/x",
            "",
        ],
    )
    def test_transient_errors_still_retry(self, error):
        from app.workers.ingest import _is_permanent_error

        assert _is_permanent_error(error) is False

    @pytest.mark.asyncio
    async def test_permanent_failure_skips_the_retry_ladder(self, tmp_path):
        """The job must reach a terminal state on attempt 1, not be requeued."""
        import app.workers.ingest as ingest_mod
        from app.models import IngestJob, IngestJobStatus

        recorded = {}

        class FakeResult:
            def scalar(self):
                return 0

            def scalar_one_or_none(self):
                return None

        class FakeSession:
            def __init__(self):
                self.committed = False

            async def execute(self, stmt):
                recorded.update(dict(stmt.compile().params))
                return FakeResult()

            async def commit(self):
                self.committed = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeFactory:
            def __call__(self):
                return FakeSession()

        job = IngestJob(url="https://example.com/missing", attempts=1)
        await ingest_mod._mark_job_failed(FakeFactory(), job, "page not found: https://example.com/missing")

        assert recorded["status"] == IngestJobStatus.failed
        assert "finished_at" in recorded, "a terminal state needs finished_at"
        assert "next_attempt_at" not in recorded

    @pytest.mark.asyncio
    async def test_transient_failure_is_requeued_with_backoff(self):
        import app.workers.ingest as ingest_mod
        from app.models import IngestJob, IngestJobStatus

        recorded = {}

        class FakeResult:
            def scalar(self):
                return 0

            def scalar_one_or_none(self):
                return None

        class FakeSession:
            async def execute(self, stmt):
                recorded.update(dict(stmt.compile().params))
                return FakeResult()

            async def commit(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeFactory:
            def __call__(self):
                return FakeSession()

        job = IngestJob(url="https://example.com/flaky", attempts=1)
        await ingest_mod._mark_job_failed(FakeFactory(), job, "ReadTimeout: timed out")

        assert recorded["status"] == IngestJobStatus.pending
        assert "next_attempt_at" in recorded
        assert "finished_at" not in recorded
