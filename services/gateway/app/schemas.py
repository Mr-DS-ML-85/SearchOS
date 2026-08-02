from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SearchSource = Literal["all", "local", "vector", "web"]


class SearchRequest(BaseModel):
    q: str = Field(..., min_length=1, max_length=512, description="Search query")
    sources: SearchSource = "all"
    num_results: int = Field(10, ge=1, le=50)
    language: str | None = None
    country: str | None = None
    time_range: Literal["day", "week", "month", "year", "all"] = "all"
    freshness: str | None = None
    domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    offset: int = Field(0, ge=0)
    cache: bool = True


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    source: str
    score: float
    rank: int
    published: str | None = None
    domain: str | None = None
    favicon: str | None = None
    raw: dict[str, Any] | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    total: int
    took_ms: int
    cache_hit: bool
    degraded: list[str]
    providers: list[str]


class ExtractRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=4096)
    format: Literal["markdown", "text", "json"] = "markdown"
    max_chars: int | None = Field(None, ge=1, le=5_000_000)


class ExtractResponse(BaseModel):
    url: str
    final_url: str | None = None
    title: str | None = None
    content: str
    content_type: str
    fetched_at: str
    cached: bool = False
    truncated: bool = False
    took_ms: int


class IngestRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=4096)
    kind: Literal["url", "sitemap"] = "url"


class IngestResponse(BaseModel):
    job_id: str
    url: str
    kind: str
    status: str


class HealthResponse(BaseModel):
    status: str
    services: dict[str, Any]


class StatsResponse(BaseModel):
    documents: int
    chunks: int
    api_keys: int
    ingest_pending: int
    uptime_seconds: float
