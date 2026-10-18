"""Reviewed script pins shared across workflow versions.

Revision ID: 0035
Revises: 0034
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None
UINT = sa.Integer().with_variant(mysql.INTEGER(unsigned=True), "mariadb", "mysql")


def upgrade():
    op.create_table(
        "workflow_script_pins",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("template_id", UINT, sa.ForeignKey("workflow_templates.id", name="fk_wsp_template"), nullable=False),
        sa.Column("step_key", sa.String(40), nullable=False),
        sa.Column("repo_path", sa.String(300), nullable=False),
        sa.Column("commit_sha", sa.CHAR(40), nullable=False),
        sa.Column("schema_fingerprint", sa.CHAR(64)),
        sa.Column("profile_id", UINT, sa.ForeignKey("dataset_profiles.id", name="fk_wsp_profile"), nullable=False),
        sa.Column("source_task_id", UINT, sa.ForeignKey("tasks.id", name="fk_wsp_task"), nullable=False),
        sa.Column("pinned_by", UINT, sa.ForeignKey("actors.id", name="fk_wsp_actor")),
        sa.Column("pinned_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("retired_at", sa.DateTime),
        mariadb_engine="InnoDB", mariadb_charset="utf8mb4", mariadb_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_wsp_active", "workflow_script_pins", ["template_id", "step_key", "retired_at"])


def downgrade():
    op.drop_table("workflow_script_pins")
