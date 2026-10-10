"""Discover, download and CLIP-embed images found on indexed pages."""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.models import Image

logger = get_logger("search.images.index")

_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"'>]+)[\"']", re.IGNORECASE)
_ALT_RE = re.compile(r"<img[^>]+alt=[\"']([^\"'>]*)[\"']", re.IGNORECASE)

_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_SRCSET_RE = re.compile(r"""(\d+)[w]\s*,|(\d+)\.\d+[xw]""", re.IGNORECASE)

# Decorative assets. An <img> for an edit shield or an iucn badge is real HTML
# but useless to search, and there are usually far more of them than content.
_ICON_HINTS = (
    "icon",
    "logo",
    "badge",
    "button",
    "shackle",
    "symbol",
    "sprite",
    "wiki",
    "edit-",
    "ambox",
    "commons-logo",
    "wikimedia-button",
    "poweruser",
    "portal",
    "flag",
    "stub",
    "template",
    "padlock",
    "magnify",
    "search",
    "user",
    "star",
    "wikidata",
)


def _attrs(tag: str) -> dict[str, str]:
    # Attribute values are HTML-escaped: src="a.jpg?x=1&amp;y=2" is the URL
    # a.jpg?x=1&y=2. Stored raw, every query-string URL kept a literal "&amp;".
    return {m.group(1).lower(): html.unescape(m.group(2) or m.group(3) or "") for m in _ATTR_RE.finditer(tag)}


def extract_image_urls(html: str, base_url: str, limit: int) -> list[tuple[str, str | None]]:
    """Content image URLs from a page, best candidates first, paired with alt text.

    Ordered by declared size rather than document order: a page's first dozen
    <img> tags are its logo, edit shields and badges, while the photograph a user
    would search for sits below the fold. Taking the first N returned nothing
    indexable at all.
    """
    if not html:
        return []
    candidates: list[tuple[int, int, str, str | None]] = []
    seen: set[str] = set()

    for tag in _IMG_TAG_RE.findall(html):
        attr = _attrs(tag)
        src = attr.get("src") or attr.get("data-src")
        if not src or src.startswith("data:"):
            continue
        url = urljoin(base_url, src.strip())
        if url in seen or not url.startswith(("http://", "https://")):
            continue

        lowered = url.lower().split("?")[0]
        stem = lowered.rsplit("/", 1)[-1]
        # Wikimedia serves SVG icons as "<name>.svg.png", so the raster-looking
        # suffix is not enough to tell a photo from an icon.
        if ".svg" in stem or stem.endswith(".ico") or any(hint in stem for hint in _ICON_HINTS):
            continue

        # Prefer the largest candidate offered in srcset.
        width = 0
        srcset = attr.get("srcset", "")
        if srcset:
            sizes = [int(g[0] or g[1] or 0) for g in _SRCSET_RE.findall(srcset)]
            sizes = [s for s in sizes if s]
            if sizes:
                width = max(sizes)
        if not width:
            try:
                width = int(re.sub(r"[^0-9]", "", attr.get("width", "")) or 0)
            except ValueError:
                width = 0
        height = 0
        try:
            height = int(re.sub(r"[^0-9]", "", attr.get("height", "")) or 0)
        except ValueError:
            height = 0
        # Unsized images still deserve a chance; score them mid-table.
        score = (width * height) if (width and height) else (width * 300)
        seen.add(url)
        candidates.append((score, width, url, attr.get("alt") or None))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [(url, alt) for _score, _w, url, alt in candidates[:limit]]


async def index_images_for_page(
    session_factory: async_sessionmaker,
    *,
    document_id,
    source_url: str,
    html: str,
) -> int:
    """Download and embed the images on a page. Returns how many were indexed."""
    from app.search import images as clip

    settings = get_settings()
    if not settings.images_enabled or not clip.available():
        return 0

    candidates = extract_image_urls(html, source_url, settings.image_max_per_document)
    if not candidates:
        return 0

    async with session_factory() as session:
        existing = set(
            (await session.scalars(select(Image.url).where(Image.url.in_([u for u, _ in candidates])))).all()
        )

    to_download = [(u, a) for u, a in candidates if u not in existing]
    if not to_download:
        return 0

    from app.http import get_http_client

    payloads: list[tuple[str, str | None, bytes, int, int]] = []
    client = get_http_client()
    for url, alt in to_download:
        try:
            resp = await client.get(
                url,
                timeout=settings.extract_timeout_seconds,
                follow_redirects=True,
            )
            if resp.status_code >= 400:
                continue
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype not in _IMAGE_TYPES:
                continue
            if len(resp.content) > settings.image_max_bytes or len(resp.content) < 2048:
                continue
            width, height = _dimensions(resp.content)
            if min(width, height) < settings.image_min_dimension:
                continue
            payloads.append((url, alt, resp.content, width, height))
        except Exception as exc:
            logger.debug("image fetch skipped %s: %s", url[:70], exc)
            continue

    if not payloads:
        return 0

    vectors = await clip.embed_images([p[2] for p in payloads])
    if not vectors:
        return 0

    written = 0
    async with session_factory() as session:
        for (url, alt, raw, w, h), vector in zip(payloads, vectors, strict=True):
            session.add(
                Image(
                    document_id=document_id,
                    source_url=source_url,
                    url=url,
                    alt_text=alt,
                    content_type=_guess_type(url),
                    byte_size=len(raw),
                    width=w,
                    height=h,
                    embedding=vector,
                    phash=phash(raw),
                    status="indexed",
                )
            )
            written += 1
        await session.commit()
    get_metrics().images_indexed.inc(written)
    logger.info("indexed %d image(s) from %s", written, source_url[:70])
    return written


def _dimensions(raw: bytes) -> tuple[int, int]:
    try:
        import io

        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(raw)) as img:
            return img.width, img.height
    except Exception:
        return 0, 0


def _guess_type(url: str) -> str:
    lowered = url.lower().split("?")[0]
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".gif"):
        return "image/gif"
    if lowered.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def phash(raw: bytes) -> int | None:
    """64-bit average-hash, used only to collapse identical assets."""
    try:
        import io

        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(raw)) as img:
            small = img.convert("L").resize((8, 8), PILImage.BILINEAR)
            pixels = list(small.getdata())
        if len(pixels) != 64:
            return None
        mean = sum(pixels) / len(pixels)
        out = 0
        for index, value in enumerate(pixels):
            if value > mean:
                out |= 1 << index
        # The bit pattern is naturally unsigned 64-bit; Postgres BIGINT is
        # signed, so a hash with the top bit set has to be reinterpreted rather
        # than rejected as out of range.
        if out >= 1 << 63:
            out -= 1 << 64
        return out
    except Exception:
        return None
