from __future__ import annotations

import re


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def chunk_text(text: str, *, max_tokens: int = 512, overlap_tokens: int = 64) -> list[str]:
    if not text:
        return []
    sentences = split_sentences(text)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for sentence in sentences:
        tokens = estimate_tokens(sentence)
        if tokens > max_tokens:
            sub_parts = _split_long(sentence, max_tokens)
            for part in sub_parts:
                if current and current_tokens + estimate_tokens(part) > max_tokens:
                    chunks.append(" ".join(current))
                    current = _tail(current, overlap_tokens)
                    current_tokens = sum(estimate_tokens(s) for s in current)
                current.append(part)
                current_tokens += estimate_tokens(part)
            continue

        if current and current_tokens + tokens > max_tokens:
            chunks.append(" ".join(current))
            current = _tail(current, overlap_tokens)
            current_tokens = sum(estimate_tokens(s) for s in current)

        current.append(sentence)
        current_tokens += tokens

    if current:
        chunks.append(" ".join(current))
    return [c for c in chunks if c.strip()]


def _split_long(sentence: str, max_tokens: int) -> list[str]:
    words = sentence.split()
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for word in words:
        tokens = estimate_tokens(word)
        if current and current_tokens + tokens > max_tokens:
            parts.append(" ".join(current))
            current = []
            current_tokens = 0
        current.append(word)
        current_tokens += tokens
    if current:
        parts.append(" ".join(current))
    return parts


def _tail(sentences: list[str], overlap_tokens: int) -> list[str]:
    if not sentences:
        return []
    result: list[str] = []
    tokens = 0
    for s in reversed(sentences):
        if tokens >= overlap_tokens:
            break
        result.insert(0, s)
        tokens += estimate_tokens(s)
    return result


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 4))
