"""Data class per task (`tasks.data_class`, research/konzept-datenzonen.md §1)

NULL inherits the project's privacy (the effective class is `COALESCE(tasks.data_class, projects.privacy)`), so
existing tasks keep exactly the class they had before; no data is changed.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("data_class", sa.Enum("cloud_ok", "local_only", "sealed", name="data_class"),
                                     nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "data_class")
