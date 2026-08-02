from __future__ import annotations

from app.embed.chunker import chunk_text, estimate_tokens, split_sentences


def test_split_sentences():
    text = "First sentence. Second sentence! Third?\n\nFourth."
    parts = split_sentences(text)
    assert len(parts) == 4


def test_chunk_text_respects_max_tokens():
    long_text = " ".join(f"Sentence number {i} about search engines and retrieval." for i in range(200))
    chunks = chunk_text(long_text, max_tokens=100, overlap_tokens=10)
    assert len(chunks) > 1
    for c in chunks:
        assert estimate_tokens(c) <= 200


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_text_small_input():
    chunks = chunk_text("Just a tiny input.")
    assert len(chunks) == 1
    assert chunks[0] == "Just a tiny input."


def test_overlap_preserves_context():
    long_text = " ".join(f"Word{i}" for i in range(300))
    chunks = chunk_text(long_text, max_tokens=100, overlap_tokens=20)
    assert len(chunks) >= 2
    assert chunks[-1]
