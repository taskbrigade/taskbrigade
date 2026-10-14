"""Sprint editor: display order of a sprint's tasks (`tasks.sprint_rank`)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Position in the sprint editor's list; NULL = not ordered yet (sorted after ordered tasks by prio, id).
    op.add_column("tasks", sa.Column("sprint_rank", mysql.SMALLINT(unsigned=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "sprint_rank")
