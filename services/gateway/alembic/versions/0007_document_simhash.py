"""Add SimHash fingerprints for near-duplicate detection.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-10

`documents.normalized_url` only collapses ``http``/``https`` and ``www.``
variants of the *same* address. The same article syndicated to five URLs, or a
documentation page mirrored across four sites, produced five results and five
sets of chunks because nothing compared their content.

`simhash` holds a 64-bit content fingerprint. A bigint is exactly 64 signed
bits, so the value round-trips without a custom Postgres type. Rows are NULL
until written; `python -m app.backfill_simhash` fills the existing corpus.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("simhash", sa.BigInteger(), nullable=True))
    # Partial index: only documents that have a fingerprint take part, which
    # keeps the index small while rows are still being backfilled.
    op.execute("CREATE INDEX IF NOT EXISTS ix_documents_simhash ON documents (simhash) WHERE simhash IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_simhash")
    op.drop_column("documents", "simhash")
