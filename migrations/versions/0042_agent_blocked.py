"""Persistent blocked state of an agent (login expired, usage limit) reported by its runner.

`tbr run` writes `.agent-blocked` locally and reports it with `POST /api/v1/agents/me/blocked`; `tbr doctor`, the
runner's own recovery check or an admin clears it. `blocked_reminded_at` is the last reminder notification.

Revision ID: 0042
Revises: 0041
"""
import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("actors", sa.Column("blocked_kind", sa.String(20), nullable=True))
    op.add_column("actors", sa.Column("blocked_reason", sa.String(1000), nullable=True))
    op.add_column("actors", sa.Column("blocked_host", sa.String(255), nullable=True))
    op.add_column("actors", sa.Column("blocked_task_nr", sa.String(30), nullable=True))
    op.add_column("actors", sa.Column("blocked_since", sa.DateTime, nullable=True))
    op.add_column("actors", sa.Column("blocked_reminded_at", sa.DateTime, nullable=True))


def downgrade() -> None:
    for column in ("blocked_reminded_at", "blocked_since", "blocked_task_nr", "blocked_host", "blocked_reason",
                   "blocked_kind"):
        op.drop_column("actors", column)
