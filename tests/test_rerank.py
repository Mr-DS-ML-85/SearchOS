from __future__ import annotations

from app.schemas import SearchResult
from app.search.rerank import Reranker


def mk(
    title: str,
    score: float = 1.0,
    published: str | None = None,
    source: str = "local",
    domain: str = "example.com",
    url: str | None = None,
) -> SearchResult:
    return SearchResult(
        title=title,
        url=url or f"https://{domain}/{abs(hash(title))}",
        snippet="",
        source=source,
        score=score,
        rank=1,
        published=published,
        domain=domain,
    )


def test_rerank_boosts_title_matches():
    r1 = mk("Introduction to Machine Learning")
    r2 = mk("Something else entirely")
    reranked = Reranker(enabled=True, top_k=10).rerank("machine learning", [r2, r1])
    assert reranked[0].title == "Introduction to Machine Learning"


def test_rerank_disabled_passthrough():
    results = [mk("A", score=2.0), mk("B", score=1.0)]
    reranked = Reranker(enabled=False).rerank("x", results)
    assert reranked == results


def test_rerank_respects_top_k():
    results = [mk(f"Result {i}", score=float(i)) for i in range(20)]
    reranked = Reranker(enabled=True, top_k=5).rerank("query", results)
    assert len(reranked) == 5


def test_rerank_domain_weights():
    r1 = mk("A", score=1.0, domain="wikipedia.org")
    r2 = mk("B", score=1.0, domain="random-blog.com")
    weights = {"wikipedia.org": 1.5, "random-blog.com": 1.0}
    reranked = Reranker(enabled=True, top_k=10, domain_weights=weights).rerank("query", [r2, r1])
    assert reranked[0].url == r1.url


class TestFederatedFailureMessages:
    """A peer error must name the cause, not just the exception class.

    Surfacing only `HTTPStatusError` cost real debugging time: .env had
    FEDERATED_ENDPOINTS but no FEDERATED_API_KEY, the peer 401'd, and the whole
    response said "All search providers failed: federated" — pointing at the
    federation machinery when the fault was one missing env var.
    """

    @staticmethod
    def _status_error(code: int) -> Exception:
        import httpx

        request = httpx.Request("GET", "http://peer/v1/search")
        return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))

    @staticmethod
    def _settings(federated_key: str = "", bootstrap: str = ""):
        from types import SimpleNamespace

        return SimpleNamespace(federated_api_key=federated_key, bootstrap_admin_api_key=bootstrap)

    def test_missing_credential_names_the_setting(self):
        from app.search.providers.federated import _describe_failure

        message = _describe_failure(self._status_error(401), self._settings())
        assert "FEDERATED_API_KEY" in message

    def test_rejected_credential_differs_from_missing_credential(self):
        from app.search.providers.federated import _describe_failure

        message = _describe_failure(self._status_error(401), self._settings(federated_key="sk_x"))
        assert "rejected" in message and "FEDERATED_API_KEY" not in message

    def test_network_failures_are_distinguishable(self):
        import httpx
        from app.search.providers.federated import _describe_failure

        settings = self._settings(federated_key="sk_x")
        assert "404" in _describe_failure(self._status_error(404), settings)
        assert "timeout" in _describe_failure(httpx.ConnectTimeout("t"), settings)
        assert "unreachable" in _describe_failure(httpx.ConnectError("c"), settings)
