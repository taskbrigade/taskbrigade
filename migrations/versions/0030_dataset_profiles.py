"""Sandboxed dataset profiles and human release.

Revision ID: 0030
Revises: 0029
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0030"
down_revision = "0029"
branch_labels = depends_on = None


def upgrade():
    uint = mysql.INTEGER(unsigned=True)
    op.create_table(
        "dataset_profiles",
        sa.Column("id", uint, primary_key=True, autoincrement=True),
        sa.Column("dataset_id", uint, sa.ForeignKey("datasets.id", name="fk_profiles_dataset"), nullable=False),
        sa.Column("task_id", uint, sa.ForeignKey("tasks.id", name="fk_profiles_task", ondelete="SET NULL")),
        sa.Column("code_task_id", uint, sa.ForeignKey("tasks.id", name="fk_profiles_code_task", ondelete="SET NULL")),
        sa.Column("profiler_version", sa.String(20), nullable=False),
        sa.Column("schema_fingerprint", mysql.CHAR(64), nullable=False),
        sa.Column("schema_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("sample_csv", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("aliases_json", mysql.MEDIUMTEXT()),
        sa.Column("status", sa.Enum("generated", "released", "rejected", name="profile_status"),
                  nullable=False, server_default="generated"),
        sa.Column("released_by", uint, sa.ForeignKey("actors.id", name="fk_profiles_releaser")),
        sa.Column("released_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Index("idx_dataset_profiles_dataset", "dataset_id", "id"),
        mariadb_charset="utf8mb4", mariadb_collate="utf8mb4_unicode_ci", mariadb_engine="InnoDB",
    )


def downgrade():
    op.drop_table("dataset_profiles")
