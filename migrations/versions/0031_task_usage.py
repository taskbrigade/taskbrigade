"""Usage tracking for tasks and workflow-run budgets (research/konzept-workflows.md §1.5/§7.4).

`task_usage` logs one row per usage report (tokens/cost/model/source). The workflow run's `used_tokens`/
`used_cents` counters are incremented on each report; budget enforcement reads them.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci", "mariadb_engine": "InnoDB"}


def upgrade() -> None:
    op.create_table(
        "task_usage",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("task_id", UINT, nullable=False),
        sa.Column("actor_id", UINT, nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("input_tokens", UINT, nullable=False, server_default="0"),
        sa.Column("output_tokens", UINT, nullable=False, server_default="0"),
        sa.Column("cost_cents", UINT, nullable=True),
        sa.Column("source", sa.Enum("agent", "runner_estimate", "manual", name="task_usage_source"),
                  nullable=False, server_default="agent"),
        sa.Column("reported_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_task_usage_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_task_usage_actor"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_task_usage_task", "task_id", "reported_at"),
        **OPTS,
    )


def downgrade() -> None:
    op.drop_table("task_usage")
