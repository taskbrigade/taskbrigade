"""Email as a second notification channel (docs/operations.md, "Email notifications"; stage 1)

`notifications.channel` (existing rows are Telegram), `notification_prefs.channels_json` (channel per event and the
bundling interval; NULL = defaults), `actors.email_verified_at` and the one-time verification links
`email_verify_codes` (only the SHA-256 is stored). `notifications.email_message_id` is the Message-ID of the mail a
row went out in (bundled rows share one) – the throttle counts distinct mails per hour.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci"}
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.add_column("notifications", sa.Column("channel", sa.Enum("telegram", "email", name="notification_channel"),
                                             nullable=False, server_default="telegram"))
    op.add_column("notifications", sa.Column("email_message_id", sa.String(200)))
    op.create_index("idx_notifications_channel_sent", "notifications", ["channel", "sent_at"])
    op.add_column("notification_prefs", sa.Column("channels_json", sa.Text))
    op.add_column("actors", sa.Column("email_verified_at", sa.DateTime))
    op.create_table(
        "email_verify_codes",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("actor_id", UINT, nullable=False),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("code_hash", mysql.CHAR(64), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime),
        sa.UniqueConstraint("code_hash", name="uq_email_verify_codes_hash"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_email_verify_codes_actor", ondelete="CASCADE"),
        **OPTS,
    )


def downgrade() -> None:
    op.drop_table("email_verify_codes")
    op.drop_column("actors", "email_verified_at")
    op.drop_column("notification_prefs", "channels_json")
    op.drop_index("idx_notifications_channel_sent", table_name="notifications")
    op.drop_column("notifications", "email_message_id")
    op.drop_column("notifications", "channel")
