"""Output gate (research/konzept-datenzonen.md §6, DZ 8): `run_results.status` gains `kept_sealed` (the result stays in
zone R, the run task is done without a release – research/konzept-workflows.md §11.2), and projects get the four-eyes
option `data_four_eyes` (NULL = the admin default `data_four_eyes`, on unless changed).

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

OLD = ("quarantine", "checks_failed", "pending_approval", "released", "rejected", "deleted")
NEW = OLD + ("kept_sealed",)


def upgrade() -> None:
    op.alter_column("run_results", "status", existing_type=sa.Enum(*OLD, name="run_result_status"),
                    type_=sa.Enum(*NEW, name="run_result_status"), existing_nullable=False,
                    existing_server_default="quarantine")
    op.add_column("projects", sa.Column("data_four_eyes", sa.Boolean, nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "data_four_eyes")
    op.execute("UPDATE run_results SET status = 'rejected' WHERE status = 'kept_sealed'")
    op.alter_column("run_results", "status", existing_type=sa.Enum(*NEW, name="run_result_status"),
                    type_=sa.Enum(*OLD, name="run_result_status"), existing_nullable=False,
                    existing_server_default="quarantine")
