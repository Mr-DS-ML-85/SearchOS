"""crawler depth for nested link following

Revision ID: 0003
Revises: 0002
Create Date: 2026-01-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ingest_jobs", sa.Column("depth", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("ingest_jobs", "depth")
