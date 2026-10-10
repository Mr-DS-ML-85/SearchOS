from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

app = FastAPI(title="SearchOS Renderer", version="1.0.0")

RENDER_TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "30000"))
MAX_RENDER_BYTES = int(os.getenv("MAX_RENDER_BYTES", "10485760"))
RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "4"))
USER_AGENT = "SearchOSBot/1.0 (+https://searchos.local/bot)"
# Shared secret the gateway sends on every /render call. The endpoint was
# previously unauthenticated while being published on the host, which made it an
# open SSRF proxy into the compose network.
RENDER_TOKEN = os.getenv("RENDERER_TOKEN", "")
BLOCK_PRIVATE_NETWORKS = os.getenv("RENDERER_BLOCK_PRIVATE_NETWORKS", "true").lower() == "true"
ALLOW_PRIVATE_HOSTS = {
    h.strip().lower() for h in os.getenv("RENDERER_ALLOW_PRIVATE_NETWORKS", "").split(",") if h.strip()
}
# Chromium's own sandbox is left on unless explicitly disabled; --no-sandbox was
# unconditional before, which weakened isolation for attacker-controlled pages.
DISABLE_CHROMIUM_SANDBOX = os.getenv("RENDERER_DISABLE_SANDBOX", "false").lower() == "true"

rend_total = Counter("searchos_renderer_total", "Render requests", ["status"])
rend_duration = Gauge("searchos_renderer_active", "Currently active render pages")
blocked_total = Counter("searchos_renderer_blocked_total", "Requests refused by policy", ["reason"])

BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(c)
    for c in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)


def _is_blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(str(ip.ipv4_mapped))
    return any(ip in net for net in BLOCKED_NETWORKS)


def validate_target(url: str) -> None:
    """Refuse non-public destinations.

    Runs before navigation *and* again on the final URL, because page.goto
    follows redirects and the redirect target is only known afterwards.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        blocked_total.labels(reason="scheme").inc()
        raise HTTPException(status_code=400, detail="url must be http(s)")
    try:
        host = parts.hostname
    except ValueError as exc:
        blocked_total.labels(reason="malformed").inc()
        raise HTTPException(status_code=400, detail=f"malformed url: {exc}") from exc
    if not host:
        blocked_total.labels(reason="no_host").inc()
        raise HTTPException(status_code=400, detail="url has no host")
    if not BLOCK_PRIVATE_NETWORKS:
        return
    if any(host == e or host.endswith(f".{e}") for e in ALLOW_PRIVATE_HOSTS):
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        blocked_total.labels(reason="dns").inc()
        raise HTTPException(status_code=400, detail=f"cannot resolve host: {host}") from exc
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            blocked_total.labels(reason="private_ip").inc()
            raise HTTPException(
                status_code=403,
                detail=f"destination is not a public address ({host} -> {info[4][0]})",
            )


def require_token(request: Request) -> None:
    if not RENDER_TOKEN:
        return
    header = request.headers.get("x-renderer-token", "")
    if not hmac.compare_digest(header, RENDER_TOKEN):
        blocked_total.labels(reason="auth").inc()
        raise HTTPException(status_code=401, detail="invalid renderer token")


_playwright = None
_browser = None
_semaphore = None
_init_lock = asyncio.Lock()


async def get_browser():
    """Launch Chromium once.

    Guarded by _init_lock: the previous version had an unsynchronised
    ``if _browser is None`` check, so two concurrent first requests could each
    start a Playwright driver and leak one. get_semaphore() right below already
    used a lock, so the pattern was known and simply missed here.
    """
    global _playwright, _browser
    async with _init_lock:
        if _browser is None:
            from playwright.async_api import async_playwright

            args = ["--disable-gpu", "--disable-dev-shm-usage"]
            if DISABLE_CHROMIUM_SANDBOX:
                args.append("--no-sandbox")
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True, args=args)
    return _browser


async def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    async with _init_lock:
        if _semaphore is None:
            # Per-process: with `--workers 2` the effective limit is 2x this
            # value, which the compose env comment did not reflect.
            _semaphore = asyncio.Semaphore(RENDER_CONCURRENCY)
        return _semaphore


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    global _browser, _playwright
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


app.router.lifespan_context = lifespan


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "browser_ready": _browser is not None,
        "concurrency": RENDER_CONCURRENCY,
        "auth_required": bool(RENDER_TOKEN),
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/render")
async def render(request: Request, url: str, max_bytes: int = MAX_RENDER_BYTES) -> Response:
    require_token(request)
    validate_target(url)

    browser = await get_browser()
    semaphore = await get_semaphore()
    async with semaphore:
        rend_duration.inc()
        page = await browser.new_page(user_agent=USER_AGENT, viewport={"width": 1366, "height": 768})
        try:
            nav = await page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(RENDER_TIMEOUT_MS, 15000))
            except Exception:
                pass
            html = await page.content()
            final_url = page.url
            # The *rendered page's* status, not this API call's. Chromium happily
            # returns a complete empty DOM for a 404 or a 500, so without this
            # the gateway sees HTTP 200 and cannot tell an error page from a real
            # one.
            page_status = nav.status if nav is not None else None
        except HTTPException:
            raise
        except Exception as e:
            rend_total.labels(status="error").inc()
            raise HTTPException(status_code=502, detail=f"render failed: {e.__class__.__name__}") from e
        finally:
            rend_duration.dec()
            try:
                await page.close()
            except Exception:
                pass

    # Re-validate: page.goto followed redirects, so the destination was unknown
    # when the pre-flight check ran.
    try:
        validate_target(final_url)
    except HTTPException:
        rend_total.labels(status="blocked_redirect").inc()
        raise

    body = html.encode("utf-8", errors="ignore")
    if len(body) > max_bytes:
        rend_total.labels(status="too_large").inc()
        raise HTTPException(status_code=413, detail="rendered page exceeds max_bytes")
    if page_status is not None and page_status >= 400:
        rend_total.labels(status="upstream_error").inc()
    headers = {"X-Final-Url": final_url}
    if page_status is not None:
        headers["X-Page-Status"] = str(page_status)
    rend_total.labels(status="ok").inc()
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers=headers,
    )
