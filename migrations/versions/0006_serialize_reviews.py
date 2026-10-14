"""Project option serialize_reviews: no new claim while the agent's task in the project awaits review

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("serialize_reviews", sa.Boolean, nullable=False,
                                        server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("projects", "serialize_reviews")
