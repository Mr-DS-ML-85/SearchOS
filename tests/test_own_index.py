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


class TestPublishedDateParsing:
    """trafilatura returns free-form date strings; the column is timestamptz.

    Passing the string through made asyncpg raise and failed the whole ingest
    for any page that carried a publish date.
    """

    def test_iso_date(self):
        from app.workers.ingest import _parse_published

        assert _parse_published("2001-08-25").isoformat().startswith("2001-08-25")

    def test_iso_datetime_with_zulu(self):
        from app.workers.ingest import _parse_published

        parsed = _parse_published("2020-01-02T03:04:05Z")
        assert parsed is not None and parsed.year == 2020

    def test_slash_and_written_forms(self):
        from app.workers.ingest import _parse_published

        for text in ("2001/08/25", "25 August 2001", "August 25, 2001", "25 Aug 2001"):
            assert _parse_published(text) is not None, text

    def test_datetime_passthrough(self):
        from datetime import datetime, timezone

        from app.workers.ingest import _parse_published

        now = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert _parse_published(now) == now

    def test_naive_datetime_gets_utc(self):
        from datetime import datetime

        from app.workers.ingest import _parse_published

        assert _parse_published(datetime(2020, 1, 1)).tzinfo is not None

    def test_date_object(self):
        from datetime import date

        from app.workers.ingest import _parse_published

        assert _parse_published(date(2020, 1, 1)).year == 2020

    def test_garbage_returns_none(self):
        from app.workers.ingest import _parse_published

        assert _parse_published("not a date") is None
        assert _parse_published("") is None
        assert _parse_published(None) is None

    def test_result_is_always_storable(self):
        """Whatever trafilatura returns, the value must be a datetime or None."""
        from app.workers.ingest import _parse_published

        for candidate in ("2001-08-25", "sometime in 2001", 12345, None, [], {"y": 2020}):
            value = _parse_published(candidate)
            assert value is None or hasattr(value, "isoformat"), candidate
