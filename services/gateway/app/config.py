from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_DIM = 384


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"
    app_debug: bool = False
    log_level: str = "INFO"
    app_name: str = "searchos"
    app_version: str = "1.0.0"

    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

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
    render_timeout_seconds: float = 35.0
    render_max_bytes: int = 10485760

    searxng_url: str = ""
    searxng_timeout_seconds: float = 12.0
    searxng_local_ttl_seconds: int = 3600
    searxng_local_min_hits: int = 3

    local_index_enabled: bool = True
    local_index_fts_weight: float = 0.6
    local_index_vector_weight: float = 0.4
    local_index_k: int = 25

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = EMBEDDING_DIM
    embedding_device: str = "cpu"
    embedding_batch_size: int = 16

    fusion_mode: str = "rrf"
    rrf_k: int = 60
    rerank_enabled: bool = True
    rerank_top_k: int = 20
    default_num_results: int = 10
    max_num_results: int = 50

    search_cache_ttl_seconds: int = 300
    extract_cache_ttl_seconds: int = 3600

    extract_max_bytes: int = 5242880
    extract_timeout_seconds: float = 20.0
    extract_user_agent: str = "SearchOSBot/1.0 (+https://searchos.local/bot)"
    fetch_respect_robots: bool = True

    auth_enabled: bool = True
    bootstrap_admin_api_key: str = "change-me-to-a-long-random-secret"
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 600
    rate_limit_per_day: int = 5000

    worker_ingest_batch: int = 5
    worker_ingest_poll_interval_seconds: float = 2.0
    worker_snippet_batch: int = 10
    worker_snippet_poll_interval_seconds: float = 5.0
    worker_max_attempts: int = 3
    worker_stale_job_seconds: float = 300.0

    crawler_follow_links: bool = True
    crawler_max_depth: int = 2
    crawler_batch_size: int = 25
    crawler_follow_domains: str = "same"
    crawler_continuous_enabled: bool = True
    crawler_continuous_interval_seconds: float = 3600.0
    crawler_recrawl_after_seconds: float = 604800.0
    crawler_recrawl_batch: int = 100

    prometheus_enabled: bool = True
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

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
