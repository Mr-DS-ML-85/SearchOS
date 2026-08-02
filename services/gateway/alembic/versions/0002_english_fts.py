"""english FTS config + OR fallback support

Revision ID: 0002
Revises: 0001
Create Date: 2026-01-02

Rebuilds the documents FTS index on the ``english`` config so queries get
stemming (e.g. ``gpus`` matches ``gpu``). Fresh installs get this via 0001.

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_fts")
    op.execute(
        """
        CREATE INDEX ix_documents_fts
        ON documents USING gin (
            to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content_text, ''))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_fts")
    op.execute(
        """
        CREATE INDEX ix_documents_fts
        ON documents USING gin (
            to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content_text, ''))
        )
        """
    )
