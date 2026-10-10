"""Backoff gate for ingest jobs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-01-03

Adds ``ingest_jobs.next_attempt_at`` so a requeued job is not re-claimed until
its backoff expires. Previously a failed job returned to ``pending`` with its
original ``created_at`` and was immediately re-claimed as the oldest row, so all
three attempts burned within milliseconds.

Also indexes ``ingest_jobs.next_attempt_at`` (the claim query's new predicate),
``ingest_jobs.created_at`` (ORDER BY) and ``usage_logs.created_at`` (retention
scans). All three are metadata-only on PostgreSQL 11+.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_jobs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ingest_jobs_next_attempt_at ON ingest_jobs (next_attempt_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ingest_jobs_created_at ON ingest_jobs (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_usage_logs_created_at ON usage_logs (created_at)")
    # The recrawler scans by (status, fetched_at) and the local provider filters
    # on status; without these both are sequential scans on a large corpus.
    op.execute("CREATE INDEX IF NOT EXISTS ix_documents_fetched_at ON documents (fetched_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_status")
    op.execute("DROP INDEX IF EXISTS ix_documents_fetched_at")
    op.execute("DROP INDEX IF EXISTS ix_usage_logs_created_at")
    op.execute("DROP INDEX IF EXISTS ix_ingest_jobs_created_at")
    op.execute("DROP INDEX IF EXISTS ix_ingest_jobs_next_attempt_at")
    op.drop_column("ingest_jobs", "next_attempt_at")
