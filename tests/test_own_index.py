from __future__ import annotations

import pytest

from app.extract.render import should_render
from app.workers.ingest import normalize_url


def test_normalize_url_strips_query_and_fragment():
    url = "https://Example.com/path/?utm_source=x#section"
    assert normalize_url(url) == "https://example.com/path"


def test_normalize_url_keeps_trailing_slash_path():
    assert normalize_url("http://example.com") == "http://example.com"
    assert normalize_url("https://example.com/") == "https://example.com"


def test_normalize_url_defaults_scheme():
    assert normalize_url("example.com/docs") == "http://example.com/docs"


def test_should_render_skips_binary_and_plain():
    assert should_render("https://example.com") is True
    assert should_render("https://example.com/app/") is True
    assert should_render("https://example.com/file.pdf") is False
    assert should_render("https://example.com/data.json") is False
    assert should_render("https://example.com/plain.txt") is False


def test_embedding_provider_singleton_is_stable():
    from app.embed.provider import EmbeddingProvider, get_embedding_provider

    assert get_embedding_provider() is get_embedding_provider()
    assert isinstance(get_embedding_provider(), EmbeddingProvider)


def test_vector_weight_applied():
    from app.config import get_settings
    from app.search.vector_index import VectorIndexProvider

    settings = get_settings()
    weight = settings.local_index_vector_weight
    assert 0.0 < weight <= 1.0
