from __future__ import annotations

import asyncio

from app.db import close_engine, get_session_factory, init_engine
from app.logging import get_logger, setup_logging
from app.workers.backfill import backfill_loop
from app.workers.ingest import ingest_worker_loop, recrawl_worker_loop
from app.workers.snippet import snippet_worker_loop

logger = get_logger("workers.runner")


async def serve_metrics(port: int) -> None:
    """Expose /metrics so worker-side counters are actually scraped.

    documents_indexed, chunks_indexed, queue_depth and embedding_failures are
    incremented here, but the worker ran no HTTP server — so every one of them
    died with the process and a stopped crawler looked identical to an idle one.
    Bound to all interfaces inside the compose network only; the worker service
    publishes no host port.
    """
    from prometheus_client import start_http_server

    start_http_server(port)
    logger.info("worker metrics listening on :%d/metrics", port)


async def main() -> None:
    setup_logging()
    init_engine()
    session_factory = get_session_factory()

    from app.config import get_settings

    settings = get_settings()
    await serve_metrics(settings.worker_metrics_port)

    tasks = [
        asyncio.create_task(ingest_worker_loop(session_factory), name="ingest"),
        asyncio.create_task(recrawl_worker_loop(session_factory), name="recrawl"),
        asyncio.create_task(snippet_worker_loop(session_factory), name="snippet"),
    ]
    if settings.backfill_enabled:
        tasks.append(asyncio.create_task(backfill_loop(session_factory), name="backfill"))
    logger.info("workers started")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    except Exception as exc:
        # One loop dying used to take all three down silently.
        logger.error("worker loop crashed: %s: %s", exc.__class__.__name__, exc)
        raise
    finally:
        await close_engine()
        logger.info("workers stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
