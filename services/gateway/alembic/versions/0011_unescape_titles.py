"""Decode HTML entities left in stored titles and snippets.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-11

Titles were stored as upstreams and extractors returned them, so 41 rows read
"Parameters &amp; Best Practices". New writes are decoded (orchestrator for live
web results, index_document for crawled pages); this repairs the rows already
written. Only rows containing an entity are touched.
"""

import html
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENTITY = r"&(amp|lt|gt|quot|apos|nbsp|#[0-9]+|#x[0-9a-fA-F]+);"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, title, content_text, meta->>'searxng_snippet' FROM documents "
            "WHERE title ~ :p OR (meta->>'searxng_snippet' = 'true' AND content_text ~ :p)"
        ),
        {"p": _ENTITY},
    ).all()
    for doc_id, title, content, is_snippet in rows:
        new_title = html.unescape(title) if title else title
        # Only snippets: a crawled page's body is extractor output and may
        # legitimately discuss entities (an HTML tutorial).
        new_content = html.unescape(content) if (content and is_snippet == "true") else content
        if new_title != title or new_content != content:
            bind.execute(
                sa.text("UPDATE documents SET title = :t, content_text = :c WHERE id = :id"),
                {"t": new_title, "c": new_content, "id": doc_id},
            )


def downgrade() -> None:
    pass
