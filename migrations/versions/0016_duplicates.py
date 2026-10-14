"""Duplicated tasks: origin link `tasks.duplicate_of_id`

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)


def upgrade() -> None:
    op.add_column("tasks", sa.Column("duplicate_of_id", UINT, nullable=True))
    op.create_foreign_key("fk_tasks_duplicate", "tasks", "tasks", ["duplicate_of_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_tasks_duplicate", "tasks", type_="foreignkey")
    op.drop_column("tasks", "duplicate_of_id")
