from __future__ import annotations

import ipaddress
import json
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

EMBEDDING_DIM = 384

LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}

# Egress policy: these destination ranges are never reachable from
# ``/v1/extract``, the crawler, or the renderer. SSRF guard.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # RFC1918
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local / cloud metadata (169.254.169.254)
        "172.16.0.0/12",  # RFC1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.168.0.0/16",  # RFC1918
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "255.255.255.255/32",  # broadcast
    )
)


def is_blocked_ip(addr: str) -> bool:
    """True when ``addr`` is not a globally routable public address."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_blocked_ip(str(ip.ipv4_mapped))
    return any(ip in net for net in BLOCKED_NETWORKS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_debug: bool = False
    log_level: str = "INFO"
    app_name: str = "searchos"
    app_version: str = "1.0.0"

    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    db_pool_size: int = 10
    db_max_overflow: int = 20

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "searchos"
    postgres_password: str = "searchos"
    postgres_db: str = "searchos"
    database_url: str | None = None

    redis_url: str = "redis://redis:6379/0"
    redis_cache_db: int = 1
    redis_ratelimit_db: int = 2

    renderer_url: str = ""
    renderer_token: str = ""
    render_timeout_seconds: float = 35.0
    render_max_bytes: int = 10485760

    searxng_url: str = ""
    searxng_timeout_seconds: float = 6.0
    # Ordered fallbacks used when SearXNG fails or is in cooldown. First one
    # that answers wins; an unconfigured tier is skipped. The local FTS and
    # vector providers are unaffected by this chain.
    tavily_api_key: str = ""
    # Tavily serves /search with no account via an X-Tavily-Access-Mode: keyless
    # header, so the tier is available on a fresh install. Set false to require
    # a key.
    tavily_keyless: bool = True
    tavily_timeout_seconds: float = 6.0
    # basic is ~4x faster than advanced. The cross-encoder reranks downstream,
    # so Tavily's deeper pass is not worth the latency in a fallback tier.
    tavily_search_depth: str = "basic"
    exa_api_key: str = ""
    # Route Exa through its hosted MCP server (free, anonymous) rather than
    # the REST API, which returns 402 x402 without a funded key.
    exa_mcp_enabled: bool = True
    exa_use_mcp: bool = True
    exa_advanced: bool = False
    exa_timeout_seconds: float = 6.0
    jina_api_key: str = ""
    jina_timeout_seconds: float = 8.0
    # After the whole chain fails, refuse live attempts for this long and serve
    # the degraded local result instead. Without it every request pays the full
    # timeout of every dead tier.
    search_cascade_cooldown_seconds: float = 30.0
    search_cascade_enabled: bool = True
    searxng_local_ttl_seconds: int = 3600
    searxng_local_min_hits: int = 3

    local_index_enabled: bool = True
    local_index_fts_weight: float = 0.6
    local_index_vector_weight: float = 0.4

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # CLIP for image/multimodal search. Separate from the text embedder:
    # they are different vector spaces and are never compared.
    clip_model: str = "openai/clip-vit-base-patch32"
    # Images smaller than this are usually icons or tracking pixels.
    image_min_dimension: int = 128
    image_max_bytes: int = 8_000_000
    image_max_per_document: int = 12
    images_enabled: bool = True
    # ColBERT late interaction. Off by default: it costs ~64KB per chunk on top
    # of the chunk embedding, and most queries do not need token-level matching.
    colbert_enabled: bool = False
    colbert_model: str = "colbert-ir/colbertv2.0"
    # Chunks above this candidate count are not rescored. MaxSim is O(query x
    # doc) per chunk in Python, so the tail costs more than it returns.
    colbert_top_n: int = 50
    # How much MaxSim moves a result relative to the cross-encoder. Blending
    # rather than replacing keeps `score` monotonic with `rank`; reordering
    # without rescoring produced results whose order contradicted their scores.
    #
    # Calibrated, not guessed: MaxSim here lands around 0.06-0.09 (it is divided
    # by query token count), while the cross-encoder emits logits of 3-6. At a
    # weight of 2 the ColBERT term moved a score by ~0.16 and never once changed
    # an ordering — it was wired in and doing nothing. 30 puts its contribution
    # in the same range as the signal it is meant to correct.
    colbert_weight: float = 30.0
    embedding_device: str = "cpu"
    embedding_batch_size: int = 16

    rrf_k: int = 60
    rerank_enabled: bool = True
    rerank_top_k: int = 20
    # Cross-encoder model. Empty disables semantic reranking and falls back to
    # the heuristic boosts, whose scores are all near-identical and therefore
    # cannot support a meaningful relevance threshold.
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L6-v2"
    rerank_batch_size: int = 32
    # Candidates handed to the cross-encoder. Beyond this the tail cannot
    # reach the result page, so scoring it is wasted inference.
    rerank_candidate_pool: int = 30
    # Thresholds are in cross-encoder logit units, not 0-1. Measured on this corpus
    # with ms-marco-MiniLM-L6-v2: relevant queries scored 4.94-9.34, nonsense 1.16
    # and below. A sigmoid squashed that to 0.995-1.0 vs 0.76, too narrow to cut on.
    # Retune if rerank_model changes.
    rerank_min_score: float = 3.0
    # When the best surviving score is under this, the response is flagged
    # low-confidence so a caller can tell "nothing here matches" from "here are the
    # best of a weak set".
    rerank_low_confidence_score: float = 4.5

    search_cache_ttl_seconds: int = 300
    # Empty and low-confidence responses expire fast so a transient miss does
    # not pin itself for the full TTL.
    search_negative_cache_ttl_seconds: int = 30
    # Reuse a previous answer for a differently-worded but near-identical
    # query. Deliberately conservative: 0.92 already matches paraphrases and
    # a higher threshold stops catching them, while 0.85 starts matching
    # merely-related questions and would answer them with the wrong result.
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.92
    # Comma-separated list of `name=url` peers. A plain string rather than a
    # list because pydantic-settings JSON-decodes list-typed fields from the
    # environment and rejects a comma-separated value outright.
    federated_endpoints: str = ""
    federated_api_key: str = ""
    federated_timeout_seconds: float = 5.0
    extract_cache_ttl_seconds: int = 3600

    extract_max_bytes: int = 5242880
    extract_timeout_seconds: float = 20.0
    extract_user_agent: str = "SearchOSBot/1.0 (+https://searchos.local/bot)"
    fetch_respect_robots: bool = True
    fetch_max_redirects: int = 5
    robots_timeout_seconds: float = 10.0
    fetch_block_private_networks: bool = True
    fetch_allow_private_networks: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_request_body_bytes: int = 262144

    auth_enabled: bool = True
    bootstrap_admin_api_key: str = ""
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 600
    rate_limit_per_day: int = 5000
    rate_limit_auth_failures_per_minute: int = 10
    rate_limit_health_endpoints: bool = False

    worker_ingest_poll_interval_seconds: float = 2.0
    worker_snippet_batch: int = 10
    worker_snippet_poll_interval_seconds: float = 5.0
    worker_max_attempts: int = 3
    worker_stale_job_seconds: float = 300.0
    worker_retry_base_seconds: float = 30.0
    worker_metrics_port: int = 9101
    worker_run_migrations: bool = False

    chunk_max_tokens: int = 512
    chunk_overlap_tokens: int = 64

    backfill_enabled: bool = True
    backfill_interval_seconds: float = 3600.0
    backfill_batch_size: int = 50
    # Delay between passes while work remains. Draining at one batch per
    # backfill_interval took hours on a corpus with hundreds of chunk-less
    # documents.
    backfill_drain_interval_seconds: float = 2.0

    crawler_follow_links: bool = True
    crawler_max_depth: int = 2
    crawler_batch_size: int = 25
    crawler_follow_domains: Literal["same", "all", "none"] = "same"
    crawler_max_urls_per_seed: int = 5000
    crawler_continuous_enabled: bool = True
    crawler_continuous_interval_seconds: float = 3600.0
    crawler_recrawl_after_seconds: float = 604800.0
    crawler_recrawl_batch: int = 100
    # Minimum spacing between fetches to the same origin. robots.txt's
    # Crawl-delay is honoured when present and must be at least this.
    crawler_min_delay_seconds: float = 1.0

    search_max_offset: int = 1000
    # Web results queued for full-text enrichment per search. Anything past
    # this is served as a search-engine snippet, so a larger N means a larger
    # share of the response is real content.
    search_enrich_top_n: int = 15
    # Collapse results whose 64-bit content fingerprints are within this
    # Hamming distance. Measured: a mirror with a footer added differs by 1,
    # a 75% truncation by 0, and a different article on the same topic by ~28.
    dedupe_near_duplicates: bool = True
    dedupe_simhash_threshold: int = 6
    search_max_candidates: int = 100
    vector_min_similarity: float = 0.0

    prometheus_enabled: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def _validate_log_level(cls, value: Any) -> str:
        level = str(value).upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(LOG_LEVELS)}, got {value!r}")
        return level

    @field_validator("cors_origins", "fetch_allow_private_networks", mode="before")
    @classmethod
    def _split_csv_or_json(cls, value: Any) -> Any:
        """Accept JSON arrays, bare ``*``, or comma-separated lists.

        pydantic-settings decodes complex fields as JSON, so ``CORS_ORIGINS=*``
        (the value shipped in .env.example) raised at import time and crashed
        the process before the lifespan ever ran. We parse every string form
        ourselves and hand pydantic a real list.
        """
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"expected a JSON array, got {raw!r}") from exc
                if not isinstance(parsed, list):
                    raise ValueError(f"expected a JSON array, got {raw!r}")
                return [str(item).strip() for item in parsed if str(item).strip()]
            if raw == "*":
                return ["*"]
            if raw == "null":
                return []
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("crawler_follow_domains", mode="before")
    @classmethod
    def _validate_follow_domains(cls, value: Any) -> str:
        value = str(value).strip().lower()
        if value not in {"same", "all", "none"}:
            raise ValueError(f"crawler_follow_domains must be same|all|none, got {value!r}")
        return value

    @field_validator("fetch_allow_private_networks", mode="after")
    @classmethod
    def _normalise_allowlist(cls, value: list[str]) -> list[str]:
        """Bare hostnames imply the whole host; keep them for host matching."""
        return [item.strip().lower() for item in value if item and item.strip()]

    @model_validator(mode="after")
    def _validate_security_posture(self) -> Settings:
        if not self.auth_enabled:
            return self
        key = self.bootstrap_admin_api_key
        if not key or key.startswith("change-me"):
            raise ValueError(
                "AUTH_ENABLED is true but BOOTSTRAP_ADMIN_API_KEY is unset or still a "
                'placeholder. Generate one with: python3 -c "import secrets; '
                "print('sk_' + secrets.token_urlsafe(32))\""
            )
        if len(key) < 16:
            raise ValueError("BOOTSTRAP_ADMIN_API_KEY must be at least 16 characters")
        return self

    @property
    def cors_allows_credentials(self) -> bool:
        """Wildcard origins must not be paired with credentialed CORS.

        Starlette reflects the request Origin when allow_credentials is on, which
        lets any site drive the API (including admin routes) from a browser.
        """
        return "*" not in self.cors_origins

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{quote(self.postgres_user, safe='')}:"
            f"{quote(self.postgres_password, safe='')}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def federated_peers(self) -> list[str]:
        """Parsed from FEDERATED_ENDPOINTS; empty disables federation."""
        return [p.strip() for p in (self.federated_endpoints or "").split(",") if p.strip()]

    @property
    def cache_redis_url(self) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.redis_url)
        path = parts.path or "/0"
        path = path.rsplit("/", 1)[0] + f"/{self.redis_cache_db}"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    @property
    def ratelimit_redis_url(self) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.redis_url)
        path = parts.path or "/0"
        path = path.rsplit("/", 1)[0] + f"/{self.redis_ratelimit_db}"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@lru_cache
def get_settings() -> Settings:
    return Settings()
