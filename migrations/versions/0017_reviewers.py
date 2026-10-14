"""Reviewer agents: role 'reviewer', reviewer per task with its own review lease, default reviewer per project

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OLD = "'admin','orchestrator','worker','member'"
NEW = "'admin','orchestrator','worker','member','reviewer'"


def upgrade() -> None:
    op.execute(f"ALTER TABLE actors MODIFY role ENUM({NEW}) NOT NULL DEFAULT 'worker'")
    op.add_column("tasks", sa.Column("reviewer_id", UINT, nullable=True))
    op.add_column("tasks", sa.Column("review_claimed_at", sa.DateTime(), nullable=True))
    op.add_column("tasks", sa.Column("review_lease_expires_at", sa.DateTime(), nullable=True))
    op.create_foreign_key("fk_tasks_reviewer", "tasks", "actors", ["reviewer_id"], ["id"])
    op.create_index("idx_tasks_review", "tasks", ["reviewer_id", "status"])
    op.add_column("projects", sa.Column("default_reviewer_id", UINT, nullable=True))
    op.create_foreign_key("fk_projects_default_reviewer", "projects", "actors", ["default_reviewer_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_projects_default_reviewer", "projects", type_="foreignkey")
    op.drop_column("projects", "default_reviewer_id")
    op.drop_constraint("fk_tasks_reviewer", "tasks", type_="foreignkey")
    op.drop_index("idx_tasks_review", "tasks")
    op.drop_column("tasks", "review_lease_expires_at")
    op.drop_column("tasks", "review_claimed_at")
    op.drop_column("tasks", "reviewer_id")
    op.execute("UPDATE actors SET role = 'worker' WHERE role = 'reviewer'")
    op.execute(f"ALTER TABLE actors MODIFY role ENUM({OLD}) NOT NULL DEFAULT 'worker'")
