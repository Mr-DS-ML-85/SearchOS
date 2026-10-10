"""Recompute normalized_url for documents whose URL has a query string.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-11

``normalize_url`` used to drop the query string, so ``articleView?id=1`` and
``articleView?id=2`` shared one key and the second overwrote the first. It now
keeps identifying parameters (dropping only tracking ones). Rows written under
the old rule are re-keyed here.

Because an overwrite replaced content but kept the first row's ``url``, a
re-keyed row's content may belong to a different page than its ``url``. Each
re-keyed row is therefore marked stale (``fetched_at`` set to the epoch, not
NULL: the recrawler selects ``fetched_at < cutoff``, which NULL never satisfies)
so it is fetched again, ahead of everything else. A row whose new key is already taken is a genuine duplicate
and is removed; its chunks cascade.

The normalisation is copied rather than imported: a migration must keep
producing the same result even if the application function changes later.
"""

from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRACKING = {"gclid", "dclid", "fbclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "ref_src"}


def _normalize(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = (parsed.path or "").rstrip("/")
    params = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    )
    key = f"{scheme}://{host}{path}"
    return key + ("?" + urlencode(params) if params else "")


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, url, normalized_url FROM documents WHERE url LIKE '%?%'")).all()
    taken = {r[0] for r in bind.execute(sa.text("SELECT normalized_url FROM documents")).all()}
    for doc_id, url, old_key in rows:
        new_key = _normalize(url)
        if new_key == old_key:
            continue
        if new_key in taken:
            bind.execute(sa.text("DELETE FROM documents WHERE id = :id"), {"id": doc_id})
            continue
        bind.execute(
            sa.text(
                "UPDATE documents SET normalized_url = :key, fetched_at = TIMESTAMPTZ '1970-01-01 00:00:00+00' WHERE id = :id"
            ),
            {"key": new_key, "id": doc_id},
        )
        taken.discard(old_key)
        taken.add(new_key)


def downgrade() -> None:
    # The old keys cannot be restored without re-merging distinct pages, which is
    # the bug this revision fixes.
    pass
