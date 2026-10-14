"""Sprints: optional level between project and task (`sprints`, `tasks.sprint_id`)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "sprints",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("goal", sa.Text),
        sa.Column("start_date", sa.Date),
        sa.Column("end_date", sa.Date),
        sa.Column("status", sa.Enum("planned", "released", "closed", name="sprint_status"), nullable=False,
                  server_default="planned"),
        sa.Column("created_by", UINT, nullable=False),
        sa.Column("released_at", sa.DateTime),
        sa.Column("closed_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "name", name="uq_sprints_project_name"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_sprints_project"),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_sprints_created_by"),
        mariadb_engine="InnoDB", mariadb_charset="utf8mb4", mariadb_collate="utf8mb4_unicode_ci",
    )
    # Existing tasks keep sprint_id NULL and behave exactly as before.
    op.add_column("tasks", sa.Column("sprint_id", UINT, nullable=True))
    op.create_foreign_key("fk_tasks_sprint", "tasks", "sprints", ["sprint_id"], ["id"])
    op.create_index("idx_tasks_sprint", "tasks", ["sprint_id", "status"])


def downgrade() -> None:
    op.drop_constraint("fk_tasks_sprint", "tasks", type_="foreignkey")
    op.drop_index("idx_tasks_sprint", "tasks")
    op.drop_column("tasks", "sprint_id")
    op.drop_table("sprints")
