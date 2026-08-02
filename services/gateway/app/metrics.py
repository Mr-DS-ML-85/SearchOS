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
        self.chunks_indexed = Counter("searchos_chunks_indexed_total", "Chunks indexed")
        self.queue_depth = Gauge("searchos_queue_depth", "Pending ingest jobs")


_metrics = Metrics()


def get_metrics() -> Metrics:
    return _metrics


def render_metrics() -> bytes:
    from prometheus_client import generate_latest

    return generate_latest(REGISTRY)
