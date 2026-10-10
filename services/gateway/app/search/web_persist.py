"""Save every live-web provider's results into the index, deduplicated.

Only SearXNG used to do this, inside its own provider, so a query answered by
Google, Tavily, Exa or Jina taught the index nothing until snippet enrichment
happened to reach its top URLs. Persistence now runs once in the orchestrator
for whichever web provider answered.

Dedupe, in order:

1. **Within the batch**, by ``normalize_url``. Postgres rejects an
   ``INSERT ... ON CONFLICT DO UPDATE`` that touches the same row twice, so one
   duplicate in a batch used to fail the whole write.
2. **Against the index**, by the unique ``normalized_url``. An existing row is
   only updated while it is still a snippet stub (``searxng_snippet`` in
   ``meta``); a crawled or ingested page is never overwritten by a snippet.
   Between two snippets the longer one is kept, so a richer upstream's text is
   not replaced by a terser one.

Near-duplicate *content* (the same article at two URLs) is collapsed at query
time by SimHash once the page is enriched; snippets are too short for SimHash
to be meaningful.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import text

from app.logging import get_logger
from app.metrics import get_metrics

logger = get_logger("search.web_persist")

#: Providers whose results are live-web results. ``web`` is the cascade; the
#: others are its tiers when pinned. ``federated`` is deliberately absent: peer
#: results come from another index, not the web, and that index owns them.
WEB_PROVIDERS = frozenset({"web", "searxng", "google", "tavily", "exa", "jina"})

#: Snippets are stored truncated to this many characters. The backfill uses it
#: to tell a stub from a page that was enriched in place.
SNIPPET_MAX_CHARS = 300

_UPSERT = text(
    """
    INSERT INTO documents (id, url, normalized_url, title, content_text, source,
                           published_at, fetched_at, status, meta)
    VALUES (:id, :url, :norm, :title, :content, 'web', :published, now(), 'indexed', CAST(:meta AS jsonb))
    ON CONFLICT (normalized_url) DO UPDATE SET
        title = EXCLUDED.title,
        content_text = CASE
            WHEN length(coalesce(EXCLUDED.content_text, '')) >= length(coalesce(documents.content_text, ''))
            THEN EXCLUDED.content_text
            ELSE documents.content_text
        END,
        published_at = coalesce(EXCLUDED.published_at, documents.published_at),
        fetched_at = now(),
        meta = EXCLUDED.meta
    WHERE documents.meta->>'searxng_snippet' = 'true'
    RETURNING (xmax = 0) AS inserted
    """
)


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_rows(results: list) -> tuple[list[dict], int]:
    """Rows to upsert, deduplicated by normalized URL; also the duplicate count."""
    from app.search.providers.searxng import SNIPPET_META_FLAG
    from app.workers.ingest import normalize_url

    rows: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    for r in results:
        url = (getattr(r, "url", "") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        norm = normalize_url(url)
        if norm in seen:
            duplicates += 1
            continue
        seen.add(norm)
        raw = getattr(r, "raw", None) or {}
        meta = {SNIPPET_META_FLAG: True, "provider": r.source}
        if isinstance(raw, dict) and raw.get("engine"):
            meta["engine"] = raw["engine"]
        rows.append(
            {
                "id": uuid.uuid4(),
                "url": url,
                "norm": norm,
                "title": (r.title or url)[:1000],
                "content": (r.snippet or "")[:SNIPPET_MAX_CHARS],
                "published": _parse_published(r.published),
                "meta": json.dumps(meta),
            }
        )
    return rows, duplicates


async def persist_web_results(session_factory, results: list) -> dict[str, int]:
    """Upsert live-web results as snippet documents. Returns counts."""
    rows, duplicates = build_rows(results)
    counts = {"inserted": 0, "updated": 0, "kept_existing": 0, "batch_duplicates": duplicates}
    if not rows:
        return counts
    async with session_factory() as session:
        for row in rows:
            outcome = (await session.execute(_UPSERT, row)).first()
            if outcome is None:
                # Conflict with a page that is no longer a snippet: left alone.
                counts["kept_existing"] += 1
            elif outcome[0]:
                counts["inserted"] += 1
            else:
                counts["updated"] += 1
        await session.commit()
    if counts["inserted"]:
        get_metrics().documents_indexed.inc(counts["inserted"])
    return counts
