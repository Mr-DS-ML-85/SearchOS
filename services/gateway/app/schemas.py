from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# "web" is the SearXNG+Tavily+Exa+Jina cascade. The individual tiers are also
# addressable so a caller can test or pin one upstream.
SearchSource = Literal["all", "local", "vector", "web", "searxng", "tavily", "exa", "jina", "federated"]


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
    offset: int = Field(0, ge=0, le=1000)
    cursor: str | None = Field(
        None,
        description=(
            "Opaque cursor from a previous response's next_cursor. Preferred over offset "
            "for deep paging: OFFSET makes Postgres walk and discard every skipped row, "
            "so page 50 costs 50x page 1."
        ),
    )
    cache: bool = True

    @model_validator(mode="before")
    @classmethod
    def _accept_comma_lists(cls, data: Any) -> Any:
        """Normalise comma-separated domains and reject self-defeating filters.

        Sequence fields on a Depends()-bound model are not registered as query
        parameters by FastAPI, so these arrive as comma-joined strings.
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for field in ("domains", "exclude_domains"):
            value = out.get(field)
            if isinstance(value, str):
                out[field] = [i.strip() for i in value.split(",") if i.strip()] or None
            elif isinstance(value, tuple):
                out[field] = list(value) or None

        def _normalise(raw: Any) -> set[str]:
            if isinstance(raw, str):
                raw = [i.strip() for i in raw.split(",") if i.strip()]
            return {str(item).lower().strip().strip(".") for item in (raw or [])}

        clash = _normalise(out.get("domains")) & _normalise(out.get("exclude_domains"))
        if clash:
            # Include-all-then-exclude-all can never match; silently returning
            # nothing reads as "no such content exists".
            raise ValueError(
                "domains and exclude_domains both contain "
                + ", ".join(sorted(clash))
                + " — no document could ever match"
            )
        return out

    @model_validator(mode="after")
    def _reject_blank_query(self) -> SearchRequest:
        """A whitespace-only query matches nothing but still costs a full search.

        Missing `query` was already rejected; this closes the adjacent case so
        validation is consistent and an agent in a retry loop cannot hammer
        SearXNG with blank queries.
        """
        if not self.q.strip():
            raise ValueError("q must not be blank")
        return self


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
    total: int = Field(..., description="Total fused matches, before pagination")
    returned: int = Field(..., description="Results in this page")
    has_more: bool = Field(
        ...,
        description="False when offset+returned >= total. Disambiguates 'no matches' from 'paged past the end'",
    )
    took_ms: int
    cache_hit: bool
    degraded: list[str]
    providers: list[str]
    top_score: float | None = Field(
        None,
        description="Highest relevance score in this page; None when nothing cleared the threshold",
    )
    low_confidence: bool = Field(
        False,
        description=(
            "True when no result cleared the low-confidence bar. The corpus may simply "
            "not cover the query; treat these results as a weak match rather than an answer."
        ),
    )
    min_score: float | None = Field(None, description="Relevance cutoff applied to this query")
    next_cursor: str | None = Field(
        None,
        description="Cursor for the next page, or null at the end. Pass back as `cursor`.",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Query completions; only populated when `include_suggestions` is set.",
    )
    semantic_cache_hit: bool = Field(
        False,
        description="True when the answer was reused from a near-identical earlier query",
    )
    semantic_cache_match: str | None = Field(
        None,
        description="The earlier query whose result was reused, when semantic_cache_hit is true",
    )
    semantic_cache_similarity: float | None = Field(None, description="Cosine similarity of the reuse")
    operators_applied: dict[str, Any] | None = Field(
        None,
        description="Operators parsed out of `q` (site/filetype/phrase/negation), or null.",
    )


class ExtractRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=4096)
    format: Literal["text", "markdown", "json"] = "markdown"
    max_chars: int | None = Field(None, ge=1, le=5_000_000)

    @model_validator(mode="after")
    def _reject_blank_url(self) -> ExtractRequest:
        if not self.url.strip():
            raise ValueError("url must not be blank")
        return self


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
    format: str = Field("text", description="Format `content` is rendered in")
    #: Only populated for format='json': structured page metadata.
    metadata: dict[str, Any] | None = None


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
    uptime_seconds: float = Field(..., description="Seconds since gateway start")
    # Index-completeness signals. `documents` and `chunks` alone cannot
    # distinguish a healthy index from one where chunking or embedding silently
    # stopped: documents kept growing while `chunks` stayed frozen, which is how
    # a fully non-functional vector index stayed invisible behind a
    # healthy-looking dashboard.
    documents_without_chunks: int = Field(0, description="Indexed documents with no chunks — should be 0")
    chunks_with_embedding: int = Field(0, description="Chunks carrying a vector")
    chunks_without_embedding: int = Field(0, description="Chunks with a NULL vector")
    embedding_coverage: float = Field(0.0, description="Fraction of documents that have at least one embedded chunk")
    vector_search_available: bool = Field(False, description="Whether the vector provider is usable right now")
