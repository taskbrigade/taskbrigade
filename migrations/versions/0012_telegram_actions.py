"""Telegram actions: button/reply tokens, questions read in the browser, message ids of sent notifications

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci"}
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "telegram_actions",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("token_hash", mysql.CHAR(64), nullable=False),
        sa.Column("keyboard", mysql.CHAR(16), nullable=False),
        sa.Column("actor_id", UINT, nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("task_nr", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime),
        sa.Column("result", sa.String(200)),
        sa.UniqueConstraint("token_hash", name="uq_telegram_actions_token"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_telegram_actions_actor", ondelete="CASCADE"),
        sa.Index("idx_telegram_actions_keyboard", "keyboard"),
        sa.Index("idx_telegram_actions_actor", "actor_id", "kind", "used_at"),
        **OPTS,
    )
    op.create_table(
        "question_reads",
        sa.Column("actor_id", UINT, primary_key=True),
        sa.Column("comment_id", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column("read_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_question_reads_actor", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["comment_id"], ["task_comments.id"], name="fk_question_reads_comment",
                                ondelete="CASCADE"),
        **OPTS,
    )
    op.add_column("notifications", sa.Column("telegram_message_id", sa.BigInteger, nullable=True))
    op.create_index("idx_notifications_message", "notifications", ["actor_id", "telegram_message_id"])


def downgrade() -> None:
    op.drop_index("idx_notifications_message", "notifications")
    op.drop_column("notifications", "telegram_message_id")
    op.drop_table("question_reads")
    op.drop_table("telegram_actions")
