"""Topic routing: a SearXNG category per topic, mapped onto this gateway's providers.

The vocabulary is SearXNG's own ``categories_as_tabs`` set rather than an invented
one, because SearXNG is the only upstream here that serves every one of them and
already has engines wired for them. Google's "tabs" are a different taxonomy with
no overlap, and re-deriving either would have meant inventing a third.

Measured against the local instance on 2026-10-14, one query ("paxos consensus"),
one request per category, through the gateway's own SearXNG URL:

    general  27 results      music     32        files          10
    images   60              it        60        social media    8
    videos   86              science   52
    news      2

Two traps that shaped this module, both found by measuring rather than by reading
the docs:

1. **An unknown category is silently ignored, not rejected.** Asking for
   ``maps`` — which is what the topic is called everywhere else — returned 27
   results, byte-identical to ``general``: SearXNG dropped the unrecognised value
   and searched the default. A map tab wired that way would have looked healthy
   and been wrong on every query. The documented spelling is the singular ``map``,
   and this instance returns **0** for it, so no category name yields maps here.
   `Topic` therefore refuses names it cannot serve instead of degrading to general.

2. **A category can be valid and still return nothing**, because an instance only
   has engines for the categories it was configured with. ``news`` returning 2
   against 86 for ``videos`` is that, not a bug in the provider.

Provider support per topic is recorded explicitly rather than inferred, because
"which upstreams can be re-targeted" is not the same question as "which ones
support this topic". `providers` is what a topic can actually reach.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: SearXNG's category vocabulary. Names are the wire values, so `social media`
#: keeps its space and `map` keeps its singular form.
SEARXNG_CATEGORIES = (
    "general",
    "images",
    "videos",
    "news",
    "map",
    "music",
    "it",
    "science",
    "files",
    "social media",
)

#: Upstreams that *could* be re-targeted per topic, and the parameter that would
#: do it. Deliberately not wired into `Topic.providers`, which names only what
#: this codebase actually re-targets today.
#:
#: - Tavily ``topic`` is a closed enum: a request with ``topic=images`` is
#:   rejected 400 with "Must be 'general', 'news', or 'finance'". Verified live.
#:   Keyless access additionally 503s on ``news``, so that pairing needs a key.
#: - Exa ``category`` takes known values (company, publication, news, personal
#:   site, financial report, people) and accepts arbitrary strings as hints.
#:   ``publication`` is the scholarly one: papers, preprints, journal articles.
#: - Google news/videos/maps are absent entirely. They are reached with
#:   ``tbm=nws`` / ``tbm=vid`` / ``tbm=lcl``, but each vertical serves a
#:   different DOM, and `google.parse_web` only understands the web layout — so
#:   wiring them would return zero rows while reporting success.
#:
#: Listing a provider here is a note about the vendor API. It is not a claim that
#: SearchOS will serve that topic from it.
VENDOR_TOPIC_PARAMS: dict[str, dict[str, str]] = {
    "tavily": {"news": "news", "finance": "finance", "general": "general"},
    "exa": {
        "general": "",
        "news": "news",
        "science": "publication",
        "it": "github",
        "files": "pdf",
        "social media": "tweet",
    },
}


@dataclass(frozen=True)
class Topic:
    """One selectable topic."""

    id: str
    label: str
    #: Wire value for SearXNG's ``categories`` parameter.
    searxng: str
    #: Providers that can actually serve this topic, in fallback order.
    providers: tuple[str, ...]
    #: Whether the local SearXNG was measured to return results. A topic can be
    #: valid upstream and empty here; saying so beats an empty tab that looks broken.
    available_here: bool
    note: str = ""

    def describe(self) -> dict[str, Any]:
        """Catalogue entry for a GUI. Declares what is unsupported rather than
        letting an empty tab be read as an outage."""
        return {
            "id": self.id,
            "label": self.label,
            "sources": list(self.providers),
            "available": self.available_here,
            "note": self.note,
        }


TOPICS: dict[str, Topic] = {
    t.id: t
    for t in (
        Topic("general", "General", "general", ("searxng",), True),
        Topic("images", "Images", "images", ("searxng",), True, "CLIP index via /v1/images/search"),
        Topic("videos", "Videos", "videos", ("searxng",), True),
        Topic(
            "news",
            "News",
            "news",
            ("searxng",),
            True,
            "Local SearXNG returned 2 results where videos returned 86. Tavily "
            "supports topic=news but needs a key under keyless access (503).",
        ),
        Topic(
            "map",
            "Maps",
            "map",
            (),
            False,
            "No provider here serves maps. The documented SearXNG category returns "
            "0 on this instance, and 'maps' is silently treated as general.",
        ),
        Topic("music", "Music", "music", ("searxng",), True),
        Topic("it", "IT", "it", ("searxng",), True),
        Topic("science", "Science", "science", ("searxng",), True),
        Topic("files", "Files", "files", ("searxng",), True),
        Topic("social", "Social", "social media", ("searxng",), True),
    )
}

#: Wire value -> topic id, for the one place a category name has to be converted
#: back. `map` maps to "map" and "social media" to "social"; nothing else differs.
_SEARXNG_TO_ID = {t.searxng: t.id for t in TOPICS.values()}


def topic_for_searxng_category(category: str) -> str | None:
    return _SEARXNG_TO_ID.get(category)


def topic_for_searxng(topic_id: str | None) -> str | None:
    """The upstream category for a topic id, or None when there is no topic.

    The mapping is deliberately not total: an untopiced request stays None so
    each provider keeps its own default, which is why adding topics cannot change
    the behaviour of a query that does not ask for one.
    """
    if not topic_id:
        return None
    topic = resolve(topic_id)
    return topic.searxng if topic else None


def catalogue() -> list[dict[str, Any]]:
    return [t.describe() for t in TOPICS.values()]


def resolve(topic_id: str) -> Topic | None:
    return TOPICS.get((topic_id or "").strip().lower())


def supported_topics() -> list[str]:
    """Topics with at least one provider that can serve them."""
    return [t.id for t in TOPICS.values() if t.providers]


def provider_names(topic_id: str) -> tuple[str, ...]:
    """Registered provider names that serve ``topic_id``.

    Empty for an unknown topic or one no provider can serve. Callers must treat
    that as a client error, not as an empty result: a silently empty topic is
    indistinguishable from an outage, and the ``maps`` trap above is exactly that
    failure arriving in a healthy-looking response.
    """
    topic = resolve(topic_id)
    return topic.providers if topic else ()


__all__ = [
    "topic_for_searxng",
    "TOPICS",
    "Topic",
    "VENDOR_TOPIC_PARAMS",
    "SEARXNG_CATEGORIES",
    "catalogue",
    "resolve",
    "supported_topics",
    "provider_names",
    "topic_for_searxng",
]
