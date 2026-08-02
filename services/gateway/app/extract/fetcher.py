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


async def fetch_page(url: str, *, respect_robots: bool | None = None, max_bytes: int | None = None) -> FetchedPage:
    from app.extract.render import render_page, should_render
    from app.extract.robots import RobotsChecker

    settings = get_settings()
    robots = RobotsChecker()
    if (respect_robots if respect_robots is not None else settings.fetch_respect_robots):
        if not await robots.allowed(url):
            get_metrics().extract_total.labels(status="robots_blocked").inc()
            raise ValidationError(f"robots.txt disallows fetching {url}")

    # Headless render first: gives the real DOM for SPAs and heavy-JS pages so
    # trafilatura can extract clean, LLM-ready content. Falls back to a plain GET.
    if settings.renderer_url and should_render(url):
        try:
            page = await render_page(url, max_bytes=max_bytes)
            if page is not None:
                return page
        except (FetchError, httpx.HTTPError):
            get_metrics().extract_total.labels(status="renderer_fallback").inc()

    client = get_http_client()
    try:
        resp = await client.get(url, timeout=settings.extract_timeout_seconds)
    except httpx.TimeoutException as e:
        raise FetchError(f"timeout fetching {url}: {e}") from e
    except httpx.HTTPError as e:
        raise FetchError(f"error fetching {url}: {e}") from e

    if resp.status_code >= 400:
        if resp.status_code == 404:
            raise NotFoundError(f"page not found: {url}")
        raise FetchError(f"HTTP {resp.status_code} fetching {url}")

    limit = max_bytes or settings.extract_max_bytes
    if len(resp.content) > limit:
        raise FetchTooLarge(f"page exceeds max size {limit} bytes: {url}")

    content_type = resp.headers.get("content-type")
    return FetchedPage(
        url=url,
        final_url=str(resp.url),
        status_code=resp.status_code,
        content_type=content_type,
        content=resp.content,
        headers=dict(resp.headers),
    )
