"""Proxify as the fallback when the native fetcher fails."""

from __future__ import annotations

import base64

import pytest
from app.errors import NotFoundError, ValidationError
from app.extract import fetcher
from app.extract.fetcher import FetchedPage, FetchError, FetchTooLarge, looks_like_challenge
from app.search.providers.google import ProxifyError


def page(content: bytes = b"<html>ok</html>", status: int = 200, ctype: str = "text/html") -> FetchedPage:
    return FetchedPage(
        url="https://e.com/a",
        final_url="https://e.com/a",
        status_code=status,
        content_type=ctype,
        content=content,
        headers={},
    )


@pytest.fixture
def env(monkeypatch):
    """Native fetch and Proxify both stubbed; egress and robots allow everything."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "proxify_url", "http://proxify:8081")
    monkeypatch.setattr(settings, "proxify_fetch_fallback", True)
    monkeypatch.setattr(settings, "fetch_respect_robots", False)
    monkeypatch.setattr(fetcher, "_validate_egress", lambda url: None)

    state = {"native": page(), "proxify": None, "proxify_calls": []}

    async def native(url, *, max_bytes=None):
        if isinstance(state["native"], Exception):
            raise state["native"]
        return state["native"]

    async def proxify_fetch(self, url, *, browser=None, timeout=None, wait_selector=None, raw=False):
        state["proxify_calls"].append({"url": url, "browser": browser, "raw": raw})
        if isinstance(state["proxify"], Exception):
            raise state["proxify"]
        return state["proxify"]

    monkeypatch.setattr(fetcher, "_fetch_native", native)
    from app.search.providers import google

    monkeypatch.setattr(google.ProxifyClient, "fetch", proxify_fetch)
    return state


def proxify_ok(body: bytes = b"<html>real</html>", final="https://e.com/a", status=200):
    return {"success": True, "status_code": status, "final_url": final, "body_b64": base64.b64encode(body).decode()}


async def test_native_success_never_calls_proxify(env):
    out = await fetcher.fetch_page("https://e.com/a")
    assert out.via == "native" and env["proxify_calls"] == []


async def test_blocked_native_falls_back(env):
    env["native"] = FetchError("HTTP 403 fetching https://e.com/a")
    env["proxify"] = proxify_ok()
    out = await fetcher.fetch_page("https://e.com/a")
    assert out.via == "proxify" and out.content == b"<html>real</html>"
    assert env["proxify_calls"][0]["raw"] is True
    # Auto strategy unless configured: Proxify escalates on its own.
    assert env["proxify_calls"][0]["browser"] is None


@pytest.mark.parametrize("exc", [NotFoundError("gone"), FetchTooLarge("big")])
async def test_answers_are_not_retried(env, exc):
    env["native"] = exc
    with pytest.raises(type(exc)):
        await fetcher.fetch_page("https://e.com/a")
    assert env["proxify_calls"] == []


async def test_challenge_page_falls_back(env):
    env["native"] = page(b"<html><head><title>Just a moment...</title></head></html>")
    env["proxify"] = proxify_ok()
    assert (await fetcher.fetch_page("https://e.com/a")).via == "proxify"


async def test_both_failing_reports_both_reasons(env):
    env["native"] = FetchError("timeout fetching https://e.com/a")
    env["proxify"] = ProxifyError("proxify: All strategies exhausted", status_code=None)
    with pytest.raises(FetchError, match="timeout.*proxify fallback also failed"):
        await fetcher.fetch_page("https://e.com/a")


async def test_proxify_404_is_not_found(env):
    env["native"] = FetchError("HTTP 403 fetching https://e.com/a")
    env["proxify"] = ProxifyError("proxify: HTTP 404 (not found)", status_code=404)
    with pytest.raises(NotFoundError):
        await fetcher.fetch_page("https://e.com/a")


async def test_landing_url_is_egress_checked(env, monkeypatch):
    """Proxify follows redirects itself, so where it landed must still pass policy."""
    env["native"] = FetchError("HTTP 403")
    env["proxify"] = proxify_ok(final="http://169.254.169.254/latest/meta-data")

    def guard(url):
        if "169.254" in url:
            raise ValidationError("URL refused by egress policy")

    monkeypatch.setattr(fetcher, "_validate_egress", guard)
    with pytest.raises(ValidationError):
        await fetcher.fetch_page("https://e.com/a")


async def test_disabled_fallback_reraises_native_error(env, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "proxify_url", "")
    env["native"] = FetchError("HTTP 503")
    with pytest.raises(FetchError, match="HTTP 503"):
        await fetcher.fetch_page("https://e.com/a")
    assert env["proxify_calls"] == []


def test_challenge_detection_ignores_large_and_normal_pages():
    # Cloudflare injects challenge-platform scripts into ordinary pages.
    assert not looks_like_challenge(page(b'<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'))
    assert not looks_like_challenge(page(b"<title>Just a moment...</title>" + b"x" * 70_000))
    assert not looks_like_challenge(page(b"%PDF-1.7 Just a moment...", ctype="application/pdf"))
    assert looks_like_challenge(page(b"<title>Attention Required! | Cloudflare</title>"))
