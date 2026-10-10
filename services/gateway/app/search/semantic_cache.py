"""Semantic cache: reuse a previous result for a *differently worded* query.

The exact-match cache only helps when a caller repeats a query byte for byte.
Agents rarely do — they rephrase. "how do i tune autovacuum" and "autovacuum
tuning guidance" want the same answer, so both pay for a full retrieval.

This embeds the incoming query and compares it against recently-answered
queries. Above a cosine threshold the stored response is reused.

Two failure modes are designed against, because both are silent:

- **False positive.** A semantically adjacent but genuinely different question
  reusing another question's answer. The threshold is deliberately high (0.95 by
  default) and both the matched query and the similarity are recorded on the
  response, so a caller can tell a reused answer from a fresh one.
- **Staleness.** Entries expire on their own TTL, independent of the exact-match
  cache the response came from.

Storage is a bounded Redis list rather than an index: the scan is over at most
``max_entries`` short vectors, which is microseconds of float arithmetic and
avoids embedding every cached query into pgvector just to serve a cache.
"""

from __future__ import annotations

import json
import time

from app.cache import get_cache
from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics

logger = get_logger("search.semantic_cache")

_LIST_KEY = "semcache:recent"
# Each entry is a JSON blob with a 384-float vector, so cap the list rather than
# let it grow without bound in a long-lived Redis.
_MAX_ENTRIES = 500


# Only consider entries answered recently; the exact-match TTL is the outer
# bound, this is the inner one.
#: Entries must not outlive the response they point at. The semantic list
#: stores a reference (the cache key), not the response itself, and the response
#: expires at `search_cache_ttl_seconds` (300s by default). With a 3600s entry
#: TTL, an entry stayed "matchable" for ~3300s after its target was gone: the
#: lookup found a strong match, failed to fetch it, and returned a miss — so the
#: cache fired intermittently and for no visible reason, depending purely on
#: whether the underlying key happened to still be warm.
_FRESH_SECONDS = 3600


def _entry_ttl_seconds() -> float:
    """Lifetime of a semantic entry, capped by the lifetime of its target."""
    from app.config import get_settings

    ttl = float(get_settings().search_cache_ttl_seconds)
    # Never exceed the target's lifetime, with a little slack so an entry is not
    # evicted microseconds before the response it refers to.
    return min(_FRESH_SECONDS, ttl + 30)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SemanticCache:
    def __init__(self, *, threshold: float | None = None, max_entries: int = _MAX_ENTRIES) -> None:
        settings = get_settings()
        self.threshold = threshold if threshold is not None else settings.semantic_cache_threshold
        self.max_entries = max_entries
        self.enabled = settings.semantic_cache_enabled

    async def _embedding(self, query: str) -> list[float] | None:
        from app.embed.provider import get_embedding_provider

        provider = get_embedding_provider()
        if not provider.available:
            return None
        try:
            return await provider.embed_one(query)
        except Exception:
            return None

    async def lookup(
        self,
        query: str,
        sources: str = "all",
        embedding: list[float] | None = None,
    ) -> tuple[dict | None, str | None, float]:
        """Return ``(cached_response, matched_query, similarity)`` or Nones.

        ``sources`` is part of the identity of an answer, not an afterthought.
        An entry recorded for ``sources=all`` must never satisfy a request for
        ``sources=federated``: the two ask different questions of the system and
        return different sets. Without this the cache silently answered a
        pinned-provider query with a whole-corpus result.
        """
        if not self.enabled or not query.strip():
            return None, None, 0.0
        if embedding is None:
            embedding = await self._embedding(query)
        if not embedding:
            return None, None, 0.0

        cache = get_cache()
        try:
            raw = await cache.lrange(_LIST_KEY, 0, self.max_entries - 1)
        except Exception:
            return None, None, 0.0
        if not raw:
            return None, None, 0.0

        now = time.time()
        # Rank every candidate first rather than picking a winner immediately.
        # A stale entry — one whose response key has since been flushed or
        # expired — used to shadow a perfectly good match: it would score higher
        # (often 1.0 for the query's own text), fail to load, and turn the whole
        # lookup into a miss. That is what made the cache fire intermittently.
        candidates: list[tuple[float, str | None, str]] = []
        best_any_query: str | None = None
        best_any_score = 0.0
        for blob in raw:
            try:
                entry = json.loads(blob)
            except (TypeError, ValueError):
                continue
            if now - float(entry.get("t", 0)) > _entry_ttl_seconds():
                # Stale: its target response is almost certainly gone too.
                continue
            if entry.get("s") != sources:
                continue
            score = cosine(embedding, entry.get("e") or [])
            # Track the best overall as well as the usable ones. Reporting only
            # candidates that already clear the threshold means a near-miss
            # below it is discarded, which is exactly the case someone tuning the
            # threshold needs to see.
            if score > best_any_score:
                best_any_score = score
                best_any_query = entry.get("q")
            if score >= self.threshold:
                candidates.append((score, entry.get("q"), entry.get("k")))

        candidates.sort(key=lambda item: item[0], reverse=True)

        if not candidates:
            # Nothing usable. Report how close the nearest entry came so the
            # caller can tell a near-miss (0.89 against 0.92) from a different
            # question entirely (0.54).
            return None, best_any_query, best_any_score

        # Walk down the ranked list until one entry's response is still cached.
        for score, matched_query, key in candidates:
            stored = await cache.get(key)
            if stored:
                stored["semantic_cache_hit"] = True
                stored["semantic_cache_match"] = matched_query
                stored["semantic_cache_similarity"] = round(score, 4)
                get_metrics().semantic_cache_hits.inc()
                return stored, matched_query, score
        # Every match above threshold pointed at an evicted response.
        return None, candidates[0][1] if candidates else None, (candidates[0][0] if candidates else 0.0)

    async def remember(
        self,
        query: str,
        cache_key: str,
        sources: str = "all",
        embedding: list[float] | None = None,
    ) -> None:
        if not self.enabled or not query.strip():
            return
        if embedding is None:
            embedding = await self._embedding(query)
        if not embedding:
            return
        cache = get_cache()
        entry = json.dumps({"q": query[:200], "k": cache_key, "e": embedding, "t": time.time(), "s": sources})
        try:
            await cache.rpush(_LIST_KEY, entry)
            await cache.ltrim(_LIST_KEY, -self.max_entries, -1)
            await cache.expire(_LIST_KEY, int(_entry_ttl_seconds()))
        except Exception:
            pass
