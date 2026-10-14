"""Profile switch "Show all projects": per-human display preference, off for everyone

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Off for existing and new humans (admins too): lists show only projects the human owns.
    op.add_column("actors", sa.Column("show_all_projects", sa.Boolean, nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("actors", "show_all_projects")
