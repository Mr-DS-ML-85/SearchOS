"""Image index for multimodal search.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-10

CLIP places images and text in one vector space, so a text query can retrieve
images by meaning. That requires an image table, which nothing in SearchOS had.

``document_id`` is nullable and set with ON DELETE SET NULL: an image should
survive the page it was found on being re-crawled or deleted, since the image
URL is usually the durable identifier.

``phash`` is a 64-bit perceptual hash. It is not used for reverse image search
here — that is a separate retrieval mode — but it makes exact-duplicate images
detectable in O(1) and prevents the index filling with the same asset reached
through many pages.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(64), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("embedding", Vector(512), nullable=True),
        sa.Column("phash", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="indexed"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # One row per image URL: the same asset referenced from twenty pages is one
    # image, not twenty.
    op.create_index("ix_images_url", "images", ["url"], unique=True)
    op.create_index("ix_images_source_url", "images", ["source_url"])
    op.create_index("ix_images_document_id", "images", ["document_id"])
    op.create_index("ix_images_phash", "images", ["phash"])
    op.execute("CREATE INDEX IF NOT EXISTS ix_images_embedding ON images USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_images_embedding")
    op.drop_table("images")
