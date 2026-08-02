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
    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttl: dict[str, int] = {}
        self._sets: dict[str, set] = {}
        self._lists: dict[str, list] = {}

    async def ping(self):
        return True

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        if ex is not None:
            self._ttl[key] = ex
        return True

    async def delete(self, key):
        self._store.pop(key, None)
        return True

    async def incr(self, key):
        v = int(self._store.get(key, 0)) + 1
        self._store[key] = str(v)
        return v

    async def expire(self, key, seconds):
        return True

    async def sadd(self, key, member):
        self._sets.setdefault(key, set()).add(member)
        return 1

    async def rpush(self, key, value):
        self._lists.setdefault(key, []).append(value)
        return 1

    async def lpop(self, key):
        lst = self._lists.get(key, [])
        return lst.pop(0) if lst else None

    async def llen(self, key):
        return len(self._lists.get(key, []))

    async def aclose(self):
        pass


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def cache(fake_redis):
    c = Cache.__new__(Cache)
    c._client = fake_redis
    return c


@pytest.fixture
def anyio_backend():
    return "asyncio"
