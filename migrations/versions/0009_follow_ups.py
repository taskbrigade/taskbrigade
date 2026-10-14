"""Parallel-task default 2, follow-up tasks (`tasks.follow_up_of_id`) and open points (`tasks.open_points`)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
UTINY = mysql.TINYINT(unsigned=True)


def upgrade() -> None:
    op.alter_column("actors", "max_parallel_tasks", existing_type=UTINY, existing_nullable=False,
                    server_default="2")
    # Agents still on the old default 1 get the new default; deliberately raised limits stay.
    op.execute("UPDATE actors SET max_parallel_tasks = 2 WHERE actor_type = 'agent' AND max_parallel_tasks = 1")
    op.add_column("tasks", sa.Column("follow_up_of_id", UINT, nullable=True))
    op.create_foreign_key("fk_tasks_follow_up", "tasks", "tasks", ["follow_up_of_id"], ["id"])
    # JSON list of strings, set by submit (open points the agent names separately from the summary).
    op.add_column("tasks", sa.Column("open_points", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "open_points")
    op.drop_constraint("fk_tasks_follow_up", "tasks", type_="foreignkey")
    op.drop_column("tasks", "follow_up_of_id")
    op.alter_column("actors", "max_parallel_tasks", existing_type=UTINY, existing_nullable=False,
                    server_default="1")
