from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.schemas import SearchResult


@dataclass
class ProviderQuery:
    q: str
    language: str | None = None
    country: str | None = None
    time_range: str = "all"
    domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    num_results: int = 20
    offset: int = 0
    skip_when_local_hit: bool = False


@dataclass
class ProviderResult:
    provider: str
    query: str
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0
    raw: dict[str, Any] | None = None


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(self, query: ProviderQuery) -> ProviderResult:
        raise NotImplementedError


def domain_of(url: str) -> str:
    """Host portion of a URL, or "" when it will not parse.

    SearXNG populated this and the fallback providers did not, so results from
    Tavily/Exa/Jina came back with `domain: null` and could not be filtered by
    the caller.
    """
    import re

    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""
