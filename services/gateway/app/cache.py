from __future__ import annotations

import json
import time
from typing import Any

import redis.asyncio as redis

from app.config import get_settings


class Cache:
    def __init__(self, url: str | None = None) -> None:
        settings = get_settings()
        self._client = redis.from_url(
            url or settings.cache_redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(key)
        except Exception:
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int = 300) -> bool:
        try:
            return bool(await self._client.set(key, json.dumps(value), ex=ttl_seconds))
        except Exception:
            return False

    async def delete(self, key: str) -> bool:
        try:
            return bool(await self._client.delete(key))
        except Exception:
            return False

    async def incr(self, key: str, ttl_seconds: int = 60) -> int:
        try:
            value = await self._client.incr(key)
            if value == 1:
                await self._client.expire(key, ttl_seconds)
            return int(value)
        except Exception:
            return 0

    async def add_to_set(self, key: str, member: str) -> bool:
        try:
            return bool(await self._client.sadd(key, member))
        except Exception:
            return False

    async def rpush(self, key: str, value: str) -> int:
        try:
            return int(await self._client.rpush(key, value))
        except Exception:
            return 0

    async def lpop(self, key: str) -> str | None:
        try:
            return await self._client.lpop(key)
        except Exception:
            return None

    async def llen(self, key: str) -> int:
        try:
            return int(await self._client.llen(key))
        except Exception:
            return 0

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None


def cache_key(prefix: str, *parts: Any) -> str:
    key = ":".join(str(p) for p in parts)
    if len(key) > 400:
        import hashlib

        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        key = key[:340] + ":" + digest
    return f"{prefix}:{key}"
