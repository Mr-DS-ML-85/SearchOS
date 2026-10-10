from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class HttpClient:
    """Thin wrapper over a process-wide ``httpx.AsyncClient``.

    Redirects are followed by default, which is unsafe for user-supplied URLs:
    the redirect chain is only validated when ``follow_redirects=False`` is
    passed per call (see ``extract.fetcher._get_following_redirects``).
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.extract_timeout_seconds, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": settings.extract_user_agent},
        )

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        follow_redirects: bool | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if follow_redirects is not None:
            kwargs["follow_redirects"] = follow_redirects
        return await self._client.get(url, **kwargs)

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        follow_redirects: bool | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {"json": json, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if follow_redirects is not None:
            kwargs["follow_redirects"] = follow_redirects
        return await self._client.post(url, **kwargs)

    async def close(self) -> None:
        await self._client.aclose()


_client: HttpClient | None = None


def get_http_client() -> HttpClient:
    global _client
    if _client is None:
        _client = HttpClient()
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
