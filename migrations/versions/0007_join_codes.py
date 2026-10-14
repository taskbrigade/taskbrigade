"""Join codes: one-time codes an agent exchanges for a fresh API token (`tm join`); only the hash is stored

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)


def upgrade() -> None:
    op.create_table(
        "join_codes",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("actor_id", UINT, nullable=False),
        sa.Column("code_hash", mysql.CHAR(64), nullable=False),
        sa.Column("created_by", UINT),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime),
        sa.Column("used_from", sa.String(64)),
        sa.UniqueConstraint("code_hash", name="uq_join_codes_hash"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_join_codes_actor", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_join_codes_creator", ondelete="SET NULL"),
        mariadb_engine="InnoDB", mariadb_charset="utf8mb4", mariadb_collate="utf8mb4_unicode_ci",
    )


def downgrade() -> None:
    op.drop_table("join_codes")
