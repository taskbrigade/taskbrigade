"""Per-dataset output policy and durable release accounting (DZ 13).

Revision ID: 0038
Revises: 0037
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import INTEGER

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("gate_policy_json", sa.Text, nullable=True))
    count_type = sa.Integer().with_variant(INTEGER(unsigned=True), "mysql", "mariadb")
    op.add_column("datasets", sa.Column("released_count", count_type, nullable=False, server_default="0"))
    op.add_column("datasets", sa.Column("epsilon_spent", sa.Numeric(30, 6), nullable=False, server_default="0"))
    op.add_column("run_results", sa.Column("release_policy_json", sa.Text, nullable=True))
    # Historical exact releases count too, including releases whose artifacts have since been deleted.
    op.execute("UPDATE datasets d SET released_count = (SELECT COUNT(*) FROM run_results r "
               "WHERE r.dataset_id = d.id AND (r.released_at IS NOT NULL OR r.status = 'released'))")


def downgrade() -> None:
    op.drop_column("run_results", "release_policy_json")
    for name in ("epsilon_spent", "released_count", "gate_policy_json"):
        op.drop_column("datasets", name)
