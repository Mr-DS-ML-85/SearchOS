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
    from app.extract.render import render_page, should_render
    from app.extract.robots import RobotsChecker

    settings = get_settings()
    # Check before any network access, including the robots fetch.
    _validate_egress(url)

    robots = RobotsChecker()
    if respect_robots if respect_robots is not None else settings.fetch_respect_robots:
        if not await robots.allowed(url):
            get_metrics().extract_total.labels(status="robots_blocked").inc()
            raise ValidationError(f"robots.txt disallows fetching {url}")

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
