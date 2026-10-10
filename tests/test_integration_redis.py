"""Cache behaviour tests against a real Redis.

Skipped unless TEST_REDIS_URL is set:

    docker run -d --name searchos-redis -p 16380:6379 redis:7-alpine \\
      --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    export TEST_REDIS_URL=redis://127.0.0.1:16380/0
    pytest tests/test_integration_redis.py -q
"""

from __future__ import annotations

import asyncio
import os

import pytest

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(not TEST_REDIS_URL, reason="TEST_REDIS_URL not set; skipping Redis integration tests")


@pytest.fixture
async def redis_cache():
    from app.cache import Cache

    cache = Cache(url=TEST_REDIS_URL)
    try:
        yield cache
    finally:
        await cache._client.flushdb()
        await cache.close()


class TestRateLimitAtomicity:
    async def test_ttl_is_set_on_every_increment(self, redis_cache):
        """Regression: INCR-then-EXPIRE-only-on-1 left keys with no TTL.

        A crash between the two left the key permanent, and because the value
        was then >1 the TTL was never retried — that API key was locked out of
        every window until someone deleted it by hand.
        """
        await redis_cache.incr("rl:test:m", 120)
        for _ in range(2, 8):
            await redis_cache.incr("rl:test:m", 120)
            assert await redis_cache._client.ttl("rl:test:m") > 0, "TTL missing after increment"

    async def test_ttl_is_refreshed_not_lost(self, redis_cache):
        await redis_cache.incr("rl:test:m", 5)
        await asyncio.sleep(1.2)
        await redis_cache.incr("rl:test:m", 5)
        assert await redis_cache._client.ttl("rl:test:m") > 0

    async def test_value_increments_correctly(self, redis_cache):
        for i in range(1, 6):
            assert await redis_cache.incr("rl:count", 60) == i


class TestCacheKeyUniqueness:
    def test_different_param_sets_do_not_collide(self):
        """Regression: ":"-joining let (language='b', country='c') equal
        (language='b:c', country='all'), serving one tenant another's results."""
        from app.cache import cache_key

        a = cache_key("search", "q", "all", 10, "b", "c", "all")
        b = cache_key("search", "q", "all", 10, "b:c", "all", "all")
        assert a != b

    def test_identical_params_produce_identical_keys(self):
        from app.cache import cache_key

        assert cache_key("search", "q", "all", 10) == cache_key("search", "q", "all", 10)

    def test_long_keys_are_bounded(self):
        from app.cache import cache_key

        key = cache_key("search", "x" * 1000)
        assert len(key) <= 400

    def test_long_keys_stay_unique_by_content(self):
        from app.cache import cache_key

        a = cache_key("search", "a" * 1000)
        b = cache_key("search", "b" * 1000)
        assert a != b

    def test_sequence_parts_serialise_distinctly(self):
        """A list and a string must not produce the same key."""
        from app.cache import cache_key

        assert cache_key("search", ["a", "b"]) != cache_key("search", "a,b")


class TestInvalidation:
    async def test_invalidate_removes_matching_keys(self, redis_cache):
        await redis_cache.set("search:one", {"a": 1}, ttl_seconds=60)
        await redis_cache.set("search:two", {"a": 2}, ttl_seconds=60)
        await redis_cache.set("extract:three", {"a": 3}, ttl_seconds=60)

        removed = await redis_cache.invalidate("search:*")
        assert removed == 2
        assert await redis_cache.get("search:one") is None
        assert await redis_cache.get("search:two") is None
        assert await redis_cache.get("extract:three") == {"a": 3}

    async def test_invalidate_is_safe_on_empty_namespace(self, redis_cache):
        assert await redis_cache.invalidate("nothing:*") == 0

    async def test_invalidate_handles_many_keys(self, redis_cache):
        for i in range(600):
            await redis_cache.set(f"search:k{i}", {"i": i}, ttl_seconds=60)
        removed = await redis_cache.invalidate("search:*")
        assert removed == 600


class TestLocking:
    async def test_lock_is_exclusive(self, redis_cache):
        assert await redis_cache.acquire_lock("lock:x", 10) is True
        assert await redis_cache.acquire_lock("lock:x", 10) is False
        await redis_cache.release_lock("lock:x")
        assert await redis_cache.acquire_lock("lock:x", 10) is True

    async def test_lock_expires(self, redis_cache):
        assert await redis_cache.acquire_lock("lock:y", 1) is True
        assert await redis_cache.acquire_lock("lock:y", 1) is False
        await asyncio.sleep(1.3)
        assert await redis_cache.acquire_lock("lock:y", 1) is True


class TestSnippetQueue:
    async def test_seen_set_dedupes(self, redis_cache):
        """FakeRedis.sadd used to always return 1, hiding this."""
        assert await redis_cache.add_to_set("set:s", "https://a.com") is True
        assert await redis_cache.add_to_set("set:s", "https://a.com") is False
        assert await redis_cache._client.scard("set:s") == 1


class TestRedisFailureMode:
    @pytest.fixture
    async def dead_cache(self):
        """A Cache pointed at a closed port: every call must fail open."""
        from app.cache import Cache

        cache = Cache(url=TEST_REDIS_URL)
        await cache._client.aclose()
        # Replace the pool so reconnects fail fast instead of re-dialling.
        cache._client.connection_pool = _DeadPool()
        return cache

    async def test_reads_fail_open(self, dead_cache):
        """Redis down must degrade to a cache miss, not raise."""
        assert await dead_cache.get("anything") is None
        assert await dead_cache.set("anything", {"a": 1}) is False
        assert await dead_cache.ping() is False
        assert await dead_cache.incr("anything") == 0
        assert await dead_cache.add_to_set("s", "m") is False
        assert await dead_cache.rpush("l", "m") == 0
        assert await dead_cache.lpop("l") is None
        assert await dead_cache.llen("l") == 0

    async def test_invalidation_and_locks_fail_open(self, dead_cache):
        assert await dead_cache.invalidate("search:*") == 0
        # acquire_lock fails open on purpose: duplicating work beats blocking.
        assert await dead_cache.acquire_lock("lock:dead", 5) is True
        assert await dead_cache.release_lock("lock:dead") is False

    async def test_close_is_safe_when_already_closed(self, dead_cache):
        await dead_cache.close()  # must not raise


class _DeadPool:
    """Minimal connection pool stand-in that always refuses to connect."""

    def __init__(self, *args, **kwargs):
        self.connection_kwargs = {"host": "127.0.0.1", "port": 1, "db": 0}

    def get_connection(self, *args, **kwargs):
        import redis

        raise redis.ConnectionError("connection pool is closed")

    def release(self, connection):  # pragma: no cover - never reached
        return None

    def disconnect(self):  # pragma: no cover
        return None
