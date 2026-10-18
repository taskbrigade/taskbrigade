"""Task chain across zones (research/konzept-datenzonen.md §1, DZ 9): `task_data_refs` – references of a task to a
dataset (input), a released profile (schema) or a released runner result (output) instead of their content.

Existing handoffs are carried over: runner tasks with `run_dataset_id` get a `dataset`/`input` reference, the draft
code task of a released profile (`dataset_profiles.code_task_id`) a `profile`/`schema` reference.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

UINT = sa.Integer().with_variant(mysql.INTEGER(unsigned=True), "mariadb", "mysql")
UBIGINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mariadb", "mysql")


def upgrade() -> None:
    op.create_table(
        "task_data_refs",
        sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
        sa.Column("task_id", UINT, sa.ForeignKey("tasks.id", name="fk_task_data_refs_task", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("ref_type", sa.Enum("dataset", "profile", "result", name="data_ref_type"), nullable=False),
        sa.Column("ref_id", UINT, nullable=False),
        sa.Column("purpose", sa.Enum("input", "schema", "output", name="data_ref_purpose"), nullable=False),
        sa.Column("created_by", UINT, sa.ForeignKey("actors.id", name="fk_task_data_refs_creator",
                                                    ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("task_id", "ref_type", "ref_id", name="uq_task_data_refs"),
        mariadb_engine="InnoDB", mariadb_charset="utf8mb4", mariadb_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_task_data_refs_ref", "task_data_refs", ["ref_type", "ref_id"])
    op.execute("INSERT INTO task_data_refs (task_id, ref_type, ref_id, purpose, created_by) "
               "SELECT id, 'dataset', run_dataset_id, 'input', created_by FROM tasks "
               "WHERE run_dataset_id IS NOT NULL AND task_kind IN ('run', 'profile')")
    op.execute("INSERT INTO task_data_refs (task_id, ref_type, ref_id, purpose, created_by) "
               "SELECT p.code_task_id, 'profile', p.id, 'schema', p.released_by FROM dataset_profiles p "
               "JOIN tasks t ON t.id = p.code_task_id WHERE p.status = 'released'")


def downgrade() -> None:
    op.drop_table("task_data_refs")
