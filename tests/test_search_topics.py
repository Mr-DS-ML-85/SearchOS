"""Tests for topic routing.

The load-bearing test here is ``test_alphabetic_category_is_not_silently_general``:
an unrecognised SearXNG category is ignored rather than rejected, so the failure
mode is a healthy-looking response full of the wrong rows.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.search import topics as T


def _topic_ids_in_schema() -> set[str]:
    """Topic ids the SearchSource Literal accepts, read from the source text."""
    # Repo-root relative, not cwd-relative: pytest may be invoked from
    # services/gateway, where a bare relative path resolves to nothing.
    src = Path(__file__).resolve().parents[1] / "services/gateway/app/schemas.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        # `SearchSource = Literal[...]` is a value assignment, not an annotated one.
        if not (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SearchSource"):
            continue
        call = node.value
        if isinstance(call, ast.Subscript):
            call = call.slice
        return {
            el.value
            for el in call.elts
            if isinstance(el, ast.Constant) and str(el.value).startswith("topic:")
        }
    raise AssertionError("SearchSource literal not found")


def test_every_topic_has_a_wire_category():
    for t in T.TOPICS.values():
        assert t.searxng in T.SEARXNG_CATEGORIES, t.id


def test_unavailable_topics_have_no_providers():
    """A topic that cannot be served must not name providers.

    Naming one produces an empty tab that reads as an outage rather than as an
    unsupported feature.
    """
    for t in T.TOPICS.values():
        if not t.available_here:
            assert t.providers == (), t.id
            assert t.note, f"{t.id} unavailable and unexplained"


def test_available_topics_have_providers():
    for t in T.TOPICS.values():
        if t.available_here:
            assert t.providers, t.id


def test_supported_topics_excludes_unserved():
    assert "map" not in T.supported_topics()
    assert "news" in T.supported_topics()


def test_alphabetic_category_is_not_silently_general():
    """`maps` is a category SearXNG does not know.

    Measured on the local instance: `maps` returned 27 results, identical to
    `general`, because SearXNG drops unrecognised categories instead of erroring.
    The catalogue must therefore spell it `map` (singular), and must never offer
    a `maps` alias — that is the spelling that silently returns general results.
    """
    assert "maps" not in {t.searxng for t in T.TOPICS.values()}
    assert "maps" not in T.TOPICS
    # The documented spelling is preserved, even though this instance has no
    # engines for it and returns 0.
    assert T.TOPICS["map"].searxng == "map"


def test_social_topic_maps_to_space_separated_wire_value():
    assert T.TOPICS["social"].searxng == "social media"


def test_resolve_is_case_and_space_insensitive():
    assert T.resolve("  News  ") is T.TOPICS["news"]
    assert T.resolve("NEWS") is T.TOPICS["news"]


def test_resolve_rejects_unknown():
    assert T.resolve("vibes") is None
    assert T.resolve("") is None


def test_provider_names_for_servable_topics():
    assert T.provider_names("news") == ("searxng",)
    assert T.provider_names("videos") == ("searxng",)


def test_provider_names_empty_for_unserved_and_unknown():
    """An unserved topic must resolve to no providers, so the router errors
    loudly instead of returning an empty page that reads as an outage."""
    assert T.provider_names("map") == ()
    assert T.provider_names("nonsense") == ()


def test_vendor_params_are_documentation_not_a_routing_claim():
    """VENDOR_TOPIC_PARAMS records what each vendor API could do.

    No topic may list a provider it is not actually re-targeted on: naming one
    would produce a topic tab whose rows came from an unconstrained general
    search, which is the same defect as the `maps` fallback.
    """
    wired = {name for t in T.TOPICS.values() for name in t.providers}
    assert wired == {"searxng"}
    assert "tavily" in T.VENDOR_TOPIC_PARAMS and "exa" in T.VENDOR_TOPIC_PARAMS


def test_catalogue_shape_is_gui_ready():
    for entry in T.catalogue():
        assert set(entry) == {"id", "label", "sources", "available", "note"}
        assert isinstance(entry["sources"], list)


@pytest.mark.parametrize("topic_id", sorted(T.TOPICS))
def test_catalogue_is_json_serialisable(topic_id):
    import json

    json.dumps(T.catalogue())


def test_provider_query_carries_topic_field():
    from app.search.base import ProviderQuery

    assert ProviderQuery(q="x").topic is None
    assert ProviderQuery(q="x", topic="news").topic == "news"

def test_topic_for_searxng_maps_ids_to_wire_categories():
    assert T.topic_for_searxng("news") == "news"
    assert T.topic_for_searxng("social") == "social media"
    assert T.topic_for_searxng("map") == "map"


def test_topic_for_searxng_is_none_without_a_topic():
    """Existing queries must be untouched by topic support."""
    for value in (None, "", "nonsense"):
        assert T.topic_for_searxng(value) is None


def test_search_source_enum_is_unchanged_by_topics():
    """Topics are a request field, not sources.

    They were briefly added to the enum, which broke the MCP/gateway parity test.
    `sources` means *provider*; a topic constrains which category a provider
    searches. Keeping them apart lets that test pass without editing the MCP tool.
    """
    import typing

    from app.schemas import SearchSource

    values = typing.get_args(SearchSource)
    assert not [v for v in values if str(v).startswith("topic:")]
    assert "all" in values and "searxng" in values


def test_topic_is_part_of_the_exact_cache_key():
    """Regression guard for a bug this feature introduced.

    `cache_key()` is called with `req.sources` and never sees `filter_identity`,
    so a topic that was added only to the filter identity still let an untopiced
    query read the News page back out of cache: two tabs, byte-identical
    results. Asserted against the source because the failure is invisible to a
    unit test of any single function — it only shows up in the assembled key.
    """
    src = Path(__file__).resolve().parents[1] / "services/gateway/app/search/orchestrator.py"
    text = src.read_text()
    assert 'f"topic={req.topic or \'\'}"' in text, "topic missing from the exact cache key"


def test_topic_is_part_of_the_filter_identity():
    src = Path(__file__).resolve().parents[1] / "services/gateway/app/search/orchestrator.py"
    text = src.read_text()
    identity = text[text.index("filter_identity = ["): text.index("cursor_fingerprint")]
    assert 'req.topic or ""' in identity, "topic missing from the semantic/cursor scope"


def test_search_request_accepts_a_topic():
    from app.schemas import SearchRequest

    assert SearchRequest(q="x").topic is None
    assert SearchRequest(q="x", topic="news").topic == "news"


def test_topic_and_sources_are_independent_fields():
    """A topic narrows the category; `sources` still names the provider.

    Conflating them (encoding the topic as a pseudo-source) is what broke the
    MCP parity test, so both are asserted to exist separately.
    """
    from app.schemas import SearchRequest

    req = SearchRequest(q="x", topic="videos", sources="searxng")
    assert req.topic == "videos"
    assert req.sources == "searxng"
