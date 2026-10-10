"""Populate ``documents.lang`` and ``documents.tsv`` for rows indexed before the
per-language column existed.

Migration 0006 adds the column as nullable; every row already in the table has a
NULL ``tsv`` and is therefore stemmed with the wrong rules at query time. This
rebuilds both for those rows without re-fetching anything.

Run after migrating:

    cd services/gateway && python -m app.backfill_language
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db import close_engine, get_session_factory, init_engine
from app.logging import get_logger, setup_logging
from app.models import Document
from app.search.langdetect import detect as detect_language
from app.tsvector import build_language_tsv

logger = get_logger("backfill_language")

BATCH = 200


async def run() -> dict[str, int]:
    session_factory = get_session_factory()
    seen = {"rows": 0, "updated": 0}

    while True:
        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(Document)
                    .where(Document.tsv.is_(None))
                    .where(Document.status == "indexed")
                    .order_by(Document.fetched_at.asc())
                    .limit(BATCH)
                )
            ).all()
            if not rows:
                break

            for doc in rows:
                seen["rows"] += 1
                body = f"{doc.title or ''} {doc.content_text or ''}"
                if not body.strip():
                    continue
                language = doc.lang or detect_language(body[:4000])
                doc.lang = language
                doc.tsv = await build_language_tsv(language, body)
                flag_modified(doc, "tsv")
                if language:
                    logger.info("backfilled %s -> lang=%s", (doc.url or "")[:70], language)
            await session.commit()
            seen["updated"] += len(rows)

        if len(rows) < BATCH:
            break
        # Yield so a large corpus does not starve the event loop of the worker
        # sharing this process.
        await asyncio.sleep(0)

    return seen


async def main() -> None:
    setup_logging()
    init_engine()
    try:
        result = await run()
        logger.info("language backfill complete: %s", result)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
