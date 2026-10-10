"""Query-side discovery endpoints: autocomplete, popular queries, freshness feed.

None of these are core search. They are the small surface an agent needs to
*form* a query — knowing what has been asked, what exists in the index, and what
was just crawled — none of which the search endpoint can express.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import ApiKey, current_api_key
from app.cache import get_cache
from app.db import get_session_factory
from app.models import Document

router = APIRouter(tags=["discovery"])


def _last_term(text: str) -> str:
    """The trailing word of a partial query, which is what is being completed."""
    import re

    terms = re.findall(r"[0-9A-Za-z_]+|[^\W\d_]+", text or "", re.UNICODE)
    return terms[-1] if terms else ""


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Suggestion(BaseModel):
    text: str
    count: int = 0


class PopularQuery(BaseModel):
    query: str
    count: int


class RecentDocument(BaseModel):
    url: str
    title: str | None = None
    domain: str | None = None
    fetched_at: str | None = None


class PopularResponse(BaseModel):
    queries: list[PopularQuery]


class RecentResponse(BaseModel):
    documents: list[RecentDocument]


async def _popular(prefix: str) -> list[tuple[str, int]]:
    cache = get_cache()
    return [row for row in await cache.top_queries(200) if row[0].lower().startswith(prefix.lower())]


@router.get("/v1/suggest")
async def suggest(
    q: Annotated[str, Query(min_length=1, max_length=120)],
    limit: Annotated[int, Query(ge=1, le=25)] = 8,
    _: ApiKey = Depends(current_api_key),
) -> list[Suggestion]:
    """Completions for a partial query.

    Merges two sources because either alone is incomplete: a term that appears
    in the index but was never queried (a freshly ingested page) and a term that
    was queried but did not match (a miss worth offering back).
    """
    prefix = (q or "").strip()
    if not prefix:
        return []
    needle = _last_term(prefix).lower()
    if not needle:
        return []

    out: list[Suggestion] = []
    seen: set[str] = set()

    for text, count in await get_cache().top_queries(200):
        if text.lower().startswith(prefix.lower()) and text.lower() not in seen:
            seen.add(text.lower())
            out.append(Suggestion(text=text, count=count))
        if len(out) >= limit:
            return out

    # FTS cannot answer "words starting with X" cheaply, so completion over the
    # corpus is a substring match on titles. The index is small enough that this
    # stays well under the latency budget, and it is capped by `limit * 4`.
    session_factory = get_session_factory()
    async with session_factory() as session:
        titles = (
            await session.scalars(
                select(Document.title)
                .where(Document.title.is_not(None))
                # `_` is a word character the term regex keeps, and a LIKE
                # wildcard: "my_var" also matched "myXvar".
                .where(func.lower(Document.title).like(f"%{_escape_like(needle)}%", escape="\\"))
                .order_by(Document.fetched_at.desc())
                .limit(limit * 4)
            )
        ).all()

    for title in titles:
        if len(out) >= limit:
            break
        key = title.lower()
        if key not in seen:
            seen.add(key)
            out.append(Suggestion(text=title))
    return out


@router.get("/v1/popular", response_model=PopularResponse)
async def popular(
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    _: ApiKey = Depends(current_api_key),
) -> PopularResponse:
    """Most-run queries, from the live Redis query counter."""
    rows = await get_cache().top_queries(limit)
    return PopularResponse(queries=[PopularQuery(query=q, count=c) for q, c in rows])


@router.get("/v1/recent", response_model=RecentResponse)
async def recent(
    hours: Annotated[int, Query(ge=1, le=24 * 90)] = 24,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    _: ApiKey = Depends(current_api_key),
) -> RecentResponse:
    """Documents the crawler indexed or re-crawled recently.

    The recrawl loop already knows what changed; this exposes it, which is what a
    "what's new" feed needs and what `search` cannot answer.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Document)
                    .where(Document.status == "indexed")
                    .where(Document.fetched_at >= cutoff)
                    .order_by(Document.fetched_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    from app.search.base import domain_of

    return RecentResponse(
        documents=[
            RecentDocument(
                url=doc.url,
                title=doc.title,
                domain=domain_of(doc.url),
                fetched_at=doc.fetched_at.isoformat() if doc.fetched_at else None,
            )
            for doc in rows
        ]
    )
