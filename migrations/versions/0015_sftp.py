"""SFTP storage: SFTP user and encrypted password per agent

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global settings (host, port, host key, base folder, pattern) live in app_settings; only the per-agent login here.
    op.add_column("actors", sa.Column("sftp_username", sa.String(100), nullable=True))
    op.add_column("actors", sa.Column("sftp_password_enc", sa.Text, nullable=True))
    op.add_column("actors", sa.Column("sftp_password_last4", sa.String(4).with_variant(mysql.CHAR(4), "mysql", "mariadb"), nullable=True))
    op.add_column("actors", sa.Column("sftp_checked_at", sa.DateTime, nullable=True))
    op.create_unique_constraint("uq_actors_sftp_username", "actors", ["sftp_username"])


def downgrade() -> None:
    op.drop_constraint("uq_actors_sftp_username", "actors", type_="unique")
    for column in ("sftp_checked_at", "sftp_password_last4", "sftp_password_enc", "sftp_username"):
        op.drop_column("actors", column)
