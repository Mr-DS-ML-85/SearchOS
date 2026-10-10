from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, replace

from app.errors import ValidationError
from app.extract.fetcher import FetchedPage


@dataclass
class ExtractedContent:
    title: str | None
    content: str
    language: str | None
    author: str | None
    published: str | None
    content_type: str


def extract_html(content: bytes, *, content_type: str | None = None) -> ExtractedContent:
    import re

    import trafilatura

    html = content.decode("utf-8", errors="ignore")
    options = {
        "include_comments": False,
        "include_tables": True,
        "favor_precision": True,
    }
    text = trafilatura.extract(html, **options)
    raw_title: str | None = None
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        raw_title = re.sub(r"\s+", " ", match.group(1)).strip() or None

    title: str | None = None
    language: str | None = None
    author: str | None = None
    published: str | None = None

    if text is None:
        meta = trafilatura.bare_extraction(html, with_metadata=True, favor_precision=True)
        if isinstance(meta, dict):
            text = meta.get("text", "")
            title = meta.get("title")
            language = meta.get("language")
            author = meta.get("author")
            published = meta.get("date")
    else:
        try:
            meta = trafilatura.metadata.extract_metadata(html)
            if meta is not None:
                title = title or meta.title
                language = language or meta.language
                author = author or meta.author
                published = published or meta.date
        except Exception:
            pass

    return ExtractedContent(
        title=raw_title or title,
        content=text or "",
        language=language,
        author=author,
        published=published,
        content_type=content_type or "text/plain",
    )


def extract_text(content: bytes, *, content_type: str | None = None) -> ExtractedContent:
    text = content.decode("utf-8", errors="ignore")
    return ExtractedContent(
        title=None,
        content=text,
        language=None,
        author=None,
        published=None,
        content_type=content_type or "text/plain",
    )


def extract_pdf(content: bytes, *, content_type: str | None = None) -> ExtractedContent:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return ExtractedContent(
        title=None,
        content="\n\n".join(pages),
        language=None,
        author=None,
        published=None,
        content_type=content_type or "application/pdf",
    )


def extract_page(page: FetchedPage, *, fmt: str = "text") -> ExtractedContent:
    """Dispatch on content type.

    Previously anything not PDF/plain/json — including image/png, application/zip
    and application/octet-stream — was fed to the HTML extractor and failed
    later with "empty extracted content". Binary types are now rejected up front.
    """
    content_type = (page.content_type or "").lower()
    mime = content_type.split(";", 1)[0].strip()

    # HTML must be checked *before* the generic text/ branch: text/html matches
    # text/*, and testing text/ first returned raw markup instead of extracted
    # article text.
    if mime in ("text/html", "application/xhtml+xml") or mime.endswith("+xml"):
        extracted = extract_html(page.content, content_type=page.content_type)
    elif mime == "application/pdf" or mime.endswith("/pdf"):
        extracted = extract_pdf(page.content, content_type=page.content_type)
    elif mime.startswith("text/") or mime == "application/json" or mime.endswith("+json"):
        extracted = extract_text(page.content, content_type=page.content_type)
    elif not mime:
        # No content-type header at all: try HTML, which is the common case.
        extracted = extract_html(page.content, content_type=page.content_type)
    else:
        if mime.startswith(("image/", "audio/", "video/", "font/")):
            raise ValidationError(f"cannot extract content of type {mime}")
        raise ValidationError(f"unsupported content type: {mime}")

    return _render(extracted, fmt, url=page.final_url or page.url)


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)


def _to_markdown(text: str) -> str:
    """Lightweight text -> markdown.

    trafilatura emits plain text, so the conversion is conservative: keep
    headings recognisable and paragraphs separated, without pretending to be a
    full HTML->markdown pass (which would mean re-parsing what we just
    deliberately stripped).
    """
    paragraphs = [block.strip() for block in re.split(r"\n{2,}", text) if block.strip()]
    return "\n\n".join(paragraphs)


def _render(extracted: ExtractedContent, fmt: str, *, url: str | None = None) -> ExtractedContent:
    """Render the extracted text into the requested output format.

    ``format`` was previously accepted and ignored: `json` returned the exact
    same bytes as `markdown`. A no-op flag is worse than no flag, so json now
    emits a real JSON document and markdown actually produces markdown.
    """
    fmt = (fmt or "text").lower()

    if fmt == "text":
        return extracted

    if fmt == "markdown":
        return replace(
            extracted,
            content=_to_markdown(extracted.content),
            content_type="text/markdown",
        )

    if fmt == "json":
        payload = {
            "url": url,
            "title": extracted.title,
            "language": extracted.language,
            "author": extracted.author,
            "published": extracted.published,
            "content_type": extracted.content_type,
            "content": extracted.content,
            "content_format": "text",
        }
        return replace(
            extracted,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            content_type="application/json",
        )

    raise ValidationError(f"unsupported extract format: {fmt}")
