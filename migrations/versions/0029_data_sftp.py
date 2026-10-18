"""Native SFTP inbox (research/konzept-datenzonen.md §4/§9): `data_sftp_accounts` maps a system SFTP user to a project,
`data_sftp_keys` holds the public keys allowed to log in. The system user, the chroot directory and the sshd drop-in
live outside the database (`deploy/ssh/60-taskbrigade-sftp.conf`, `tbr-admin data sftp-user`).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci", "mariadb_engine": "InnoDB"}


def upgrade() -> None:
    op.create_table(
        "data_sftp_accounts",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("username", sa.String(64), nullable=False),  # System user, no shell, chrooted
        sa.Column("project_id", UINT, nullable=False),  # Who the deliveries belong to
        sa.Column("name", sa.String(200), nullable=True),  # Human label, e.g. "Accounting export"
        sa.Column("owner_id", UINT, nullable=False),  # Human credited as uploader in the audit
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("last_import_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_data_sftp_accounts_project"),
        sa.ForeignKeyConstraint(["owner_id"], ["actors.id"], name="fk_data_sftp_accounts_owner"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_data_sftp_accounts_username"),
        **OPTS,
    )
    op.create_table(
        "data_sftp_keys",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("account_id", UINT, nullable=False),
        sa.Column("key_type", sa.String(32), nullable=False),  # ssh-ed25519 / ssh-rsa / ecdsa-sha2-…
        sa.Column("key_blob", sa.Text(), nullable=False),  # Base64 key body, never the comment
        sa.Column("fingerprint", mysql.CHAR(64), nullable=False),  # SHA256:… (the `authorized_keys` comment)
        sa.Column("comment", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["data_sftp_accounts.id"], name="fk_data_sftp_keys_account",
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "fingerprint", name="uq_data_sftp_keys_fingerprint"),
        **OPTS,
    )


def downgrade() -> None:
    op.drop_table("data_sftp_keys")
    op.drop_table("data_sftp_accounts")
