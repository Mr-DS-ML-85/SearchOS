"""Security tests: SSRF egress policy, error envelope, CORS, auth throttling."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from app.config import Settings, is_blocked_ip
from app.models import ApiKey, ApiKeyRole
from app.netguard import BlockedURLError, validate_url


class TestIsBlockedIP:
    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.5.4",
            "192.168.1.1",
            "169.254.169.254",  # AWS/GCP metadata
            "100.64.0.1",  # CGNAT
            "0.0.0.0",
            "224.0.0.1",
            "255.255.255.255",
            "::1",
            "fe80::1",  # link-local v6
            "fc00::1",  # unique-local v6
            "::ffff:127.0.0.1",  # v4-mapped loopback
        ],
    )
    def test_non_public_addresses_are_blocked(self, addr):
        assert is_blocked_ip(addr) is True

    @pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
    def test_public_addresses_are_allowed(self, addr):
        assert is_blocked_ip(addr) is False

    def test_garbage_is_treated_as_blocked(self):
        assert is_blocked_ip("not-an-ip") is True


class TestValidateURL:
    def test_rejects_non_http_schemes(self, settings: Settings):
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com",
            "javascript:alert(1)",
            "data:text/html,<script>1</script>",
            "//example.com/no-scheme",
        ):
            with pytest.raises(BlockedURLError, match="scheme not allowed"):
                validate_url(url, settings=settings)

    def test_rejects_internal_targets(self, settings: Settings):
        settings.fetch_block_private_networks = True
        for url in (
            "http://127.0.0.1:8000/admin",
            "http://localhost:6379/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
            "http://[::1]:8080/",
        ):
            with pytest.raises(BlockedURLError):
                validate_url(url, settings=settings)

    def test_rejects_missing_host(self, settings: Settings):
        with pytest.raises(BlockedURLError, match="no host"):
            validate_url("http:///path", settings=settings)

    def test_allows_public_url(self, settings: Settings):
        settings.fetch_block_private_networks = False
        result = validate_url("https://example.com/page", settings=settings)
        assert result.host == "example.com"
        assert result.port == 443
        assert result.scheme == "https"

    def test_explicit_allowlist_permits_internal_host(self, settings: Settings):
        settings.fetch_block_private_networks = True
        settings.fetch_allow_private_networks = ["renderer"]
        result = validate_url("http://renderer:8010/healthz", settings=settings)
        assert result.host == "renderer"
        assert result.port == 8010

    def test_allowlist_matches_subdomains(self, settings: Settings):
        settings.fetch_block_private_networks = True
        settings.fetch_allow_private_networks = ["example.internal"]
        validate_url("http://api.example.internal/x", settings=settings)

    def test_allowlist_does_not_permit_suffix_confusion(self, settings: Settings):
        settings.fetch_block_private_networks = True
        settings.fetch_allow_private_networks = ["example.internal"]
        with pytest.raises(BlockedURLError):
            validate_url("http://evilexample.internal/x", settings=settings)

    def test_unresolvable_host_is_blocked(self, settings: Settings):
        settings.fetch_block_private_networks = True
        with pytest.raises(BlockedURLError, match="cannot resolve"):
            validate_url("http://this-host-does-not-exist-searchos-test.invalid/", settings=settings)


class TestSSRFViaExtractEndpoint:
    """The extract endpoint must never proxy into the container network."""

    def test_extract_refuses_metadata_endpoint(self):
        from app.errors import ValidationError
        from app.extract.fetcher import _validate_egress

        with pytest.raises(ValidationError, match="egress policy"):
            _validate_egress("http://169.254.169.254/latest/meta-data/iam/security-credentials/")

    def test_extract_refuses_internal_service_names(self):
        """A container-internal name is refused either way.

        Off the compose network the name does not resolve (a DNS failure, which
        is deliberately *not* reported as an egress violation); on it the name
        resolves to a private address and is refused by policy. What matters is
        that neither path results in a fetch.
        """
        from app.errors import AppError
        from app.extract.fetcher import _validate_egress

        with pytest.raises(AppError):
            _validate_egress("http://redis:6379/")

    def test_extract_increments_blocked_metric(self):
        from app.errors import ValidationError
        from app.extract import fetcher

        before = fetcher.get_metrics().blocked_requests.labels(reason="ssrf")._value.get()
        with pytest.raises(ValidationError):
            fetcher._validate_egress("http://127.0.0.1:5432/")
        after = fetcher.get_metrics().blocked_requests.labels(reason="ssrf")._value.get()
        assert after == before + 1


class TestErrorEnvelope:
    def test_all_error_types_share_one_shape(self):
        from app.errors import (
            AppError,
            AuthError,
            ForbiddenError,
            NotFoundError,
            ProviderError,
            RateLimitError,
            ValidationError,
            error_body,
        )

        for exc in (
            AppError("x"),
            NotFoundError("x"),
            ValidationError("x"),
            AuthError("x"),
            ForbiddenError("x"),
            RateLimitError("x", retry_after_seconds=30),
            ProviderError("x"),
        ):
            body = error_body(exc)
            assert set(body) == {"code", "message", "details"}

    def test_rate_limit_carries_retry_after(self):
        from app.errors import RateLimitError, error_headers

        headers = error_headers(RateLimitError("slow down", retry_after_seconds=42))
        assert headers["Retry-After"] == "42"

    def test_auth_carries_www_authenticate(self):
        from app.errors import AuthError, error_headers

        assert "WWW-Authenticate" in (error_headers(AuthError("nope")) or {})

    def test_validation_error_status_is_422_literal(self):
        """Must not depend on Starlette >= 0.47 constants."""
        from app.errors import ValidationError

        assert ValidationError.http_status == 422


class TestCorsPosture:
    def test_wildcard_origin_disables_credentials(self):
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, cors_origins=["*"])
        assert s.cors_allows_credentials is False

    def test_explicit_origins_allow_credentials(self):
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, cors_origins=["https://app.example.com"])
        assert s.cors_allows_credentials is True


class TestSettingsValidation:
    def test_cors_origins_accepts_bare_star(self):
        """CORS_ORIGINS=* used to crash the process at import time."""
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, cors_origins="*")
        assert s.cors_origins == ["*"]

    def test_cors_origins_accepts_comma_list(self):
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, cors_origins="https://a.com, https://b.com")
        assert s.cors_origins == ["https://a.com", "https://b.com"]

    def test_cors_origins_accepts_json(self):
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, cors_origins='["https://a.com"]')
        assert s.cors_origins == ["https://a.com"]

    def test_cors_origins_empty_string_becomes_empty_list(self):
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, cors_origins="")
        assert s.cors_origins == []

    def test_default_cors_origins_is_empty_not_wildcard(self):
        from app.config import Settings

        assert Settings(_env_file=None, auth_enabled=False).cors_origins == []

    def test_invalid_log_level_is_rejected(self):
        from app.config import Settings
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            Settings(_env_file=None, auth_enabled=False, log_level="VERBOSE")

    def test_placeholder_bootstrap_key_rejected(self):
        from app.config import Settings
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError, match="BOOTSTRAP_ADMIN_API_KEY"):
            Settings(_env_file=None, auth_enabled=True, bootstrap_admin_api_key="change-me-to-a-long-random-secret")

    def test_short_bootstrap_key_rejected(self):
        from app.config import Settings
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError, match="at least 16"):
            Settings(_env_file=None, auth_enabled=True, bootstrap_admin_api_key="short")

    def test_placeholder_key_allowed_when_auth_disabled(self):
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, bootstrap_admin_api_key="change-me")
        assert s.auth_enabled is False

    def test_invalid_follow_domains_rejected(self):
        from app.config import Settings
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            Settings(_env_file=None, auth_enabled=False, crawler_follow_domains="subdomains")

    def test_db_url_percent_encodes_password(self):
        """A password with ':' or '@' used to produce a broken DSN."""
        from app.config import Settings

        s = Settings(
            _env_file=None,
            auth_enabled=False,
            database_url=None,
            postgres_user="user",
            postgres_password="p@ss:w/rd#1",
            postgres_host="db",
            postgres_port=5432,
            postgres_db="searchos",
        )
        assert "p%40ss%3Aw%2Frd%231" in s.db_url

    def test_database_url_takes_precedence(self):
        from app.config import Settings

        s = Settings(
            _env_file=None,
            auth_enabled=False,
            database_url="postgresql+asyncpg://x/y",
            postgres_password="ignored",
        )
        assert s.db_url == "postgresql+asyncpg://x/y"

    def test_cache_and_ratelimit_use_separate_redis_dbs(self):
        from app.config import Settings

        s = Settings(_env_file=None, auth_enabled=False, redis_url="redis://redis:6379/0")
        assert s.cache_redis_url.endswith("/1")
        assert s.ratelimit_redis_url.endswith("/2")
        assert s.cache_redis_url != s.ratelimit_redis_url


class TestAuthFailureThrottling:
    async def test_auth_failures_are_throttled(self, cache, settings):
        from app.errors import RateLimitError
        from app.security import SecurityService

        settings.auth_enabled = True
        settings.rate_limit_auth_failures_per_minute = 3
        svc = SecurityService(cache)
        svc.settings = settings

        for _ in range(3):
            await svc.check_auth_failures("10.0.0.9")

        with pytest.raises(RateLimitError, match="authentication"):
            await svc.check_auth_failures("10.0.0.9")

    async def test_successful_requests_never_count_as_failures(self, cache, settings):
        """Regression: checking this limiter before authenticate locked out
        legitimate clients after ten requests a minute."""
        from app.security import SecurityService

        settings.auth_enabled = True
        settings.rate_limit_auth_failures_per_minute = 3
        svc = SecurityService(cache)
        svc.settings = settings

        # A valid key going through the normal path must not touch the counter.
        key = ApiKey(name="u", key_hash="h", role=ApiKeyRole.user, active=True)
        key.id = uuid.uuid4()
        settings.rate_limit_per_minute = 1000
        settings.rate_limit_per_hour = 10000
        settings.rate_limit_per_day = 100000
        for _ in range(50):
            await svc.check_rate_limit(key)
            # No failure is recorded, so this must never raise.
            await svc.check_rate_limit(key)

    async def test_throttle_is_per_source_ip(self, cache, settings):
        from app.security import SecurityService

        settings.auth_enabled = True
        settings.rate_limit_auth_failures_per_minute = 2
        svc = SecurityService(cache)
        svc.settings = settings

        await svc.check_auth_failures("10.0.0.1")
        await svc.check_auth_failures("10.0.0.1")
        # A different attacker has their own budget.
        await svc.check_auth_failures("10.0.0.2")

    async def test_no_throttling_when_auth_disabled(self, cache, settings):
        from app.security import SecurityService

        settings.auth_enabled = False
        svc = SecurityService(cache)
        svc.settings = settings

        for _ in range(50):
            await svc.check_auth_failures("10.0.0.3")


class TestEnvExampleIsUsable:
    """The shipped .env.example must boot the app.

    Regression: CORS_ORIGINS=* made pydantic-settings JSON-decode fail at import
    time, so copying .env.example to .env produced a container restart loop for
    anyone following the README.
    """

    def test_env_example_parses(self, tmp_path):
        from app.config import Settings

        source = Path(__file__).resolve().parents[1] / ".env.example"
        target = tmp_path / ".env"
        target.write_text(source.read_text())

        settings = Settings(_env_file=target, auth_enabled=False)
        assert settings.cors_origins == []
        assert settings.fetch_block_private_networks is True
        assert settings.max_request_body_bytes > 0

    def test_env_example_has_no_dead_variables(self):
        """Every advertised variable must map to a real Settings field.

        Regression: CACHE_HIT_LIMIT and RATE_LIMIT_BURST were documented in
        .env.example but had no field, so `extra="ignore"` discarded them and
        operators tuned a knob that did nothing.
        """
        from app.config import Settings

        source = Path(__file__).resolve().parents[1] / ".env.example"
        fields = {name.lower() for name in Settings.model_fields}
        ignored: list[str] = []
        for line in source.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name = line.split("=", 1)[0].strip()
            if name.isupper() and name.lower() not in fields:
                ignored.append(name)
        assert not ignored, f".env.example advertises vars with no Settings field: {ignored}"

    def test_shipped_settings_have_no_unused_fields(self):
        """Flag Settings fields that nothing in the app reads."""
        import re

        from app.config import Settings

        gw_root = Path(__file__).resolve().parents[1] / "services" / "gateway"
        gateway = gw_root / "app"
        sources = "\n".join(p.read_text() for p in gateway.rglob("*.py"))
        # Entrypoints outside app/ (worker_entrypoint.py, alembic/env.py) too.
        sources += "\n" + (gw_root / "worker_entrypoint.py").read_text()
        sources += "\n" + (gw_root / "alembic" / "env.py").read_text()
        config_src = (gateway / "config.py").read_text()
        unused: list[str] = []
        for name in Settings.model_fields:
            # Consumed by a @property or validator inside config.py itself.
            if re.search(rf"\bself\.{name}\b", config_src):
                continue
            # Consumed as settings.x, get_settings().x, cfg.x or self.settings.x.
            if re.search(rf"\b(settings|cfg|self\.settings)\s*\.\s*{name}\b", sources) or re.search(
                rf"get_settings\(\)\s*\.\s*{name}\b", sources
            ):
                continue
            unused.append(name)
        assert not unused, f"Settings fields that nothing reads: {unused}"
