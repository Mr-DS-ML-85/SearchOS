"""Multilingual full-text search.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10

Adds a second GIN index over a ``simple``-configured tsvector.

The existing ``ix_documents_fts`` uses the ``english`` configuration, which
applies English stemming and an English stopword list. Applied to another
language that is actively destructive: Japanese text is split into runs of
ideographs that the English stemmer discards, and most non-English stopwords are
not in the list but the stemming still mangles the tokens. The result was that a
document written in any other language was effectively unsearchable by FTS.

``simple`` performs no stemming and no stopword removal, so it works for every
language. It is a lower-quality configuration for English, which is why the
existing index is kept and queried first.

Both indexes are built over an expression rather than a stored column, so this
is a metadata-only operation on PostgreSQL 11+ — the same reason
``ix_chunks_embedding`` can be created CONCURRENTLY.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain CREATE INDEX, matching 0004. CONCURRENTLY would avoid a write lock on
    # `documents`, but it cannot run inside the transaction Alembic wraps each
    # migration in, and autocommit_block() did not lift that in this
    # configuration — it failed with "CREATE INDEX CONCURRENTLY cannot run inside
    # a transaction block" and left the migration unapplied. Revisit if the index
    # ever needs to be built on a table large enough for the lock to matter.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_documents_fts_simple
        ON documents
        USING gin (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content_text, '')))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_fts_simple")
