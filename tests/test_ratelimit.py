from __future__ import annotations

import uuid

import pytest

from app.errors import RateLimitError
from app.models import ApiKey, ApiKeyRole
from app.security import SecurityService, hash_api_key


@pytest.fixture
def api_key() -> ApiKey:
    return ApiKey(
        id=uuid.uuid4(),
        name="test",
        key_hash=hash_api_key("sk_test"),
        role=ApiKeyRole.user,
        active=True,
    )


async def test_rate_limit_allows_under_budget(cache, api_key):
    svc = SecurityService(cache)
    svc.settings.auth_enabled = True
    svc.settings.rate_limit_per_minute = 5
    for _ in range(5):
        await svc.check_rate_limit(api_key)


async def test_rate_limit_rejects_over_budget(cache, api_key):
    svc = SecurityService(cache)
    svc.settings.auth_enabled = True
    svc.settings.rate_limit_per_minute = 3
    for _ in range(3):
        await svc.check_rate_limit(api_key)
    with pytest.raises(RateLimitError):
        await svc.check_rate_limit(api_key)


async def test_rate_limit_skipped_when_auth_disabled(cache, api_key):
    svc = SecurityService(cache)
    svc.settings.auth_enabled = False
    svc.settings.rate_limit_per_minute = 1
    for _ in range(10):
        await svc.check_rate_limit(api_key)


async def test_hash_is_deterministic():
    assert hash_api_key("sk_abc") == hash_api_key("sk_abc")
    assert hash_api_key("sk_abc") != hash_api_key("sk_abd")
