"""Federated search across remote SearchOS instances.

Enterprise search is rarely one index. A team wants its own private corpus
searched together with a shared one, or a deployment wants to search a regional
index and a global one without the caller knowing which is which. This provider
fans a query out to every configured peer and merges the answers.

Design points that are not obvious:

- **Peers are trusted to return results, not to execute anything.** Each peer is
  called with the same query and the same filters; nothing from a peer's response
  is interpreted except the documented result shape.
- **One slow peer must not sink the request.** Each call is bounded by its own
  timeout and failures are reported through ``degraded``, matching how the other
  providers behave.
- **A peer that is down is remembered briefly.** Without that, every query pays
  the full timeout of every dead peer, which is the same failure the web cascade
  already had to solve.

Results merge with the same Reciprocal Rank Fusion the rest of the pipeline uses,
so a peer that ranks something highly is trusted the same way the local index is.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider, domain_of

logger = get_logger("search.providers.federated")

# A peer that fails is skipped without a live call for this long.
_OUTAGE_SECONDS = 30.0

#: Sent on every request to a peer. A gateway receiving it does not federate
#: again (no `federated` provider, no `federated_web` tier), so peers whose own
#: web tiers are rate-limited cannot bounce one query between them forever.
HOP_HEADER = "X-SearchOS-Federated-Hop"

#: mode -> the `sources` values asked of each peer (requested in parallel).
_MODE_SOURCES = {
    # The peer's own index. local and vector separately, because older peers
    # accept no single value meaning "index only".
    "index": ("local", "vector"),
    # The peer's whole pipeline, including its live-web cascade.
    "all": ("all",),
    # Only the peer's live-web cascade: the rate-limit fallback tier.
    "web": ("web",),
}


def _describe_failure(exc: Exception, settings) -> str:
    """Turn a peer exception into something an operator can act on.

    The failure list surfaced only the exception class, so a peer rejecting our
    credentials came back as "peer-a: HTTPStatusError" — indistinguishable from
    a timeout, a DNS failure or a 500. The missing-key case was the expensive
    one: FEDERATED_ENDPOINTS set, FEDERATED_API_KEY unset, and the only clue was
    the generic "All search providers failed: federated".
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            if not (settings.federated_api_key or settings.bootstrap_admin_api_key):
                return f"HTTP {status} — no credential configured. Set FEDERATED_API_KEY (the peer requires auth)"
            return f"HTTP {status} — the configured credential was rejected by the peer"
        if status == 404:
            return "HTTP 404 — peer does not expose /v1/search (is it a SearchOS gateway?)"
        return f"HTTP {status}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout — peer did not respond in time"
    if isinstance(exc, httpx.ConnectError):
        return "connection refused — peer URL unreachable from this container (check the host/port)"
    return f"{exc.__class__.__name__}"


