from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.registry import REGISTRY


class Metrics:
    def __init__(self) -> None:
        self.requests_total = Counter(
            "searchos_requests_total",
            "Total API requests",
            ["endpoint", "status"],
        )
        self.search_total = Counter(
            "searchos_search_total",
            "Total search queries",
            ["provider", "hit"],
        )
        self.extract_total = Counter(
            "searchos_extract_total",
            "Total extraction requests",
            ["status"],
        )
        self.search_duration = Histogram(
            "searchos_search_duration_seconds",
            "Search duration",
            ["provider"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self.extract_duration = Histogram(
            "searchos_extract_duration_seconds",
            "Extract duration",
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self.cache_hits = Counter("searchos_cache_hits_total", "Cache hits")
        self.cache_misses = Counter("searchos_cache_misses_total", "Cache misses")
        self.provider_errors = Counter(
            "searchos_provider_errors_total",
            "Provider errors",
            ["provider", "kind"],
        )
        self.rate_limited = Counter(
            "searchos_rate_limited_total",
            "Rate limited requests",
            ["window"],
        )
        self.upstream_latency = Histogram(
            "searchos_upstream_latency_seconds",
            "Upstream provider latency",
            ["provider"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self.documents_indexed = Counter("searchos_documents_indexed_total", "Documents indexed")
        self.federated_peer_errors = Counter(
            "searchos_federated_peer_errors_total",
            "Failed federated peer calls, by peer",
            ["peer"],
        )
        self.semantic_cache_hits = Counter(
            "searchos_semantic_cache_hits_total",
            "Searches answered from a near-identical earlier query",
        )
        self.deduped_near_duplicates = Counter(
            "searchos_deduped_near_duplicates_total",
            "Results dropped because a near-identical document was already ranked higher",
        )
        self.crawl_politeness_wait = Histogram(
            "searchos_crawl_politeness_wait_seconds",
            "Time the crawler spent waiting to respect per-origin politeness",
        )
        self.chunks_indexed = Counter("searchos_chunks_indexed_total", "Chunks indexed")
        self.queue_depth = Gauge("searchos_queue_depth", "Pending ingest jobs")
        self.embedding_failures = Counter(
            "searchos_embedding_failures_total",
            "Documents indexed without embeddings because the embedder raised",
        )
        self.embedding_skipped = Counter(
            "searchos_embedding_skipped_total",
            "Documents indexed without embeddings, by reason (unavailable|error|no_vectors)",
            ["reason"],
        )
        self.documents_without_vectors = Counter(
            "searchos_documents_without_vectors_total",
            "Documents stored with no embedding vectors (keyword-only retrieval)",
        )
        self.ingest_jobs_running = Gauge("searchos_ingest_jobs_running", "Ingest jobs currently running")
        self.ingest_jobs_failed = Gauge("searchos_ingest_jobs_failed", "Ingest jobs failed after retries")
        self.auth_failures = Counter(
            "searchos_auth_failures_total",
            "Failed authentication attempts",
            ["reason"],
        )
        self.blocked_requests = Counter(
            "searchos_blocked_requests_total",
            "Requests refused by policy (SSRF, body size, private network)",
            ["reason"],
        )


_metrics = Metrics()


def get_metrics() -> Metrics:
    return _metrics


def render_metrics() -> bytes:
    """Render the registry, aggregating across uvicorn workers when configured.

    With ``--workers 2`` each process has its own registry, so a scrape of the
    single gateway target returned one random worker's numbers and roughly half
    of all traffic was invisible. PROMETHEUS_MULTIPROC_DIR fixes that.
    """
    import os

    from prometheus_client import CollectorRegistry, generate_latest, multiprocess

    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if multiproc_dir:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=multiproc_dir)
        return generate_latest(registry)
    return generate_latest(REGISTRY)
