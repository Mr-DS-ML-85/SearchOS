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


def bare_host(value: str) -> str:
    """Lowercase a hostname and drop a leading ``www.`` so filters match subdomains."""
    h = (value or "").strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def domain_allowed(url: str, domains: list[str] | None, exclude_domains: list[str] | None) -> bool:
    """Whether ``url`` satisfies the caller's domain constraints.

    Not every upstream honours them. s.jina.ai has no domain parameter at all,
    and Exa's MCP server returned undsci.berkeley.edu alongside
    en.wikipedia.org despite being given ``includeDomains`` — so ``domains``
    has to be re-applied client-side or ``domains=wikipedia.org`` quietly
    returns unrelated sites.
    """
    included = [bare_host(d) for d in (domains or []) if d]
    excluded = [bare_host(d) for d in (exclude_domains or []) if d]
    if not included and not excluded:
        return True
    host = bare_host(url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0])
    if excluded and any(host == d or host.endswith("." + d) for d in excluded):
        return False
    if included and not any(host == d or host.endswith("." + d) for d in included):
        return False
    return True
