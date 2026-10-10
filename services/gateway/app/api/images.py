"""Multimodal search: find images by text, or images like another image.

CLIP embeds text and images into one space, so both directions are a cosine
similarity query rather than a special case.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import ApiKey, current_api_key
from app.db import get_session_factory
from app.models import Image

router = APIRouter(tags=["images"])


class ImageHit(BaseModel):
    url: str
    source_url: str | None = None
    alt_text: str | None = None
    width: int | None = None
    height: int | None = None
    score: float


class ImageSearchResponse(BaseModel):
    query: str | None = None
    images: list[ImageHit]
    took_ms: int
    image_search_available: bool
    min_score: float | None = None
    #: Nearest images dropped by ``min_score``. Distinguishes "nothing in the
    #: index depicts this" from "the index has no images".
    below_min_score: int = 0
    #: "local" (CLIP over the crawled index) or "google" (Google Images via Proxify).
    source: str = "local"


@router.get("/v1/images/search", response_model=ImageSearchResponse)
async def search_images(
    q: Annotated[str | None, Query(max_length=300)] = None,
    image_url: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    source: Annotated[Literal["local", "google"], Query()] = "local",
    _: ApiKey = Depends(current_api_key),
) -> ImageSearchResponse:
    """Text-to-image, or image-to-image when ``image_url`` is given.

    Omitting both is an error rather than an empty result: returning nothing for
    a query that was never supplied looks like a broken index.
    """
    import time

    from app.errors import ValidationError
    from app.search import images as clip

    started = time.monotonic()
    if not q and not image_url:
        raise ValidationError("provide q (text) or image_url (image)")
    if q and image_url:
        # image_url used to win silently, discarding the text the caller sent.
        raise ValidationError("provide q (text) or image_url (image), not both")

    if source == "google":
        return await _google_images(q, image_url, limit, started)

    if not clip.available():
        return ImageSearchResponse(
            query=q or image_url,
            images=[],
            took_ms=int((time.monotonic() - started) * 1000),
            image_search_available=False,
        )

    if image_url:
        vector = await _embed_by_url(image_url)
    else:
        vectors = await clip.embed_texts([q or ""])
        vector = vectors[0] if vectors else None
    if not vector:
        return ImageSearchResponse(
            query=q or image_url,
            images=[],
            took_ms=int((time.monotonic() - started) * 1000),
            image_search_available=True,
        )

    from app.config import get_settings

    min_score = None if image_url else get_settings().image_text_min_score
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Image)
                    .where(Image.embedding.is_not(None))
                    .order_by(Image.embedding.cosine_distance(vector))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    hits = [
        ImageHit(
            url=row.url,
            source_url=row.source_url,
            alt_text=row.alt_text,
            width=row.width,
            height=row.height,
            # Ordering already came from pgvector's cosine_distance. The
            # returned column is a plain list, not a Vector, so recompute the
            # similarity as a dot product — CLIP vectors are L2-normalised,
            # so that is the cosine.
            score=round(_dot(row.embedding, vector), 4),
        )
        for row in rows
    ]
    kept = [h for h in hits if min_score is None or h.score >= min_score]
    return ImageSearchResponse(
        query=q or image_url,
        images=kept,
        took_ms=int((time.monotonic() - started) * 1000),
        image_search_available=True,
        min_score=min_score,
        below_min_score=len(hits) - len(kept),
    )


async def _google_images(q: str | None, image_url: str | None, limit: int, started: float) -> ImageSearchResponse:
    """Google Images through Proxify. Text queries only.

    ``url`` is Google's thumbnail (full-size URLs are not in the results page);
    ``source_url`` is the page the image appears on. Scores encode Google's
    order, since Google gives no similarity.
    """
    import time

    from app.errors import ProviderError, ValidationError
    from app.search.providers.google import ProxifyError, build_client
    from app.search.providers.google import search_images as google_search_images

    if image_url:
        raise ValidationError("source=google supports text queries (q) only")
    client = build_client()
    if client is None:
        raise ProviderError("Google image search is not configured: set PROXIFY_URL")
    try:
        items = await google_search_images(client, q or "", limit)
    except ProxifyError as e:
        raise ProviderError(f"google images: {e}") from e
    return ImageSearchResponse(
        query=q,
        images=[
            ImageHit(
                url=item["thumbnail"],
                source_url=item.get("source_url"),
                alt_text=item["title"],
                score=round(1.0 - rank * 0.01, 4),
            )
            for rank, item in enumerate(items)
        ],
        took_ms=int((time.monotonic() - started) * 1000),
        image_search_available=True,
        source="google",
    )


def _dot(a, b) -> float:
    if not a:
        return 0.0
    total = 0.0
    for x, y in zip(a, b, strict=False):
        total += float(x) * float(y)
    return max(0.0, min(1.0, total))


async def _embed_by_url(url: str) -> list[float] | None:
    """Download and embed a caller-supplied image URL.

    Goes through the same egress guard as every other outbound fetch: this
    endpoint takes an arbitrary URL from the caller, so without it the image
    path would be an SSRF hole that the document fetch path already closes.
    """
    from app.config import get_settings
    from app.http import get_http_client
    from app.netguard import validate_url
    from app.search import images as clip

    settings = get_settings()
    client = get_http_client()
    try:
        validate_url(url)
        resp = await client.get(
            url,
            timeout=settings.extract_timeout_seconds,
            follow_redirects=True,
        )
        if resp.status_code >= 400 or not resp.content:
            return None
        if len(resp.content) > settings.image_max_bytes:
            return None
        raw = resp.content
    except Exception:
        return None
    return await clip.embed_one_image(raw)
