"""ColBERT token embeddings.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-10

One vector per token rather than one per chunk. Stored as Vector(128), which is
the difference between ~0.5GB and ~11GB for this corpus — see
app/search/colbert.py for the measured comparison.

``position`` is the token index within the chunk and is part of the primary key,
so re-indexing a chunk replaces its vectors exactly instead of accumulating
duplicates.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunk_tokens",
        sa.Column("chunk_id", sa.Uuid(), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("embedding", Vector(128), nullable=False),
        sa.ForeignKey("chunks.id", ondelete="CASCADE"),
    )
    # MaxSim fetches every token of a candidate chunk at once; without this the
    # lookup degrades to a heap scan on a table holding thousands of rows per
    # chunk.
    op.create_index("ix_chunk_tokens_chunk_id", "chunk_tokens", ["chunk_id"])


def downgrade() -> None:
    op.drop_table("chunk_tokens")
