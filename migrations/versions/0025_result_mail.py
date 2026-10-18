"""Work results by email (docs/konzept-email.md §3, stage 2): send requests `outgoing_mails` and the allowed
recipients per project `project_mail_recipients`.

Agents never send mail themselves: they file a request, the server checks recipient list, data class, size, types and
the daily limit and sends it after a human approved it (or at once for recipients marked "without approval").
Attachments are references (task attachment, file of the task branch or merge commit) with their SHA-256; the server
fetches them again when sending and refuses a changed file. Body and attachment references are cleared after 30 days,
the metadata (recipients, subject, hashes, status) stays.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci"}
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "project_mail_recipients",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("address", sa.String(200), nullable=False),
        sa.Column("label", sa.String(100)),
        sa.Column("mode", sa.Enum("freigabe", "ohne_freigabe", name="mail_recipient_mode"), nullable=False,
                  server_default="freigabe"),
        sa.Column("created_by", UINT),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.UniqueConstraint("project_id", "address", name="uq_mail_recipients_address"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_mail_recipients_project",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_mail_recipients_actor", ondelete="SET NULL"),
        **OPTS,
    )
    op.create_table(
        "outgoing_mails",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("task_id", UINT, nullable=False),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("requested_by", UINT, nullable=False),
        sa.Column("to_json", sa.Text, nullable=False),
        sa.Column("cc_json", sa.Text),
        sa.Column("reply_to", sa.String(200)),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", mysql.MEDIUMTEXT),
        sa.Column("attachments_json", sa.Text),
        sa.Column("status", sa.Enum("pending_approval", "queued", "sent", "failed", "rejected",
                                    name="outgoing_mail_status"), nullable=False, server_default="pending_approval"),
        sa.Column("on_approve", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("approved_by", UINT),
        sa.Column("approved_at", sa.DateTime),
        sa.Column("decided_note", sa.String(500)),
        sa.Column("message_id", sa.String(200)),
        sa.Column("smtp_error", sa.String(500)),
        sa.Column("attempts", mysql.TINYINT(unsigned=True), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("sent_at", sa.DateTime),
        sa.Column("cleared_at", sa.DateTime),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_outgoing_mails_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_outgoing_mails_project",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["actors.id"], name="fk_outgoing_mails_requester"),
        sa.ForeignKeyConstraint(["approved_by"], ["actors.id"], name="fk_outgoing_mails_approver",
                                ondelete="SET NULL"),
        sa.Index("idx_outgoing_mails_due", "status", "next_attempt_at"),
        sa.Index("idx_outgoing_mails_project", "project_id", "created_at"),
        sa.Index("idx_outgoing_mails_sent", "sent_at"),
        **OPTS,
    )


def downgrade() -> None:
    op.drop_table("outgoing_mails")
    op.drop_table("project_mail_recipients")
