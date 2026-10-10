"""Add a per-language tsvector to documents.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10

`ix_documents_fts` and `ix_documents_fts_simple` are expression indexes, so both
stem the entire table with a single regconfig. A document written in French was
stemmed with English rules and had its French stopwords removed.

`to_tsvector(regconfig, text)` is IMMUTABLE only when the config is a literal,
so a generated column cannot pick a per-row language. The vector therefore has
to be computed in the application and stored, which is what this column is.
Existing rows start NULL and are repaired by `python -m app.backfill_language`;
the query path falls back to the expression indexes for those rows so they stay
searchable in the meantime.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("tsv", TSVECTOR(), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_documents_tsv ON documents USING gin (tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_tsv")
    op.drop_column("documents", "tsv")
