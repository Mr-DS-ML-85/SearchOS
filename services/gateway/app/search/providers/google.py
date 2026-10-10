"""Google web and image search through a self-hosted Proxify gateway.

Proxify (github.com/Mr-DS-ML-85/proxify) fetches the results page in its headful
VM Chrome, which carries the operator's own browser session. SearchOS only
parses what comes back.

Two things this module deliberately does:

- **No captcha solving.** Proxify's /fetch runs its captcha bridge on a
  challenge page unless the request carries ``X-Orchestrator-Challenge-Solve:
  skip``. Every request here sends it, and a challenge page is reported as a
  failed tier so the cascade moves on. Nothing retries a blocked page.
- **Link resolution.** Google now emits result links as ``/goto?url=<token>``
  where the token is opaque; the destination exists only as the redirect target.
  Each kept link is followed once through Proxify's cheap ``simple`` strategy.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider, domain_allowed, domain_of

logger = get_logger("search.providers.google")

GOOGLE = "https://www.google.com"
NO_CAPTCHA_SOLVE = {"X-Orchestrator-Challenge-Solve": "skip"}
_TIME_RANGE = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


class ProxifyError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        #: The target's HTTP status when Proxify reached it, so a caller can
        #: tell "the page is gone" (404) from "Proxify could not get through".
        self.status_code = status_code


class ProxifyClient:
    def __init__(self, base_url: str, *, timeout: float = 45.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def fetch(
        self,
        url: str,
        *,
        browser: str | None = None,
        timeout: float | None = None,
        wait_selector: str | None = None,
        raw: bool = False,
    ):
        """POST /fetch. Returns the response dict, or raises ProxifyError.

        Proxify sets ``success: false`` for 4xx/5xx and for block or captcha
        pages, so a False here is never a page worth parsing.
        """
        import httpx

        budget = timeout or self.timeout
        body = {
            "url": url,
            "timeout": budget,
            "headers": dict(NO_CAPTCHA_SOLVE),
        }
        # None lets Proxify pick and escalate strategies itself.
        if browser:
            body["browser"] = browser
        if raw:
            # Bytes in body_b64 and no DOM cleaning: the caller extracts.
            body["raw"] = True
        if wait_selector:
            body["wait_selector"] = wait_selector
        try:
            # Proxify's own timeout plus headroom for its queueing.
            async with httpx.AsyncClient(timeout=budget + 15) as client:
                resp = await client.post(f"{self.base_url}/fetch", json=body)
        except httpx.HTTPError as e:
            raise ProxifyError(f"proxify unreachable: {e.__class__.__name__}: {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise ProxifyError(f"proxify returned non-JSON (HTTP {resp.status_code})") from e
        if resp.status_code >= 400 or not data.get("success"):
            reason = data.get("error") or data.get("detail") or f"HTTP {resp.status_code}"
            raise ProxifyError(f"proxify: {reason}", status_code=data.get("status_code"))
        return data


def google_url(path: str, params: dict) -> str:
    return f"{GOOGLE}{path}?{urlencode({k: v for k, v in params.items() if v})}"


def _with_site_operators(q: str, domains: list[str] | None, exclude: list[str] | None) -> str:
    parts = [q]
    if domains:
        parts.append(" OR ".join(f"site:{d}" for d in domains))
    parts.extend(f"-site:{d}" for d in exclude or [])
    return " ".join(parts)


def _href_target(href: str) -> str | None:
    """A result href as either a final URL or a Google URL that redirects to one."""
    if not href:
        return None
    if href.startswith("/url?"):
        # Older SERP markup carries the destination in plain text.
        target = parse_qs(urlsplit(href).query).get("q", [None])[0]
        return target if target and target.startswith("http") else None
    if href.startswith("/goto?"):
        return GOOGLE + href
    if href.startswith("http") and not urlsplit(href).netloc.endswith("google.com"):
        return href
    return None


def parse_web(html: str) -> list[dict]:
    """Organic results: ``{title, href, snippet}``, in page order."""
    import lxml.html

    if not html:
        return []
    tree = lxml.html.fromstring(html)
    out: list[dict] = []
    seen: set[str] = set()
    for anchor in tree.xpath("//a[.//h3][@href]"):
        target = _href_target(anchor.get("href"))
        if not target or target in seen:
            continue
        seen.add(target)
        title = anchor.xpath(".//h3")[0].text_content().strip()
        container = anchor
        for _ in range(8):
            parent = container.getparent()
            if parent is None:
                break
            container = parent
            if "MjjYud" in (container.get("class") or ""):
                break
        snippet_nodes = container.xpath('.//*[@data-sncf] | .//div[contains(@class, "VwiC3b")]')
        snippet = " ".join(snippet_nodes[0].text_content().split()) if snippet_nodes else ""
        # The snippet block ends with Google's own "Read more" link text.
        snippet = snippet.removesuffix("Read more").rstrip()
        out.append({"title": title, "href": target, "snippet": snippet})
    return out


def _thumbnail(element):
    for img in element.xpath(".//img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("https://encrypted-tbn") or src.startswith("data:image/jpeg"):
            return img
    return None


def parse_images(html: str) -> list[dict]:
    """Image results: ``{title, href, thumbnail, site}``.

    Each card holds exactly one /goto link (source page, with favicon, title
    and site name) and a sibling thumbnail. Climbing from the link to the first
    ancestor with a thumbnail finds the card; an ancestor holding several links
    is a wrapper, not a card, and is skipped. Inline ``data:`` thumbnails are
    dropped: they are not addressable and bloat a response by kilobytes each.
    """
    import lxml.html

    if not html:
        return []
    tree = lxml.html.fromstring(html)
    out: list[dict] = []
    seen: set[str] = set()
    for anchor in tree.xpath('//a[starts-with(@href, "/goto")]'):
        card = anchor
        img = None
        for _ in range(3):
            card = card.getparent()
            if card is None:
                break
            img = _thumbnail(card)
            if img is not None:
                break
        if img is None or len(card.xpath('.//a[starts-with(@href, "/goto")]')) != 1:
            continue
        thumb = img.get("src") or img.get("data-src") or ""
        if not thumb.startswith("https://"):
            continue
        href = GOOGLE + anchor.get("href")
        if href in seen:
            continue
        seen.add(href)
        title = (img.get("alt") or "").strip()
        text = " ".join(anchor.text_content().split())
        site = text[len(title) :].strip(" |·-") if title and text.startswith(title) else ""
        out.append({"title": title or text, "href": href, "thumbnail": thumb, "site": site or None})
    return out


async def resolve_links(client: ProxifyClient, hrefs: list[str]) -> list[str | None]:
    """Follow each Google redirect link to its destination; None where it fails."""
    settings = get_settings()
    gate = asyncio.Semaphore(max(settings.proxify_resolve_concurrency, 1))

    async def one(href: str) -> str | None:
        if not href.startswith(GOOGLE):
            return href
        async with gate:
            try:
                data = await client.fetch(
                    href, browser=settings.proxify_resolve_browser, timeout=settings.proxify_resolve_timeout_seconds
                )
            except ProxifyError as e:
                logger.debug("goto resolve failed: %s", e)
                return None
        final = data.get("final_url") or ""
        # Still on google.com means the redirect did not happen (consent wall,
        # interstitial); a google.com URL is not the result the caller wants.
        if not final.startswith("http") or urlsplit(final).netloc.endswith("google.com"):
            return None
        return final

    return list(await asyncio.gather(*(one(h) for h in hrefs)))


def build_client() -> ProxifyClient | None:
    settings = get_settings()
    if not settings.proxify_url:
        return None
    return ProxifyClient(settings.proxify_url, timeout=settings.proxify_timeout_seconds)


class GoogleProvider(SearchProvider):
    """Google web results as a cascade tier (also pinnable as sources=google)."""

    name = "google"

    def __init__(self, client: ProxifyClient) -> None:
        self.settings = get_settings()
        self.client = client

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)
        url = google_url(
            "/search",
            {
                "q": _with_site_operators(query.q, query.domains, query.exclude_domains),
                "hl": (query.language or "en").split("-")[0],
                "gl": query.country,
                "tbs": _TIME_RANGE.get(query.time_range),
                "num": 10,
            },
        )
        try:
            page = await self.client.fetch(url, browser=self.settings.proxify_browser, wait_selector="#search")
        except ProxifyError as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            result.error = f"google: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        parsed = parse_web(page.get("html") or "")[: query.num_results]
        if not parsed:
            result.raw = {"empty": True}
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        targets = await resolve_links(self.client, [p["href"] for p in parsed])
        unresolved = 0
        for rank, (item, target) in enumerate(zip(parsed, targets, strict=True), start=1):
            if not target:
                unresolved += 1
                continue
            if not domain_allowed(target, query.domains, query.exclude_domains):
                continue
            result.results.append(
                SearchResult(
                    title=item["title"] or target,
                    url=target,
                    snippet=item["snippet"][:1200],
                    source=self.name,
                    rank=rank,
                    domain=domain_of(target),
                    # Google gives an order, not a score.
                    score=round(1.0 - (rank - 1) * 0.01, 4),
                )
            )
        result.raw = {"parsed": len(parsed), "unresolved_links": unresolved}
        result.latency_ms = (time.monotonic() - start) * 1000
        get_metrics().search_duration.labels(provider=self.name).observe(result.latency_ms / 1000)
        return result


async def search_images(client: ProxifyClient, q: str, limit: int, *, resolve: bool = True) -> list[dict]:
    """Google Images results, source pages resolved for the first ``limit``."""
    settings = get_settings()
    page = await client.fetch(
        google_url("/search", {"q": q, "udm": 2, "hl": "en"}),
        browser=settings.proxify_browser,
        wait_selector="#search",
    )
    items = parse_images(page.get("html") or "")[:limit]
    if resolve and items:
        for item, target in zip(items, await resolve_links(client, [i["href"] for i in items]), strict=True):
            item["source_url"] = target
    return items
