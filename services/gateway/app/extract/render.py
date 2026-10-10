from __future__ import annotations

import httpx

from app.config import get_settings
from app.extract.fetcher import FetchedPage, FetchError
from app.http import get_http_client

_NON_RENDERABLE_SUFFIXES = (
    ".pdf",
    ".txt",
    ".json",
    ".xml",
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".zip",
    ".gz",
)


def should_render(url: str) -> bool:
    """Heuristic: only route HTML documents through the headless renderer."""
    from urllib.parse import urlsplit

    path = urlsplit(url).path.lower().rstrip("/")
    if path.endswith(_NON_RENDERABLE_SUFFIXES):
        return False
    return True


async def render_page(url: str, *, max_bytes: int | None = None) -> FetchedPage | None:
    """Render a URL with the headless-browser renderer service.

    Returns ``None`` when no renderer is configured; raises :class:`FetchError` when the
    renderer is configured but fails so the caller can fall back to a direct fetch.
    """
    settings = get_settings()
    if not settings.renderer_url:
        return None

    client = get_http_client()
    params = {"url": url, "max_bytes": max_bytes or settings.render_max_bytes}
    headers = {}
    if settings.renderer_token:
        headers["X-Renderer-Token"] = settings.renderer_token
    try:
        resp = await client.get(
            f"{settings.renderer_url}/render",
            params=params,
            headers=headers,
            timeout=settings.render_timeout_seconds,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # 403 from the renderer means it refused the URL (SSRF policy). That is
        # a decision, not a transient failure — do not silently fall back to a
        # direct fetch that would bypass the check.
        if e.response.status_code in (400, 403):
            from app.errors import ValidationError

            raise ValidationError(f"renderer refused {url}: {e.response.status_code} {e.response.text[:200]}") from e
        raise FetchError(f"renderer error for {url}: {e.__class__.__name__}: {e}") from e
    except httpx.HTTPError as e:
        raise FetchError(f"renderer error for {url}: {e.__class__.__name__}: {e}") from e

    # `resp.status_code` is the *renderer API's* status, which is 200 even when
    # the page it rendered is a 404. The renderer reports the rendered page's
    # status separately in X-Page-Status; fall back to the API status only if an
    # older renderer is in use.
    page_status = resp.status_code
    reported = resp.headers.get("x-page-status")
    if reported:
        try:
            page_status = int(reported)
        except ValueError:
            page_status = resp.status_code

    return FetchedPage(
        url=url,
        final_url=resp.headers.get("x-final-url") or url,
        status_code=page_status,
        content_type=resp.headers.get("content-type") or "text/html",
        content=resp.content,
        headers=dict(resp.headers),
    )
