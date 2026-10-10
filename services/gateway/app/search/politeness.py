"""Per-origin politeness for the crawler.

The worker fetches one job at a time with no delay between requests to the same
host, so a recursive crawl of a documentation site fires requests as fast as the
network allows. That is how a crawler gets a host to block it, and blocking one
origin can take out the whole seed.

The gate runs before every outbound fetch and enforces two things:

- ``robots.txt``'s ``Crawl-delay`` when the origin declares one, and
- ``crawler_min_delay_seconds`` otherwise, so an origin that omits the
  directive is still throttled.

State lives in Redis rather than in the worker process so every worker shares one
schedule, and so a restart cannot reset an origin's window and immediately
re-burst against it.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from app.cache import get_cache
from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics

logger = get_logger("search.politeness")

_KEY_PREFIX = "crawl:last_hit:"
_LAST_HIT_TTL = 3600

# Fallback used when Redis is unreachable. Without it the gate would silently
# disappear and the crawler would resume bursting at an origin, which is the
# failure this module exists to prevent. It only covers this process, so a
# multi-worker deployment degrades to per-worker spacing rather than to none.
_local_last_hit: dict[str, float] = {}


def _prune_local(now: float) -> None:
    if len(_local_last_hit) > 2048:
        stale = [k for k, t in _local_last_hit.items() if now - t > _LAST_HIT_TTL]
        for key in stale:
            _local_last_hit.pop(key, None)


def origin_of(url: str) -> str:
    """Scheme + host + port. Two paths on one host share a budget."""
    parts = urlsplit(url or "")
    if not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


async def _declared_delay(url: str) -> float:
    """robots.txt Crawl-delay if declared, else the configured minimum.

    A missing or unreachable robots.txt is not an error here — the fetch itself
    still enforces the allow/disallow rules — so this falls back rather than
    raising.
    """
    settings = get_settings()
    from app.extract.robots import RobotsChecker

    try:
        declared = await RobotsChecker().crawl_delay(url)
    except Exception:
        declared = None
    if declared is not None and declared >= 0:
        return max(declared, settings.crawler_min_delay_seconds)
    return settings.crawler_min_delay_seconds


async def wait_for_origin(url: str, delay_seconds: float | None = None) -> float:
    """Block until this origin may be fetched again. Returns seconds waited."""
    if delay_seconds is None:
        delay_seconds = await _declared_delay(url)
    if delay_seconds <= 0:
        return 0.0

    cache = get_cache()
    key = f"{_KEY_PREFIX}{origin_of(url)}"
    now = time.monotonic()
    wait = 0.0
    try:
        last = await cache.get(key)
        if last is not None:
            wait = max(0.0, delay_seconds - (now - float(last)))
        # Stamp before waiting: concurrent jobs must not all read the same stale
        # timestamp and then sleep out the same window in parallel.
        await cache.set(key, now, ttl_seconds=_LAST_HIT_TTL)
    except Exception:
        wait = 0.0

    _prune_local(now)
    if wait <= 0:
        previous = _local_last_hit.get(key)
        if previous is not None:
            wait = max(0.0, delay_seconds - (now - previous))
    _local_last_hit[key] = now

    if wait > 0:
        logger.debug("politeness: waiting %.2fs before fetching %s", wait, origin_of(url))
        get_metrics().crawl_politeness_wait.observe(wait)
        await asyncio.sleep(wait)
    return wait