class FederatedProvider(SearchProvider):
    """Fan out to remote SearchOS gateways and merge with RRF.

    ``mode`` picks what peers are asked for (see ``_MODE_SOURCES``). The
    `federated` provider uses "index" (or "all" via FEDERATED_PEER_SOURCES);
    the `federated_web` cascade tier uses "web".
    """

    name = "federated"

    def __init__(
        self, endpoints: list[tuple[str, str]], timeout: float = 10.0, *, mode: str = "index", name: str | None = None
    ) -> None:
        # endpoints: [(name, base_url)] — the name prefixes each result's source
        # so a caller can tell which peer answered.
        self.settings = get_settings()
        self.endpoints = endpoints
        self.timeout = timeout
        self.mode = mode
        if name:
            self.name = name
        self._down_until: dict[str, float] = {}

    def _live(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        return [(n, u) for n, u in self.endpoints if self._down_until.get(n, 0.0) <= now]

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)

        if query.federated_hop:
            # This request came from a peer; federating again could loop.
            result.error = f"{self.name}: skipped on a federated hop"
            return result

        peers = self._live()
        if not peers:
            result.error = f"{self.name}: every peer is in outage"
            return result

        base_params: dict = {
            "q": query.q,
            "num_results": max(query.num_results, 10),
            "time_range": query.time_range,
            "cache": "true",
        }
        if query.language:
            base_params["language"] = query.language
        if query.domains:
            base_params["domains"] = query.domains
        if query.exclude_domains:
            base_params["exclude_domains"] = query.exclude_domains

        settings = self.settings
        headers = {
            "Authorization": f"Bearer {settings.federated_api_key or settings.bootstrap_admin_api_key}",
            HOP_HEADER: "1",
        }

        async def ask(client: httpx.AsyncClient, base: str, sources: str) -> dict:
            resp = await client.get(
                f"{base.rstrip('/')}/v1/search", params={**base_params, "sources": sources}, headers=headers
            )
            resp.raise_for_status()
            return resp.json()

        async def ask_peer(client: httpx.AsyncClient, name: str, base: str) -> tuple[str, list[dict] | None, str]:
            """One peer's results for this provider's mode, its sources in parallel.

            The index mode is the default because asking every peer for
            sources=all on every query made each run its own ~5s web cascade
            against a 5s timeout, so peers were degraded on most queries.
            Pooling web search across peers is still available, either always
            (FEDERATED_PEER_SOURCES=all) or only when this instance's own web
            tiers fail (the federated_web cascade tier).
            """
            outcomes = await asyncio.gather(
                *(ask(client, base, sources) for sources in _MODE_SOURCES[self.mode]), return_exceptions=True
            )
            rows: list[dict] = []
            seen: set[str] = set()
            errors = [o for o in outcomes if isinstance(o, BaseException)]
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    continue
                for row in outcome.get("results") or []:
                    url = row.get("url")
                    if url and url not in seen:
                        seen.add(url)
                        rows.append(row)
            if len(errors) == len(outcomes):
                # Only a peer that answered nothing at all is in outage; a peer
                # without embeddings still serves its FTS index.
                return name, None, _describe_failure(errors[0], settings)
            return name, rows, ""

        rankings: dict[str, list[str]] = {}
        metadata: dict[str, dict] = {}
        failures: list[str] = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # All peers at once: queried in turn, N peers cost N round trips.
            answers = await asyncio.gather(*(ask_peer(client, name, base) for name, base in peers))
        for name, rows, failure in answers:
            if rows is None:
                self._down_until[name] = time.monotonic() + _OUTAGE_SECONDS
                failures.append(f"{name}: {failure}")
                get_metrics().federated_peer_errors.labels(peer=name).inc()
                continue
            urls = [r["url"] for r in rows[: query.num_results]]
            if urls:
                rankings[name] = urls
                # The peer's title and snippet have to come along. Sending
                # bare URLs made every federated result unscoreable by the
                # cross-encoder downstream, so all of them fell below
                # RERANK_MIN_SCORE and the provider returned nothing.
                for row in rows[: query.num_results]:
                    metadata[row["url"]] = row

        if not rankings:
            result.latency_ms = (time.monotonic() - start) * 1000
            if failures:
                result.error = f"{self.name}: no peer returned results: " + "; ".join(failures)
            else:
                # Every peer answered 200 with nothing. That is an empty answer,
                # not a failure: reported as `degraded`, it made a healthy peer
                # with no matching documents look broken on most queries.
                result.raw = {"empty": True, "peers": [n for n, _ in peers]}
            return result

        merged = _rrf(rankings, k=self.settings.rrf_k)
        merged = merged[: query.num_results]
        for rank, (url, score, peer) in enumerate(merged, start=1):
            peer_row = metadata.get(url, {})
            result.results.append(
                SearchResult(
                    title=peer_row.get("title") or url,
                    url=url,
                    snippet=(peer_row.get("snippet") or "")[:1200],
                    source=f"{self.name}:{peer}",
                    score=score,
                    rank=rank,
                    domain=domain_of(url),
                )
            )
        result.raw = {"peers": sorted(rankings), "failed": failures, "dropped_peers": len(self.endpoints) - len(peers)}
        result.latency_ms = (time.monotonic() - start) * 1000
        return result


def _rrf(rankings: dict[str, list[str]], k: int = 60) -> list[tuple[str, float, str]]:
    """Reciprocal Rank Fusion across peers.

    A URL appearing in several peers' lists scores higher than one that appears
    in a single list at the same position, which is the property that makes
    agreement between independent indices meaningful.
    """
    scores: dict[str, float] = {}
    peer_of: dict[str, str] = {}
    for peer, urls in rankings.items():
        for position, url in enumerate(urls):
            scores[url] = scores.get(url, 0.0) + 1.0 / (k + position + 1)
            peer_of.setdefault(url, peer)
    return sorted(((url, score, peer_of[url]) for url, score in scores.items()), key=lambda t: -t[1])


def build_federated(settings=None) -> FederatedProvider | None:
    cfg = settings or get_settings()
    peers: list[tuple[str, str]] = []
    for raw in cfg.federated_peers:
        if "=" in raw:
            name, url = raw.split("=", 1)
        else:
            name, url = raw.rstrip("/").rsplit("//", 1)[-1].split("/", 1)[0], raw
        peers.append((name.strip(), url.strip()))
    if not peers:
        return None
    if cfg.federated_peer_sources == "all":
        # The peer runs its own web cascade on every query; 5s is not enough.
        return FederatedProvider(peers, timeout=cfg.federated_web_timeout_seconds, mode="all")
    return FederatedProvider(peers, timeout=cfg.federated_timeout_seconds, mode="index")


def build_federated_web(settings=None) -> FederatedProvider | None:
    """The `federated_web` cascade tier: peers run the web search for us."""
    cfg = settings or get_settings()
    if not cfg.federated_web_fallback:
        return None
    base = build_federated(cfg)
    if base is None:
        return None
    return FederatedProvider(
        base.endpoints, timeout=cfg.federated_web_timeout_seconds, mode="web", name="federated_web"
    )
