"""Initial schema (spec section 7) and bootstrap admin seed

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

OPTS = {"mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci"}
UINT = mysql.INTEGER(unsigned=True)
NOW = sa.text("CURRENT_TIMESTAMP")
NOW_UPD = sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=NOW_UPD),
    ]


def upgrade() -> None:
    # Spec 7: set the database default to utf8mb4_unicode_ci (NAS default is utf8mb4_general_ci).
    op.execute("ALTER DATABASE CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    op.create_table(
        "actors",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("actor_type", sa.Enum("human", "agent"), nullable=False),
        sa.Column("agent_kind", sa.Enum("hermes", "openclaw", "codex", "claude_code", "opencode", "other")),
        sa.Column("runtime", sa.Enum("local", "cloud"), nullable=False, server_default="local"),
        sa.Column("role", sa.Enum("admin", "orchestrator", "worker"), nullable=False, server_default="worker"),
        sa.Column("api_key_hash", mysql.CHAR(64)),
        sa.Column("api_key_prefix", mysql.CHAR(8)),
        sa.Column("api_key_created_at", sa.DateTime),
        sa.Column("email", sa.String(200)),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("tailscale_login", sa.String(200), unique=True),
        sa.Column("gitea_username", sa.String(100), unique=True),
        sa.Column("gitea_token_enc", sa.Text),
        sa.Column("gitea_token_last4", mysql.CHAR(4)),
        sa.Column("gitea_token_checked_at", sa.DateTime),
        sa.Column("gitea_email", sa.String(200)),
        sa.Column("lease_minutes", mysql.SMALLINT(unsigned=True), nullable=False, server_default="120"),
        sa.Column("max_parallel_tasks", mysql.TINYINT(unsigned=True), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("notes", sa.Text),
        sa.Column("last_seen_at", sa.DateTime),
        *_timestamps(),
        **OPTS,
    )

    op.create_table(
        "projects",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("project_key", sa.String(8), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.Enum("idea", "active", "paused", "archived"), nullable=False, server_default="idea"),
        sa.Column("privacy", sa.Enum("local_only", "cloud_ok"), nullable=False, server_default="local_only"),
        sa.Column("gitea_owner", sa.String(100)),
        sa.Column("gitea_repo", sa.String(100)),
        sa.Column("gitea_url", sa.String(500)),
        sa.Column("default_branch", sa.String(100), nullable=False, server_default="main"),
        sa.Column("brain_path", sa.String(200), nullable=False, server_default="brain.md"),
        sa.Column("next_task_seq", UINT, nullable=False, server_default="1"),
        sa.Column("created_by", UINT, nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("gitea_owner", "gitea_repo", name="uq_projects_gitea"),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_projects_created_by"),
        **OPTS,
    )

    op.create_table(
        "project_members",
        sa.Column("project_id", UINT, primary_key=True),
        sa.Column("actor_id", UINT, primary_key=True),
        sa.Column("gitea_permission", sa.Enum("read", "write"), nullable=False, server_default="write"),
        sa.Column("gitea_synced_at", sa.DateTime),
        sa.Column("sync_error", sa.Text),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_pm_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_pm_actor", ondelete="CASCADE"),
        **OPTS,
    )

    op.create_table(
        "app_settings",
        sa.Column("setting_key", sa.String(100), primary_key=True),
        sa.Column("setting_value", sa.Text),
        sa.Column("is_secret", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("updated_by", UINT),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=NOW_UPD),
        sa.ForeignKeyConstraint(["updated_by"], ["actors.id"], name="fk_settings_actor"),
        **OPTS,
    )

    op.create_table(
        "tasks",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("task_nr", sa.String(16), nullable=False, unique=True),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("parent_task_id", UINT),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", mysql.MEDIUMTEXT),
        sa.Column("acceptance_criteria", sa.Text),
        sa.Column("status", sa.Enum("defined", "in_progress", "questions", "for_review",
                                    "changes_requested", "done", "cancelled"),
                  nullable=False, server_default="defined"),
        sa.Column("prio", mysql.TINYINT(unsigned=True), nullable=False, server_default="3"),
        sa.Column("assigned_to", UINT),
        sa.Column("created_by", UINT, nullable=False),
        sa.Column("detail_task_git_url", sa.String(500)),
        sa.Column("branch_name", sa.String(200)),
        sa.Column("pr_number", UINT),
        sa.Column("pr_url", sa.String(500)),
        sa.Column("result_commit_sha", mysql.CHAR(40)),
        sa.Column("result_summary", sa.Text),
        sa.Column("due_date", sa.Date),
        sa.Column("claimed_at", sa.DateTime),
        sa.Column("lease_expires_at", sa.DateTime),
        sa.Column("submitted_at", sa.DateTime),
        sa.Column("done_at", sa.DateTime),
        *_timestamps(),
        sa.Column("row_version", UINT, nullable=False, server_default="1"),
        sa.CheckConstraint("prio BETWEEN 1 AND 5", name="chk_tasks_prio"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_tasks_project"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"], name="fk_tasks_parent"),
        sa.ForeignKeyConstraint(["assigned_to"], ["actors.id"], name="fk_tasks_assigned"),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_tasks_creator"),
        **OPTS,
    )
    op.create_index("idx_tasks_pick", "tasks", ["assigned_to", "status", "prio", "created_at"])
    op.create_index("idx_tasks_project_status", "tasks", ["project_id", "status"])
    op.create_index("idx_tasks_lease", "tasks", ["status", "lease_expires_at"])

    op.create_table(
        "task_dependencies",
        sa.Column("task_id", UINT, primary_key=True),
        sa.Column("depends_on_task_id", UINT, primary_key=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_dep_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"], name="fk_dep_on", ondelete="CASCADE"),
        **OPTS,
    )

    op.create_table(
        "task_comments",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("task_id", UINT, nullable=False),
        sa.Column("author_id", UINT, nullable=False),
        sa.Column("comment_type", sa.Enum("comment", "question", "answer", "review", "system"),
                  nullable=False, server_default="comment"),
        sa.Column("body", mysql.MEDIUMTEXT, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_comments_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["actors.id"], name="fk_comments_author"),
        **OPTS,
    )
    op.create_index("idx_comments_task", "task_comments", ["task_id", "created_at"])

    op.create_table(
        "task_events",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("task_id", UINT, nullable=False),
        sa.Column("actor_id", UINT),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("from_value", sa.String(100)),
        sa.Column("to_value", sa.String(100)),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_events_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_events_actor"),
        **OPTS,
    )
    op.create_index("idx_events_task", "task_events", ["task_id", "created_at"])

    op.create_table(
        "labels",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("color", mysql.CHAR(7), nullable=False, server_default="#888888"),
        **OPTS,
    )
    op.create_table(
        "task_labels",
        sa.Column("task_id", UINT, primary_key=True),
        sa.Column("label_id", UINT, primary_key=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["label_id"], ["labels.id"], ondelete="CASCADE"),
        **OPTS,
    )

    actors = sa.table(
        "actors",
        *(sa.column(c, sa.String) for c in
          ("name", "display_name", "actor_type", "agent_kind", "runtime", "role")),
    )
    # Only the bootstrap admin (default `admin`); agents are created by the operator (Admin → New agent).
    # Installations migrated before this change keep the agents an earlier version of this migration seeded.
    admin = get_settings().bootstrap_admin_actor
    op.bulk_insert(actors, [
        {"name": admin, "display_name": admin.capitalize(), "actor_type": "human", "agent_kind": None,
         "runtime": "local", "role": "admin"},
    ])


def downgrade() -> None:
    for table in ("task_labels", "labels", "task_events", "task_comments", "task_dependencies",
                  "tasks", "app_settings", "project_members", "projects", "actors"):
        op.drop_table(table)
