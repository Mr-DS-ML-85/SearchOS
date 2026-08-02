from __future__ import annotations

import httpx

from app.config import get_settings


class HttpClient:
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
    ) -> httpx.Response:
        return await self._client.get(url, params=params, headers=headers, timeout=timeout)

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        return await self._client.post(url, json=json, headers=headers, timeout=timeout)

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
