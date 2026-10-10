from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("APP_ENV", "test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "gateway"))

from app.cache import Cache  # noqa: E402


class FakeRedis:
    """Minimal async Redis double.

    Models the behaviours the app actually depends on, including the ones the
    previous version got wrong: ``sadd`` returns 1 only for a *new* member (the
    snippet queue's dedupe depends on it), ``incr``/``expire`` can be made to
    fail independently (to test rate-limit key poisoning), and ``scan_iter``
    exists so cache invalidation is testable.
    """

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttl: dict[str, int] = {}
        self._sets: dict[str, set] = {}
        self._lists: dict[str, list] = {}
        self.fail_on: set[str] = set()
        self.calls: list[tuple] = []

    def _maybe_fail(self, op: str) -> None:
        self.calls.append((op,))
        if op in self.fail_on:
            raise ConnectionError(f"redis unavailable during {op}")

    async def ping(self):
        self._maybe_fail("ping")
        return True

    async def get(self, key):
        self._maybe_fail("get")
        return self._store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self._maybe_fail("set")
        if nx and key in self._store:
            return None
        self._store[key] = value
        if ex is not None:
            self._ttl[key] = ex
        return True

    async def delete(self, *keys):
        self._maybe_fail("delete")
        for key in keys:
            self._store.pop(key, None)
            self._ttl.pop(key, None)
        return True

    async def incr(self, key):
        self._maybe_fail("incr")
        v = int(self._store.get(key, 0)) + 1
        self._store[key] = str(v)
        return v

    async def expire(self, key, seconds):
        self._maybe_fail("expire")
        self._ttl[key] = seconds
        return True

    async def ttl(self, key):
        return self._ttl.get(key, -1)

    async def sadd(self, key, member):
        self._maybe_fail("sadd")
        bucket = self._sets.setdefault(key, set())
        if member in bucket:
            return 0
        bucket.add(member)
        return 1

    async def scard(self, key):
        return len(self._sets.get(key, ()))

    async def rpush(self, key, value):
        self._maybe_fail("rpush")
        self._lists.setdefault(key, []).append(value)
        return len(self._lists[key])

    async def lpop(self, key):
        self._maybe_fail("lpop")
        lst = self._lists.get(key, [])
        return lst.pop(0) if lst else None

    async def llen(self, key):
        return len(self._lists.get(key, []))

    async def keys(self, pattern="*"):
        prefix = pattern.rstrip("*")
        return [k for k in self._store if k.startswith(prefix)]

    async def scan_iter(self, match="*"):
        prefix = match.rstrip("*")
        for key in list(self._store):
            if key.startswith(prefix):
                yield key

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _restore_global_settings():
    """Undo any direct assignment to the cached global Settings after each test.

    Fifteen tests assign to it directly (``orch.settings.rerank_enabled = False``
    is the global object, not a copy), so the change leaked into every later
    test. Results depended on order: the DB end-to-end test passed in the full
    run, because an earlier test had switched the reranker off, and failed when
    run alone.
    """
    from app.config import get_settings

    settings = get_settings()
    snapshot = dict(settings.__dict__)
    yield
    for name, value in snapshot.items():
        if settings.__dict__.get(name) is not value:
            object.__setattr__(settings, name, value)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def cache(fake_redis):
    c = Cache.__new__(Cache)
    c._client = fake_redis
    return c


@pytest.fixture
def settings():
    """A Settings instance built from a clean environment.

    Tests must never mutate the lru_cached global: doing so leaked between
    tests and made results order-dependent.
    """
    from app.config import Settings

    return Settings(
        _env_file=None,
        auth_enabled=False,
        app_env="test",
        fetch_block_private_networks=False,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
