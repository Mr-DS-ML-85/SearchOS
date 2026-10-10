from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.errors import AppError, NotFoundError, ValidationError
from app.http import get_http_client
from app.metrics import get_metrics


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    content_type: str | None
    content: bytes
    headers: dict[str, str]
    #: "native" (renderer or direct GET) or "proxify" (fallback).
    via: str = "native"


class FetchError(AppError):
    code = "fetch_error"


class FetchTooLarge(FetchError):
    code = "fetch_too_large"


def _validate_egress(url: str) -> None:
    """Refuse requests to non-public destinations (SSRF guard)."""
    from app.errors import ProviderError
    from app.netguard import BlockedURLError, DNSResolutionError, validate_url

    try:
        validate_url(url)
    except DNSResolutionError as exc:
        # An unresolvable name is a DNS/typo problem, not a policy decision.
        # Reporting it as an egress violation sends debugging in the wrong
        # direction.
        get_metrics().blocked_requests.labels(reason="dns").inc()
        raise ProviderError(f"cannot resolve host for {url}: {exc}") from exc
    except BlockedURLError as exc:
        get_metrics().blocked_requests.labels(reason="ssrf").inc()
        raise ValidationError(f"URL refused by egress policy: {exc}") from exc


async def fetch_page(url: str, *, respect_robots: bool | None = None, max_bytes: int | None = None) -> FetchedPage:
    from app.extract.robots import RobotsChecker

    settings = get_settings()
    # Check before any network access, including the robots fetch.
    _validate_egress(url)

    robots = RobotsChecker()
    if respect_robots if respect_robots is not None else settings.fetch_respect_robots:
        if not await robots.allowed(url):
            get_metrics().extract_total.labels(status="robots_blocked").inc()
            raise ValidationError(f"robots.txt disallows fetching {url}")

    try:
        page = await _fetch_native(url, max_bytes=max_bytes)
    except (NotFoundError, FetchTooLarge):
        # Answers, not failures: a different client would get the same result.
        raise
    except FetchError as exc:
        return await _proxify_fallback(url, max_bytes=max_bytes, cause=exc)
    if _proxify_enabled() and looks_like_challenge(page):
        get_metrics().extract_total.labels(status="challenge_page").inc()
        return await _proxify_fallback(
            url, max_bytes=max_bytes, cause=FetchError(f"anti-bot challenge page served for {url}")
        )
    return page


async def _fetch_native(url: str, *, max_bytes: int | None = None) -> FetchedPage:
    from app.extract.render import render_page, should_render

    settings = get_settings()
    # Headless render first: gives the real DOM for SPAs and heavy-JS pages so
    # trafilatura can extract clean, LLM-ready content. Falls back to a plain GET.
    if settings.renderer_url and should_render(url):
        try:
            page = await render_page(url, max_bytes=max_bytes)
            if page is not None:
                # The renderer's own API call succeeding says nothing about the
                # *rendered page's* status: Chromium happily returns an empty
                # DOM for a 404 or a 500. Without this check a dead link was
                # reported as HTTP 200 with empty content, which is the worst
                # failure class for a search/extract API — the data looks fine.
                _raise_for_status(page.status_code, url)
                return page
        except ValidationError:
            # The renderer refused the URL under its egress policy; falling back
            # to a direct GET would bypass exactly that check.
            get_metrics().extract_total.labels(status="renderer_refused").inc()
            raise
        except (FetchError, httpx.HTTPError):
            get_metrics().extract_total.labels(status="renderer_fallback").inc()

    return await _get_following_redirects(url, max_bytes=max_bytes)


# Markers that appear only on interstitial challenge pages, not on ordinary
# pages behind the same vendor. Cloudflare injects /cdn-cgi/challenge-platform
# scripts into *normal* pages too, so that string is deliberately absent.
_CHALLENGE_MARKERS = (
    b"<title>just a moment...</title>",
    b"attention required! | cloudflare",
    b"cf_chl_opt",
    b"captcha-delivery.com",
    b"px-captcha",
)
# Challenge interstitials are small; a large page that mentions one of these
# strings in passing is content.
_CHALLENGE_MAX_BYTES = 64_000


