from __future__ import annotations

import asyncio

from app.db import get_session_factory, init_engine
from app.logging import get_logger, setup_logging
from app.workers.ingest import ingest_worker_loop, recrawl_worker_loop
from app.workers.snippet import snippet_worker_loop

logger = get_logger("workers.runner")


async def main() -> None:
    setup_logging()
    init_engine()
    session_factory = get_session_factory()
    tasks = [
        asyncio.create_task(ingest_worker_loop(session_factory)),
        asyncio.create_task(recrawl_worker_loop(session_factory)),
        asyncio.create_task(snippet_worker_loop(session_factory)),
    ]
    logger.info("workers started")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
