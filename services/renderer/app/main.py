from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, Response

app = FastAPI(title="SearchOS Renderer", version="1.0.0")

RENDER_TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "30000"))
MAX_RENDER_BYTES = int(os.getenv("MAX_RENDER_BYTES", "10485760"))
RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "4"))
USER_AGENT = "SearchOSBot/1.0 (+https://searchos.local/bot)"


_playwright = None
_browser = None
_semaphore = None
_semaphore_lock = asyncio.Lock()


async def get_browser():
    global _playwright, _browser
    if _browser is None:
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
    return _browser


async def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    async with _semaphore_lock:
        if _semaphore is None:
            _semaphore = asyncio.Semaphore(RENDER_CONCURRENCY)
        return _semaphore


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/render")
async def render(url: str, max_bytes: int = MAX_RENDER_BYTES) -> Response:
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be http(s)")

    browser = await get_browser()
    semaphore = await get_semaphore()
    async with semaphore:
        page = await browser.new_page(user_agent=USER_AGENT, viewport={"width": 1366, "height": 768})
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(RENDER_TIMEOUT_MS, 15000))
            except Exception:
                pass
            html = await page.content()
            final_url = page.url
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"render failed: {e.__class__.__name__}")
        finally:
            await page.close()

    body = html.encode("utf-8", errors="ignore")
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail="rendered page exceeds max_bytes")
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={"X-Final-Url": final_url},
    )
