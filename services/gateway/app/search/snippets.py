"""Query-aware snippet construction.

Snippets were the first ``length`` characters of the document, which is close to
worthless: the opening paragraph of a long page rarely contains what was asked
for, so a hit deep inside a document produced a snippet with no visible reason to
click it.

This picks the densest window of query-term matches instead, and reports which
terms matched so a caller can highlight them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TERM_RE = re.compile(r"[0-9A-Za-z_]+|[^\W\d_]+", re.UNICODE)

DEFAULT_LENGTH = 300
_MIN_TERM = 2


@dataclass
class Snippet:
    text: str
    highlights: list[str]
    matched: bool


def query_terms(query: str) -> list[str]:
    return [t for t in TERM_RE.findall((query or "").lower()) if len(t) >= _MIN_TERM]


def _best_window(lowered: str, terms: list[str], positions: list[int], length: int) -> int:
    """Offset of the window covering the most distinct terms; earliest wins ties."""
    best_offset = positions[0]
    best_key = (-1, -1)
    for start_pos in positions:
        end = start_pos + length
        covered: dict[str, int] = {}
        for pos in positions:
            if pos < start_pos or pos >= end:
                continue
            for term in terms:
                if lowered.startswith(term, pos):
                    covered[term] = covered.get(term, 0) + 1
        key = (len(covered), sum(covered.values()))
        if key > best_key:
            best_key = key
            best_offset = start_pos
    return best_offset


def build_snippet(content: str, query: str, length: int = DEFAULT_LENGTH) -> Snippet:
    """Return the best-matching window of ``content`` for ``query``.

    Falls back to the leading window when nothing matches, and to an empty
    snippet when there is no content at all.
    """
    text = (content or "").strip()
    if not text:
        return Snippet(text="", highlights=[], matched=False)

    terms = query_terms(query)
    if not terms or len(text) <= length:
        return Snippet(text=text[:length].strip(), highlights=[], matched=False)

    lowered = text.lower()
    positions: list[int] = []
    for term in terms:
        start = lowered.find(term)
        while start >= 0:
            positions.append(start)
            start = lowered.find(term, start + len(term))
    if not positions:
        return Snippet(text=text[:length].strip(), highlights=[], matched=False)

    positions.sort()
    anchor = _best_window(lowered, terms, positions, length)

    start = max(0, anchor - length // 6)
    end = min(len(text), start + length)
    window = text[start:end].strip()
    if start > 0:
        window = "…" + window
    if end < len(text):
        window += "…"

    matched = [t for t in terms if t in lowered[anchor : anchor + length]]
    return Snippet(text=window, highlights=sorted(set(matched)), matched=True)
