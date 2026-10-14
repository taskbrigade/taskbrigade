"""Conscious waiting for long background runs (`tbr heartbeat --waiting`)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("waiting_reason", sa.String(300), nullable=True))
    op.add_column("tasks", sa.Column("waiting_until", sa.DateTime, nullable=True))
    op.add_column("tasks", sa.Column("waiting_file", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "waiting_file")
    op.drop_column("tasks", "waiting_until")
    op.drop_column("tasks", "waiting_reason")
