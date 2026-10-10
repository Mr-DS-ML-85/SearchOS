"""english FTS config + OR fallback support

Revision ID: 0002
Revises: 0001
Create Date: 2026-01-02

Rebuilds the documents FTS index on the ``english`` config so queries get
stemming (e.g. ``gpus`` matches ``gpu``). Fresh installs get this via 0001.

Both directions now use CREATE/DROP INDEX CONCURRENTLY. The previous version ran
a plain ``DROP INDEX`` (ACCESS EXCLUSIVE on the table) followed by a plain
``CREATE INDEX`` (SHARE lock held for the whole build), which meant any deploy
against a populated ``documents`` table was a full read/write outage for the
duration of the GIN rebuild.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_SQL_ENGLISH = """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_fts
    ON documents USING gin (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content_text, ''))
    )
"""

_INDEX_SQL_SIMPLE = """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_fts
    ON documents USING gin (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content_text, ''))
    )
"""


def _concurrently(sql: str) -> None:
    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(sql)


def upgrade() -> None:
    # Drop and rebuild so a stale 'simple'-config index is replaced even when
    # the definition looks identical to 0001's.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_documents_fts")
    _concurrently(_INDEX_SQL_ENGLISH)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_documents_fts")
    _concurrently(_INDEX_SQL_SIMPLE)
