"""Preserve the data class of workflow email requests.

Revision ID: 0033
Revises: 0032
"""
import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outgoing_mails", sa.Column("data_class", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("outgoing_mails", "data_class")
