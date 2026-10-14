"""Telegram: chat links per human, one-time link codes, notification settings and the notification outbox/log

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci"}
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "telegram_links",
        sa.Column("actor_id", UINT, primary_key=True),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("chat_name", sa.String(100)),
        sa.Column("linked_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.UniqueConstraint("chat_id", name="uq_telegram_links_chat"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_telegram_links_actor", ondelete="CASCADE"),
        **OPTS,
    )
    op.create_table(
        "telegram_link_codes",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("actor_id", UINT, nullable=False),
        sa.Column("code_hash", mysql.CHAR(64), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime),
        sa.UniqueConstraint("code_hash", name="uq_telegram_codes_hash"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_telegram_codes_actor", ondelete="CASCADE"),
        **OPTS,
    )
    op.create_table(
        "notification_prefs",
        sa.Column("actor_id", UINT, primary_key=True),
        sa.Column("events", sa.Text),
        sa.Column("quiet_start", mysql.SMALLINT(unsigned=True)),
        sa.Column("quiet_end", mysql.SMALLINT(unsigned=True)),
        sa.Column("mute_all_until", sa.DateTime),
        sa.Column("mute_review_until", sa.DateTime),
        sa.Column("all_projects", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_notification_prefs_actor", ondelete="CASCADE"),
        **OPTS,
    )
    op.create_table(
        "notifications",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("actor_id", UINT, nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("task_nr", sa.String(16)),
        sa.Column("status", sa.Enum("queued", "held", "sent", "failed", "skipped", name="notification_status"),
                  nullable=False, server_default="queued"),
        sa.Column("text", sa.Text),
        sa.Column("attempts", mysql.TINYINT(unsigned=True), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime),
        sa.Column("last_error", sa.String(200)),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("sent_at", sa.DateTime),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_notifications_actor", ondelete="CASCADE"),
        sa.Index("idx_notifications_due", "status", "next_attempt_at"),
        sa.Index("idx_notifications_created", "created_at"),
        **OPTS,
    )


def downgrade() -> None:
    for table in ("notifications", "notification_prefs", "telegram_link_codes", "telegram_links"):
        op.drop_table(table)
