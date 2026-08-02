from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import Cache
from app.config import get_settings
from app.errors import AuthError, ForbiddenError, RateLimitError
from app.metrics import get_metrics
from app.models import ApiKey, ApiKeyRole


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> str:
    return "sk_" + secrets.token_urlsafe(32)


def sign_nonce(key: str, nonce: str) -> str:
    return hmac.new(hash_api_key(key).encode(), nonce.encode(), hashlib.sha256).hexdigest()


class SecurityService:
    def __init__(self, cache: Cache) -> None:
        self.cache = cache
        self.settings = get_settings()

    async def bootstrap_admin(self, session: AsyncSession) -> str | None:
        key = self.settings.bootstrap_admin_api_key
        if not key or key.startswith("change-me"):
            return None
        existing = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key)))
        if existing:
            return key
        session.add(
            ApiKey(
                name="bootstrap-admin",
                key_hash=hash_api_key(key),
                role=ApiKeyRole.admin,
                active=True,
            )
        )
        await session.commit()
        return key

    async def authenticate(self, session: AsyncSession, token: str | None) -> ApiKey:
        if not self.settings.auth_enabled:
            return ApiKey(name="anonymous", key_hash="anonymous", role=ApiKeyRole.user, active=True)
        if not token:
            raise AuthError("Missing API key. Provide via Authorization: Bearer <key> or X-API-Key header.")
        api_key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
        if api_key is None or not api_key.active:
            raise AuthError("Invalid or inactive API key.")
        return api_key

    async def require_role(self, api_key: ApiKey, role: ApiKeyRole) -> None:
        if api_key.role != role and api_key.role != ApiKeyRole.admin:
            raise ForbiddenError(f"Requires {role.value} role.")

    async def check_rate_limit(self, api_key: ApiKey) -> None:
        if not self.settings.auth_enabled:
            return
        key_id = str(api_key.id)
        now = time.time()
        window = int(now // 60) * 60
        min_key = f"rl:{key_id}:m:{window}"
        hour_key = f"rl:{key_id}:h:{int(now // 3600)}"
        day_key = f"rl:{key_id}:d:{int(now // 86400)}"

        minute = await self.cache.incr(min_key, 120)
        if minute > self.settings.rate_limit_per_minute:
            self._inc_rate_limited("minute")
            raise RateLimitError("Per-minute rate limit exceeded.", retry_after_seconds=60 - int(now % 60))

        hour = await self.cache.incr(hour_key, 7200)
        if hour > self.settings.rate_limit_per_hour:
            self._inc_rate_limited("hour")
            raise RateLimitError("Per-hour rate limit exceeded.", retry_after_seconds=3600 - int(now % 3600))

        day = await self.cache.incr(day_key, 172800)
        if day > self.settings.rate_limit_per_day:
            self._inc_rate_limited("day")
            raise RateLimitError("Per-day rate limit exceeded.", retry_after_seconds=86400 - int(now % 86400))

    def _inc_rate_limited(self, window: str) -> None:
        try:
            get_metrics().rate_limited.labels(window=window).inc()
        except Exception:
            pass

    async def log_usage(
        self, api_key: ApiKey, endpoint: str, status_code: int, duration_ms: int
    ) -> None:
        from app.db import get_session_factory
        from app.models import UsageLog

        if not self.settings.auth_enabled:
            return
        try:
            async with get_session_factory()() as session:
                session.add(
                    UsageLog(
                        api_key_id=api_key.id,
                        endpoint=endpoint,
                        status_code=status_code,
                        duration_ms=duration_ms,
                    )
                )
                await session.execute(
                    update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=func.now())
                )
                await session.commit()
        except Exception:
            pass


_security: SecurityService | None = None


def get_security_service(cache: Cache | None = None) -> SecurityService:
    """Return the process-wide SecurityService.

    Rate-limit counters live in their own Redis database (``redis_ratelimit_db``) so they
    never evict or race with the response caches.
    """
    global _security
    if _security is None:
        if cache is None:
            from app.cache import Cache

            cache = Cache(url=get_settings().ratelimit_redis_url)
        _security = SecurityService(cache)
    return _security
