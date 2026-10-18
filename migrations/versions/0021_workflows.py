"""Stage-1 workflow templates, versions, steps, runs and task links.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)


def upgrade() -> None:
    op.create_table(
        "workflow_templates",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("workflow_key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", UINT, nullable=False),
        sa.Column("current_version_id", UINT, nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "paused", "archived", name="workflow_template_status"),
            server_default="active",
            nullable=False,
        ),
        sa.Column("default_budget_tokens", UINT, nullable=True),
        sa.Column("default_budget_cents", UINT, nullable=True),
        sa.Column("created_by", UINT, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_wft_creator"),
        sa.ForeignKeyConstraint(["owner_id"], ["actors.id"], name="fk_wft_owner"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_wft_project"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "workflow_key", name="uq_workflow_templates_key"),
        mariadb_charset="utf8mb4",
        mariadb_collate="utf8mb4_unicode_ci",
        mariadb_engine="InnoDB",
    )
    op.create_table(
        "workflow_template_versions",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("template_id", UINT, nullable=False),
        sa.Column("version", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column(
            "state",
            sa.Enum("draft", "published", "retired", name="workflow_version_state"),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("definition_sha256", mysql.CHAR(64), nullable=True),
        sa.Column("change_note", sa.String(length=300), nullable=True),
        sa.Column("created_by", UINT, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("published_by", UINT, nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_wfv_creator"),
        sa.ForeignKeyConstraint(["published_by"], ["actors.id"], name="fk_wfv_publisher"),
        sa.ForeignKeyConstraint(["template_id"], ["workflow_templates.id"], name="fk_wfv_template", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "version", name="uq_workflow_versions_number"),
        mariadb_charset="utf8mb4",
        mariadb_collate="utf8mb4_unicode_ci",
        mariadb_engine="InnoDB",
    )
    op.create_table(
        "workflow_steps",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("version_id", UINT, nullable=False),
        sa.Column("position", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("step_key", sa.String(length=40), nullable=False),
        sa.Column(
            "step_type",
            sa.Enum("agent", "script", "runner", "approval", "gate", "notify", name="workflow_step_type"),
            nullable=False,
        ),
        sa.Column("title_tpl", sa.String(length=200), nullable=False),
        sa.Column("description_tpl", mysql.MEDIUMTEXT(), nullable=True),
        sa.Column("acceptance_tpl", sa.Text(), nullable=True),
        sa.Column("assignee_id", UINT, nullable=True),
        sa.Column("agent_role", sa.String(length=40), nullable=True),
        sa.Column("data_class", sa.Enum("cloud_ok", "local_only", "sealed", name="workflow_data_class"), nullable=True),
        sa.Column(
            "review_mode",
            sa.Enum("manual", "agent", "auto", name="workflow_review_mode"),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("reviewer_id", UINT, nullable=True),
        sa.Column("model_tier", sa.Enum("simple", "standard", "complex", name="model_tier"), nullable=True),
        sa.Column("timeout_minutes", UINT, nullable=True),
        sa.Column("wait_reminder_hours", mysql.SMALLINT(unsigned=True), nullable=True),
        sa.Column("max_retries", mysql.TINYINT(unsigned=True), server_default="0", nullable=False),
        sa.Column("retry_backoff_s", UINT, server_default="300", nullable=False),
        sa.Column("optional", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("inputs_json", sa.Text(), nullable=True),
        sa.Column("outputs_json", sa.Text(), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["assignee_id"], ["actors.id"], name="fk_wfs_assignee"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["actors.id"], name="fk_wfs_reviewer"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["workflow_template_versions.id"], name="fk_wfs_version", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "position", name="uq_workflow_steps_position"),
        sa.UniqueConstraint("version_id", "step_key", name="uq_workflow_steps_key"),
        mariadb_charset="utf8mb4",
        mariadb_collate="utf8mb4_unicode_ci",
        mariadb_engine="InnoDB",
    )
    op.create_table(
        "workflow_params",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("version_id", UINT, nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column(
            "param_type",
            sa.Enum(
                "string",
                "text",
                "int",
                "decimal",
                "bool",
                "date",
                "month",
                "enum",
                "dataset",
                "task",
                name="workflow_param_type",
            ),
            nullable=False,
        ),
        sa.Column("required", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("default_expr", sa.String(length=200), nullable=True),
        sa.Column("enum_values", sa.Text(), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("help", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(
            ["version_id"], ["workflow_template_versions.id"], name="fk_wfp_version", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "name", name="uq_workflow_params_name"),
        mariadb_charset="utf8mb4",
        mariadb_collate="utf8mb4_unicode_ci",
        mariadb_engine="InnoDB",
    )
    op.create_table(
        "workflow_role_bindings",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("actor_id", UINT, nullable=False),
        sa.Column("rank", mysql.TINYINT(unsigned=True), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_wfrb_actor"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_wfrb_project"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "role", "actor_id", name="uq_workflow_role_bindings_actor"),
        mariadb_charset="utf8mb4",
        mariadb_collate="utf8mb4_unicode_ci",
        mariadb_engine="InnoDB",
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("template_id", UINT, nullable=False),
        sa.Column("version_id", UINT, nullable=False),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("run_seq", UINT, nullable=False),
        sa.Column(
            "trigger_kind",
            sa.Enum("manual", "api", "schedule", "event", "workflow", name="workflow_trigger_kind"),
            nullable=False,
        ),
        sa.Column("parent_run_id", UINT, nullable=True),
        sa.Column("scheduled_for", sa.DateTime(), nullable=True),
        sa.Column("started_by", UINT, nullable=True),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "running", "waiting", "paused", "succeeded", "failed", "cancelled", name="workflow_run_status"
            ),
            nullable=False,
        ),
        sa.Column("current_position", mysql.SMALLINT(unsigned=True), nullable=True),
        sa.Column("budget_tokens", UINT, nullable=True),
        sa.Column("budget_cents", UINT, nullable=True),
        sa.Column("used_tokens", UINT, server_default="0", nullable=False),
        sa.Column("used_cents", UINT, server_default="0", nullable=False),
        sa.Column("pause_reason", sa.String(length=40), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["parent_run_id"], ["workflow_runs.id"], name="fk_wfr_parent"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_wfr_project"),
        sa.ForeignKeyConstraint(["started_by"], ["actors.id"], name="fk_wfr_starter"),
        sa.ForeignKeyConstraint(["template_id"], ["workflow_templates.id"], name="fk_wfr_template"),
        sa.ForeignKeyConstraint(["version_id"], ["workflow_template_versions.id"], name="fk_wfr_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "run_seq", name="uq_workflow_runs_seq"),
        mariadb_charset="utf8mb4",
        mariadb_collate="utf8mb4_unicode_ci",
        mariadb_engine="InnoDB",
    )
    op.create_index("idx_workflow_runs_project_created", "workflow_runs", ["project_id", "created_at"], unique=False)
    op.create_index("idx_workflow_runs_status", "workflow_runs", ["status"], unique=False)
    op.create_table(
        "workflow_run_steps",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("run_id", UINT, nullable=False),
        sa.Column("step_id", UINT, nullable=False),
        sa.Column("position", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("attempt", mysql.TINYINT(unsigned=True), server_default="1", nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "active",
                "waiting",
                "succeeded",
                "failed",
                "skipped",
                "cancelled",
                name="workflow_run_step_status",
            ),
            nullable=False,
        ),
        sa.Column("task_id", UINT, nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("output_json", mysql.MEDIUMTEXT(), nullable=True),
        sa.Column(
            "output_class", sa.Enum("cloud_ok", "local_only", "sealed", name="workflow_output_class"), nullable=True
        ),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], name="fk_wfrs_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["workflow_steps.id"], name="fk_wfrs_step"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_wfrs_task"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "step_id", "attempt", name="uq_workflow_run_steps_attempt"),
        mariadb_charset="utf8mb4",
        mariadb_collate="utf8mb4_unicode_ci",
        mariadb_engine="InnoDB",
    )
    op.create_foreign_key(
        "fk_wft_current_version", "workflow_templates", "workflow_template_versions", ["current_version_id"], ["id"]
    )
    op.add_column("tasks", sa.Column("workflow_run_step_id", UINT, nullable=True))
    op.add_column("tasks", sa.Column("merge_commit_sha", mysql.CHAR(40), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "task_kind",
            sa.Enum("work", "profile", "run", "approval", name="task_kind"),
            server_default="work",
            nullable=False,
        ),
    )
    op.create_foreign_key("fk_tasks_workflow_run_step", "tasks", "workflow_run_steps", ["workflow_run_step_id"], ["id"])
    op.create_index("idx_tasks_workflow_run_step", "tasks", ["workflow_run_step_id"], unique=False)


def downgrade() -> None:
    op.drop_constraint("fk_tasks_workflow_run_step", "tasks", type_="foreignkey")
    op.drop_index("idx_tasks_workflow_run_step", table_name="tasks")
    op.drop_column("tasks", "task_kind")
    op.drop_column("tasks", "merge_commit_sha")
    op.drop_column("tasks", "workflow_run_step_id")
    op.drop_constraint("fk_wft_current_version", "workflow_templates", type_="foreignkey")
    op.drop_table("workflow_run_steps")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_role_bindings")
    op.drop_table("workflow_params")
    op.drop_table("workflow_steps")
    op.drop_table("workflow_template_versions")
    op.drop_table("workflow_templates")
