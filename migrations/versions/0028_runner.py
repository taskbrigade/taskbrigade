"""Data-zone runner (research/konzept-datenzonen.md §2/§5/§6, DZ 6): agent kind `runner`, the run fields of a task and
`run_results` (job outputs in quarantine until the output gate releases them).

- `actors.agent_kind` gains `runner` (model location `none`, no LLM).
- `tasks.run_commit_sha`/`run_script_path` (§1) name the merged script of a `run` task; `tasks.run_dataset_id` is
  its input dataset until `task_data_refs` (DZ 9) takes over the reference.
- `run_results` holds metadata only; the encrypted outputs and logs live below `TBR_DATA_DIR/quarantine`.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci"}
OLD_KINDS = ("hermes", "openclaw", "codex", "claude_code", "opencode", "other")
NEW_KINDS = OLD_KINDS + ("runner",)
RESULT_STATUSES = ("quarantine", "checks_failed", "pending_approval", "released", "rejected", "deleted")


def upgrade() -> None:
    op.alter_column("actors", "agent_kind", existing_type=sa.Enum(*OLD_KINDS, name="agent_kind"),
                    type_=sa.Enum(*NEW_KINDS, name="agent_kind"), existing_nullable=True)
    op.add_column("tasks", sa.Column("run_commit_sha", mysql.CHAR(40), nullable=True))
    op.add_column("tasks", sa.Column("run_script_path", sa.String(300), nullable=True))
    op.add_column("tasks", sa.Column("run_dataset_id", UINT, nullable=True))
    op.create_foreign_key("fk_tasks_run_dataset", "tasks", "datasets", ["run_dataset_id"], ["id"],
                          ondelete="SET NULL")
    op.create_table(
        "run_results",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("task_id", UINT, sa.ForeignKey("tasks.id", name="fk_run_results_task", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("dataset_id", UINT, sa.ForeignKey("datasets.id", name="fk_run_results_dataset",
                                                    ondelete="SET NULL")),
        sa.Column("runner_id", UINT, sa.ForeignKey("actors.id", name="fk_run_results_runner", ondelete="SET NULL")),
        sa.Column("job_id", sa.String(64), nullable=False, unique=True),
        sa.Column("task_kind", sa.Enum("profile", "run", name="run_result_kind"), nullable=False,
                  server_default="run"),
        sa.Column("script_commit_sha", mysql.CHAR(40)),
        sa.Column("script_path", sa.String(300)),
        sa.Column("started_at", sa.DateTime),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("exit_code", sa.SmallInteger),
        sa.Column("service_result", sa.String(40)),
        sa.Column("peak_memory_mb", UINT),
        sa.Column("status", sa.Enum(*RESULT_STATUSES, name="run_result_status"), nullable=False,
                  server_default="quarantine"),
        sa.Column("output_path", sa.String(500)),
        sa.Column("output_sha256", mysql.CHAR(64)),
        sa.Column("files_json", sa.Text),
        sa.Column("dek_wrapped", mysql.VARBINARY(128)),
        sa.Column("checks_json", sa.Text),
        sa.Column("released_json", mysql.MEDIUMTEXT),
        sa.Column("approvals_json", sa.Text),
        sa.Column("released_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Index("idx_run_results_task", "task_id"),
        sa.Index("idx_run_results_status", "status"),
        **OPTS,
    )


def downgrade() -> None:
    op.drop_table("run_results")
    op.drop_constraint("fk_tasks_run_dataset", "tasks", type_="foreignkey")
    op.drop_column("tasks", "run_dataset_id")
    op.drop_column("tasks", "run_script_path")
    op.drop_column("tasks", "run_commit_sha")
    op.execute("UPDATE actors SET agent_kind = 'other' WHERE agent_kind = 'runner'")
    op.alter_column("actors", "agent_kind", existing_type=sa.Enum(*NEW_KINDS, name="agent_kind"),
                    type_=sa.Enum(*OLD_KINDS, name="agent_kind"), existing_nullable=True)
