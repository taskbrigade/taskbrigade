"""Reopen a closed sprint: `sprints.moved_task_ids` stores the ids of the tasks that `close` moved
out of the sprint (to another sprint or detached) as a JSON list.

`reopen` moves exactly those tasks back. The event-based lookup alone is not reliable enough: sprint names are only
unique per project, and a task moved out by hand while the sprint was still released would look like a close move.
Sprints closed before this migration have NULL and fall back to the `sprint` events written at/after `closed_at`.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-17
"""
import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sprints", sa.Column("moved_task_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sprints", "moved_task_ids")
