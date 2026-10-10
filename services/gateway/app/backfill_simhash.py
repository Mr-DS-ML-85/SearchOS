"""Populate ``documents.simhash`` for rows indexed before migration 0007.

Migration 0007 adds the column as nullable; every existing row has a NULL
fingerprint, so near-duplicate detection cannot see it and syndicated or mirrored
pages keep occupying several result slots. This rebuilds them from content that
is already stored — no re-fetch.

    cd services/gateway && python -m app.backfill_simhash
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select, update

from app.db import close_engine, get_session_factory, init_engine
from app.logging import get_logger, setup_logging
from app.models import Document
from app.search.simhash import simhash

logger = get_logger("backfill_simhash")

BATCH = 200


async def run() -> dict[str, int]:
    session_factory = get_session_factory()
    seen = {"rows": 0, "updated": 0}

    while True:
        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(Document)
                    .where(Document.simhash.is_(None))
                    .where(Document.status == "indexed")
                    .order_by(Document.fetched_at.asc())
                    .limit(BATCH)
                )
            ).all()
            if not rows:
                break
            for doc in rows:
                seen["rows"] += 1
                text = f"{doc.title or ''} {doc.content_text or ''}"
                if not text.strip():
                    continue
                await session.execute(update(Document).where(Document.id == doc.id).values(simhash=simhash(text)))
            await session.commit()
            seen["updated"] += len(rows)

        if len(rows) < BATCH:
            break
        await asyncio.sleep(0)

    return seen


async def main() -> None:
    setup_logging()
    init_engine()
    try:
        logger.info("simhash backfill complete: %s", await run())
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
