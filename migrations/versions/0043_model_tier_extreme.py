"""Model tier `extreme` for extremely hard tasks (`tasks.model_tier`, `workflow_steps.model_tier`); Claude Code agents
map it to Fable unless they set their own model for it (`app/services/model_choice.py` `KIND_TIERS`).

Revision ID: 0043
Revises: 0042
"""
import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

OLD = ("simple", "standard", "complex")
NEW = OLD + ("extreme",)
TABLES = ("tasks", "workflow_steps")


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "model_tier", existing_type=sa.Enum(*OLD, name="model_tier"),
                        type_=sa.Enum(*NEW, name="model_tier"), existing_nullable=True)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"UPDATE {table} SET model_tier = 'complex' WHERE model_tier = 'extreme'")
        op.alter_column(table, "model_tier", existing_type=sa.Enum(*NEW, name="model_tier"),
                        type_=sa.Enum(*OLD, name="model_tier"), existing_nullable=True)
