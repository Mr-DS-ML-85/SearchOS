from __future__ import annotations

import re


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def estimate_tokens(text: str) -> int:
    """Rough token count — 4 characters per token.

    This is a heuristic, not a BPE count, but it is applied consistently: the
    budget checks below measure the *assembled* chunk text, not the sum of its
    parts. The previous code summed per-unit estimates (each truncated down)
    while the emitted chunk paid for the inserted separators, so the guard fired
    up to 3x too late.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def chunk_text(text: str, *, max_tokens: int = 512, overlap_tokens: int = 64) -> list[str]:
    if not text:
        return []
    if max_tokens <= 0:
        return [text]

    chunks: list[str] = []
    current: list[str] = []

    def assembled(parts: list[str]) -> str:
        return " ".join(parts)

    def flush() -> None:
        nonlocal current
        if current:
            joined = assembled(current)
            if joined.strip():
                chunks.append(joined)
        # Overlap: carry the tail of what we just emitted into the next chunk.
        # Cap the carry at max_tokens so an oversized unit cannot be duplicated
        # wholesale (the old _tail kept at least one full unit regardless of the
        # overlap budget, which doubled chunk size).
        carry: list[str] = []
        budget = min(overlap_tokens, max_tokens)
        used = 0
        for unit in reversed(current):
            unit_tokens = estimate_tokens(unit)
            if used + unit_tokens > budget:
                break
            carry.insert(0, unit)
            used += unit_tokens
        current = carry

    for sentence in split_sentences(text):
        for unit in _split_oversized(sentence, max_tokens):
            if current and estimate_tokens(assembled(current + [unit])) > max_tokens:
                flush()
            current.append(unit)

    if current:
        joined = assembled(current)
        if joined.strip():
            chunks.append(joined)
    return [c for c in chunks if c.strip()]


def _split_oversized(sentence: str, max_tokens: int) -> list[str]:
    """Break a unit into pieces that each fit within ``max_tokens``.

    Handles three cases the old implementation did not:
    - a single word longer than max_tokens (long URLs, base64 blobs)
    - text with no whitespace at all (CJK, minified content) — previously
      produced one giant chunk that silently overflowed the embedding model's
      context window
    - multi-word sentences, split on word boundaries
    """
    if estimate_tokens(sentence) <= max_tokens:
        return [sentence]

    words = sentence.split()
    if len(words) <= 1:
        return _split_by_chars(sentence, max_tokens)

    parts: list[str] = []
    current: list[str] = []
    for word in words:
        if estimate_tokens(word) > max_tokens:
            if current:
                parts.append(" ".join(current))
                current = []
            parts.extend(_split_by_chars(word, max_tokens))
            continue
        if current and estimate_tokens(" ".join(current + [word])) > max_tokens:
            parts.append(" ".join(current))
            current = []
        current.append(word)
    if current:
        parts.append(" ".join(current))
    return parts


def _split_by_chars(text: str, max_tokens: int) -> list[str]:
    """Hard character-level split; the last resort for unsplittable input."""
    step = max(1, max_tokens * 4)
    if len(text) <= step:
        return [text]
    return [text[i : i + step] for i in range(0, len(text), step)]
