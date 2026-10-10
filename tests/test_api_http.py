"""HTTP-level tests for the gateway API surface.

Uses ``TestClient`` against the real app (no mocks), which is what caught the
``domains`` binding bug: unit tests on the orchestrator could never see it
because the parameters were dropped by FastAPI before reaching the code.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://searchos:searchos@127.0.0.1:15432/searchos")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:16380/0")


@pytest.fixture(scope="module")
def client():
    """A TestClient with the lifespan run, unless a database is unavailable."""
    from app.main import app
    from fastapi.testclient import TestClient

    try:
        with TestClient(app) as c:
            yield c
    except Exception as exc:  # pragma: no cover - no infrastructure
        if isinstance(exc, (OSError, ConnectionError)) or "connect" in str(exc).lower():
            pytest.skip(f"no database available: {exc}")
        raise


class TestOpenApiContract:
    def test_search_exposes_domain_filters_as_query_params(self, client):
        """Regression: on a Depends()-bound GET model, list fields are treated as
        body params, so ?domains= was silently discarded end to end."""
        spec = client.get("/openapi.json").json()
        names = {p["name"] for p in spec["paths"]["/v1/search"]["get"]["parameters"]}
        assert "domains" in names
        assert "exclude_domains" in names

    def test_search_params_are_not_required_beyond_q(self, client):
        spec = client.get("/openapi.json").json()
        params = spec["paths"]["/v1/search"]["get"]["parameters"]
        required = [p["name"] for p in params if p.get("required")]
        assert required == ["q"]

    def test_error_schema_is_declared(self, client):
        assert "components" in client.get("/openapi.json").json()

    def test_root_reports_service_and_version(self, client):
        body = client.get("/").json()
        assert body["service"] == "searchos"
        assert "version" in body


class TestHealthAndOps:
    def test_liveness(self, client):
        assert client.get("/health/live").json()["status"] == "ok"

    def test_readiness_reports_component_status(self, client):
        body = client.get("/health/ready").json()
        assert body["status"] in ("ok", "degraded")
        assert "postgres" in body["services"]

    def test_health_alias(self, client):
        assert client.get("/health").json()["status"] in ("ok", "degraded")

    def test_metrics_is_prometheus_text(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "searchos_requests_total" in resp.text

    def test_stats_uptime_is_not_hardcoded_zero(self, client):
        body = client.get("/stats").json()
        assert body["uptime_seconds"] > 0


class TestSearchValidation:
    def test_missing_query_is_422(self, client):
        resp = client.get("/v1/search")
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_num_results_above_max_is_422(self, client):
        resp = client.get("/v1/search", params={"q": "x", "num_results": 500})
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_negative_offset_is_422(self, client):
        resp = client.get("/v1/search", params={"q": "x", "offset": -1})
        assert resp.status_code == 422

    def test_offset_above_max_is_422(self, client):
        resp = client.get("/v1/search", params={"q": "x", "offset": 100000})
        assert resp.status_code == 422

    def test_unknown_source_is_422(self, client):
        resp = client.get("/v1/search", params={"q": "x", "sources": "nope"})
        assert resp.status_code == 422

    def test_domains_accept_repeated_params(self, client):
        resp = client.get("/v1/search", params=[("q", "test"), ("domains", "a.com"), ("domains", "b.com")])
        assert resp.status_code in (200, 404, 502)

    def test_domains_accept_comma_list(self, client):
        resp = client.get("/v1/search", params={"q": "test", "domains": "a.com,b.com"})
        assert resp.status_code in (200, 404, 502)

    def test_comma_list_and_repeated_params_are_equivalent(self):
        from app.api.search import _split_domains

        assert _split_domains(["a.com,b.com"]) == ["a.com", "b.com"]
        assert _split_domains(["a.com", "b.com"]) == ["a.com", "b.com"]
        assert _split_domains([]) is None
        assert _split_domains([" "]) is None


class TestExtractValidation:
    def test_ssrf_targets_are_refused(self, client):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:6379/",
            "http://10.0.0.1/",
            "file:///etc/passwd",
            "gopher://example.com/",
        ):
            resp = client.post("/v1/extract", json={"url": url})
            assert resp.status_code == 422, f"{url} was not refused"
            assert resp.json()["code"] == "validation_error"

    def test_short_url_is_422(self, client):
        assert client.post("/v1/extract", json={"url": "ab"}).status_code == 422

    def test_unknown_format_is_422(self, client):
        resp = client.post("/v1/extract", json={"url": "https://example.com/x", "format": "exe"})
        assert resp.status_code == 422


class TestAdminValidation:
    def test_ingest_refuses_ssrf_targets(self, client):
        resp = client.post("/v1/admin/ingest", json={"url": "http://169.254.169.254/"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_ingest_refuses_internal_hostnames(self, client):
        resp = client.post("/v1/admin/ingest", json={"url": "http://redis:6379/"})
        assert resp.status_code == 422

    def test_key_name_is_validated(self, client):
        assert client.post("/v1/admin/keys", json={"name": ""}).status_code == 422

    def test_revoke_rejects_non_uuid(self, client):
        assert client.delete("/v1/admin/keys/not-a-uuid").status_code == 422


class TestBodySizeLimit:
    def test_oversized_body_is_413(self, client):
        payload = {"url": "https://example.com/", "pad": "x" * 300_000}
        resp = client.post("/v1/extract", json=payload)
        assert resp.status_code == 413
        assert resp.json()["code"] == "payload_too_large"

    def test_normal_body_is_accepted(self, client):
        resp = client.post("/v1/extract", json={"url": "https://example.com/"})
        assert resp.status_code != 413


class TestMethodNotAllowed:
    def test_search_rejects_post(self, client):
        assert client.post("/v1/search", json={"q": "x"}).status_code == 405

    def test_extract_rejects_get(self, client):
        assert client.get("/v1/extract").status_code == 405

    def test_405_uses_the_standard_envelope(self, client):
        body = client.post("/v1/search", json={"q": "x"}).json()
        assert body["code"] == "method_not_allowed"
        assert set(body) == {"code", "message", "details"}


class TestCaching:
    def test_cache_hit_reports_fresh_took_ms(self):
        """A 2 ms cache hit used to report the original multi-second latency."""
        import asyncio

        from app.schemas import SearchRequest, SearchResult
        from app.search.base import ProviderQuery, ProviderResult
        from app.search.orchestrator import SearchOrchestrator

        from tests.test_orchestrator import FakeProvider, mk

        class C:
            def __init__(self):
                self.store = {}

            async def get(self, key):
                return self.store.get(key)

            async def set(self, key, value, ttl_seconds=300):
                self.store[key] = value
                return True

        async def run():
            cache = C()
            p = FakeProvider("local", [mk("T", "https://a.com/1", "local")])
            orch = SearchOrchestrator(providers={"local": p}, cache=cache)
            await orch.search(SearchRequest(q="x", cache=True))
            hit = await orch.search(SearchRequest(q="x", cache=True))
            assert hit.cache_hit is True
            assert hit.took_ms < 1000

        asyncio.run(run())


class TestCacheKeyIsolation:
    def test_domain_filter_changes_the_cache_key(self):
        """Otherwise a filtered query serves another query's cached results."""
        from app.cache import cache_key

        base = cache_key("search", "q", "all", 10, None, "all", None, (), ())
        with_dom = cache_key("search", "q", "all", 10, None, "all", None, ("a.com",), ())
        assert base != with_dom

    def test_different_pages_have_different_keys(self):
        from app.cache import cache_key

        assert cache_key("search", "q", "all", 10) != cache_key("search", "q", "all", 20)
