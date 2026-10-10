from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
        key_hash = hash_api_key(key)
        # INSERT ... ON CONFLICT DO NOTHING: with multiple uvicorn workers
        # starting concurrently, the previous SELECT-then-INSERT made the second
        # worker raise IntegrityError inside the lifespan and crash-loop.
        stmt = (
            pg_insert(ApiKey.__table__)
            .values(
                id=uuid.uuid4(),
                name="bootstrap-admin",
                key_hash=key_hash,
                role=ApiKeyRole.admin.value,
                active=True,
            )
            .on_conflict_do_nothing(index_elements=["key_hash"])
        )
        await session.execute(stmt)
        await session.commit()
        return key

    async def authenticate(self, session: AsyncSession, token: str | None) -> ApiKey:
        if not self.settings.auth_enabled:
            # AUTH_ENABLED=false means "no auth", so the anonymous principal must
            # be able to reach admin routes too. Previously it was granted the
            # user role and every /v1/admin/* call returned 403, so turning auth
            # off silently disabled the admin API instead of opening it.
            return ApiKey(
                id=uuid.UUID(int=0),
                name="anonymous",
                key_hash="anonymous",
                role=ApiKeyRole.admin,
                active=True,
            )
        if not token:
            raise AuthError("Missing API key. Provide via Authorization: Bearer <key> or X-API-Key header.")
        api_key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
        if api_key is None or not api_key.active:
            raise AuthError("Invalid or inactive API key.")
        return api_key

    async def require_role(self, api_key: ApiKey, role: ApiKeyRole) -> None:
        if api_key.role != role and api_key.role != ApiKeyRole.admin:
            raise ForbiddenError(f"Requires {role.value} role.")

    async def check_rate_limit(self, api_key: ApiKey | None) -> None:
        if not self.settings.auth_enabled or api_key is None:
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

    async def check_auth_failures(self, client_host: str) -> None:
        """Count and throttle *failed* authentications, keyed on source IP.

        Nothing used to count a failed authenticate, so guessing API keys was
        unthrottled and unlogged. Successful requests must never touch this
        counter — an earlier version checked it before authenticating, which
        locked out legitimate clients after ten requests a minute.
        """
        if not self.settings.auth_enabled:
            return
        now = time.time()
        key = f"rl:authfail:{client_host}:{int(now // 60)}"
        failures = await self.cache.incr(key, 120)
        try:
            get_metrics().auth_failures.labels(reason="invalid_key").inc()
        except Exception:
            pass
        if failures > self.settings.rate_limit_auth_failures_per_minute:
            self._inc_rate_limited("auth_failure")
            raise RateLimitError(
                "Too many failed authentication attempts.",
                retry_after_seconds=60 - int(now % 60),
            )

    def _inc_rate_limited(self, window: str) -> None:
        try:
            get_metrics().rate_limited.labels(window=window).inc()
        except Exception:
            pass

    async def log_usage(self, api_key: ApiKey, endpoint: str, status_code: int, duration_ms: int) -> None:
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
                # Throttle last_used_at: updating the hot api_keys row on every
                # request caused row-lock contention and table bloat across workers.
                await session.execute(
                    update(ApiKey)
                    .where(
                        ApiKey.id == api_key.id,
                        or_(
                            ApiKey.last_used_at.is_(None),
                            ApiKey.last_used_at < func.now() - text("interval '5 minutes'"),
                        ),
                    )
                    .values(last_used_at=func.now())
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