def looks_like_challenge(page: FetchedPage) -> bool:
    if len(page.content) > _CHALLENGE_MAX_BYTES:
        return False
    if "html" not in (page.content_type or "text/html").lower():
        return False
    head = page.content.lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _proxify_enabled() -> bool:
    settings = get_settings()
    return bool(settings.proxify_url and settings.proxify_fetch_fallback)


async def _proxify_fallback(url: str, *, max_bytes: int | None, cause: FetchError) -> FetchedPage:
    """Retry a failed native fetch once through Proxify, or re-raise ``cause``.

    Proxify follows redirects itself, so the hop-by-hop egress validation of
    the native path cannot apply inside it; the landing URL is validated
    instead, and a page that ended up somewhere the policy forbids is refused.
    Its captcha bridge is disabled for these requests (see ProxifyClient).
    """
    import base64

    from app.search.providers.google import ProxifyClient, ProxifyError

    if not _proxify_enabled():
        raise cause
    settings = get_settings()
    metrics = get_metrics()
    client = ProxifyClient(settings.proxify_url, timeout=settings.proxify_fetch_timeout_seconds)
    try:
        data = await client.fetch(url, browser=settings.proxify_fetch_browser or None, raw=True)
    except ProxifyError as exc:
        metrics.extract_total.labels(status="proxify_fallback_failed").inc()
        if exc.status_code == 404:
            raise NotFoundError(f"page not found: {url}") from exc
        raise FetchError(f"{cause}; proxify fallback also failed: {exc}") from exc

    final_url = data.get("final_url") or url
    _validate_egress(final_url)
    status_code = int(data.get("status_code") or 200)
    _raise_for_status(status_code, url)
    raw_body = data.get("body_b64")
    content = base64.b64decode(raw_body) if raw_body else (data.get("html") or "").encode()
    if not content:
        raise FetchError(f"{cause}; proxify fallback returned an empty body")
    limit = max_bytes or settings.extract_max_bytes
    if len(content) > limit:
        raise FetchTooLarge(f"page exceeds max size {limit} bytes: {url}")
    metrics.extract_total.labels(status="proxify_fallback").inc()
    return FetchedPage(
        url=url,
        final_url=final_url,
        status_code=status_code,
        content_type=data.get("content_type") or "text/html",
        content=content,
        headers={},
        via="proxify",
    )


def _raise_for_status(status_code: int, url: str) -> None:
    if status_code < 400:
        return
    if status_code == 404:
        raise NotFoundError(f"page not found: {url}")
    raise FetchError(f"HTTP {status_code} fetching {url}")


async def _get_following_redirects(url: str, *, max_bytes: int | None = None) -> FetchedPage:
    """GET ``url``, following redirects manually and re-validating every hop.

    httpx's ``follow_redirects=True`` validates nothing between hops, so an
    attacker-controlled 302 to 169.254.169.254 would bypass any check applied
    only to the original URL. Here each Location is resolved and re-validated
    before the next request is issued.
    """
    settings = get_settings()
    budget = max(settings.fetch_max_redirects, 0)
    current = url

    for _ in range(budget + 1):
        _validate_egress(current)
        try:
            resp = await get_http_client().get(
                current,
                timeout=settings.extract_timeout_seconds,
                follow_redirects=False,
            )
        except httpx.TimeoutException as e:
            raise FetchError(f"timeout fetching {current}: {e}") from e
        except httpx.HTTPError as e:
            raise FetchError(f"error fetching {current}: {e}") from e

        if resp.is_redirect:
            location = resp.headers.get("location")
            if not location:
                raise FetchError(f"redirect without Location header from {current}")
            current = str(httpx.URL(current).join(location))
            continue

        _raise_for_status(resp.status_code, url)

        limit = max_bytes or settings.extract_max_bytes
        if len(resp.content) > limit:
            raise FetchTooLarge(f"page exceeds max size {limit} bytes: {url}")

        return FetchedPage(
            url=url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            content_type=resp.headers.get("content-type"),
            content=resp.content,
            headers=dict(resp.headers),
        )

    get_metrics().blocked_requests.labels(reason="redirect_chain").inc()
    raise FetchError(f"too many redirects (limit {budget}) starting at {url}")
