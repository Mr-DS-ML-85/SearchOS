import asyncio
import os
import sys
from pathlib import Path

from app.logging import get_logger, setup_logging
from app.workers.runner import main

logger = get_logger("worker.entrypoint")

GATEWAY_DIR = Path(__file__).resolve().parent


def run_migrations() -> None:
    """Optional migration run, gated by WORKER_RUN_MIGRATIONS.

    The gateway command and this entrypoint both ran `alembic upgrade head`
    concurrently with no advisory lock and no ordering, so a cold start could
    interleave DDL or leave the gateway blocked behind the worker. The gateway
    is the single migration owner by default; set WORKER_RUN_MIGRATIONS=true to
    restore worker-owned migrations.
    """
    from app.config import get_settings

    if not get_settings().worker_run_migrations:
        logger.info("skipping migrations (WORKER_RUN_MIGRATIONS is not true; gateway owns them)")
        return

    # Config("alembic.ini") is relative to the CWD; using an absolute path means
    # `python services/gateway/worker_entrypoint.py` works from anywhere.
    os.chdir(GATEWAY_DIR)
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(GATEWAY_DIR / "alembic.ini")), "head")


async def run() -> None:
    setup_logging()
    run_migrations()
    await main()


if __name__ == "__main__":
    if str(GATEWAY_DIR) not in sys.path:
        sys.path.insert(0, str(GATEWAY_DIR))
    asyncio.run(run())
