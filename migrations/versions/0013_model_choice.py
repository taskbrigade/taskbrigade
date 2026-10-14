"""Model choice: task model tier/concrete model, agent default model, tier mapping and allow list

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All nullable: existing tasks and agents keep today's behaviour (no model switch, the agent's own default).
    op.add_column("tasks", sa.Column("model_tier", sa.Enum("simple", "standard", "complex", name="model_tier"),
                                     nullable=True))
    op.add_column("tasks", sa.Column("model", sa.String(100), nullable=True))
    op.add_column("actors", sa.Column("model_default", sa.String(100), nullable=True))
    op.add_column("actors", sa.Column("model_tiers", sa.Text, nullable=True))
    op.add_column("actors", sa.Column("models_allowed", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("actors", "models_allowed")
    op.drop_column("actors", "model_tiers")
    op.drop_column("actors", "model_default")
    op.drop_column("tasks", "model")
    op.drop_column("tasks", "model_tier")
