from __future__ import annotations

import io
import time
from dataclasses import dataclass

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


def extract_page(page: FetchedPage) -> ExtractedContent:
    start = time.monotonic()
    content_type = (page.content_type or "").lower()
    if "application/pdf" in content_type:
        extracted = extract_pdf(page.content, content_type=page.content_type)
    elif "text/plain" in content_type or "json" in content_type:
        extracted = extract_text(page.content, content_type=page.content_type)
    else:
        extracted = extract_html(page.content, content_type=page.content_type)
    return extracted
