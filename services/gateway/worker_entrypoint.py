import asyncio

from alembic import command
from alembic.config import Config

from app.workers.runner import main


def run_migrations() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


async def run() -> None:
    run_migrations()
    await main()


if __name__ == "__main__":
    asyncio.run(run())
