from __future__ import annotations

import json
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
        """Increment a counter and (re)set its TTL atomically.

        The old version ran ``INCR`` then ``EXPIRE`` only when the result was 1.
        A crash between the two left the key with no TTL, and because the value
        was then greater than 1 the TTL was never retried — permanently locking
        that API key out of every rate-limit window until someone deleted the
        key by hand. A Lua script makes both operations indivisible.
        """
        script = "local v = redis.call('INCR', KEYS[1]) redis.call('EXPIRE', KEYS[1], ARGV[1]) return v"
        try:
            value = await self._client.eval(script, 1, key, str(ttl_seconds))
            return int(value)
        except Exception:
            # Fall back for clients without EVAL support (e.g. the test double).
            try:
                value = await self._client.incr(key)
                await self._client.expire(key, ttl_seconds)
                return int(value)
            except Exception:
                return 0

    async def acquire_lock(self, key: str, ttl_seconds: int = 10) -> bool:
        """Best-effort single-flight lock to stop a cache stampede."""
        try:
            return bool(await self._client.set(key, "1", ex=ttl_seconds, nx=True))
        except Exception:
            return True  # fail open: better to duplicate work than to block

    async def release_lock(self, key: str) -> bool:
        try:
            return bool(await self._client.delete(key))
        except Exception:
            return False

    async def invalidate(self, pattern: str) -> int:
        """Delete every key matching ``pattern`` (used by admin cache flush).

        Uses SCAN so it never blocks the Redis event loop on a large keyspace.
        """
        removed = 0
        try:
            batch: list[str] = []
            async for key in self._client.scan_iter(match=pattern, count=500):
                batch.append(key)
                if len(batch) >= 500:
                    removed += int(await self._client.delete(*batch))
                    batch = []
            if batch:
                removed += int(await self._client.delete(*batch))
        except Exception:
            return removed
        return removed

    async def record_query(self, query: str, *, results: int = 0) -> None:
        """Count a search for the popular-queries view.

        Kept in Redis rather than Postgres so the hot path never writes, and so
        a burst of searches cannot lock rows.
        """
        q = " ".join((query or "").split())[:120].lower()
        if not q:
            return
        try:
            await self._client.zincrby(_QUERY_STATS_KEY, 1, q)
            if results == 0:
                await self._client.zincrby(_QUERY_EMPTY_KEY, 1, q)
            await self._client.expire(_QUERY_STATS_KEY, settings_ttl())
            # The empty-result counter had no expiry at all, so it grew forever.
            await self._client.expire(_QUERY_EMPTY_KEY, settings_ttl())
        except Exception:
            pass

    async def top_queries(self, limit: int = 10) -> list[tuple[str, int]]:
        try:
            rows = await self._client.zrevrange(_QUERY_STATS_KEY, 0, max(0, limit - 1), withscores=True)
        except Exception:
            return []
        return [(str(name), int(score)) for name, score in rows]

    async def empty_query_count(self, query: str) -> int:
        q = " ".join((query or "").split())[:120].lower()
        try:
            score = await self._client.zscore(_QUERY_EMPTY_KEY, q)
        except Exception:
            return 0
        return int(score or 0)

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

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        try:
            return list(await self._client.lrange(key, start, stop))
        except Exception:
            return []

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        try:
            return bool(await self._client.ltrim(key, start, stop))
        except Exception:
            return False

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        try:
            return bool(await self._client.expire(key, ttl_seconds))
        except Exception:
            return False

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


_QUERY_STATS_KEY = "searchos:query_stats"
_QUERY_EMPTY_KEY = "searchos:query_stats:empty"


def settings_ttl() -> int:
    from app.config import get_settings

    return max(get_settings().search_cache_ttl_seconds * 24, 3600)


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
    """Build a cache key that cannot collide across different parameter sets.

    The previous implementation joined parts with ``":"``, so
    ``(language="b", country="c")`` and ``(language="b:c", country="all")``
    produced the same key and one tenant could be served another tenant's
    response. Parts are now JSON-encoded and hashed when the key gets long.
    """
    encoded = json.dumps(parts, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(encoded) > 400:
        import hashlib

        digest = hashlib.sha256(encoded.encode()).hexdigest()[:32]
        encoded = encoded[:340] + ":" + digest
    return f"{prefix}:{encoded}"
