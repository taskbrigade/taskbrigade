"""Task attachments (files live in the project repo; only metadata lives here)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)


def upgrade() -> None:
    op.create_table(
        "task_attachments",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("task_id", UINT, nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("repo_path", sa.String(500), nullable=False),
        sa.Column("size_bytes", UINT, nullable=False),
        sa.Column("commit_sha", mysql.CHAR(40)),
        sa.Column("uploaded_by", UINT, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("task_id", "filename", name="uq_attachment_name"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_attachments_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["actors.id"], name="fk_attachments_actor"),
        mariadb_engine="InnoDB", mariadb_charset="utf8mb4", mariadb_collate="utf8mb4_unicode_ci",
    )


def downgrade() -> None:
    op.drop_table("task_attachments")
