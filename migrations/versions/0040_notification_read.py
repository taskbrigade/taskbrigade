"""Inbox notices feed read state (UI 3): `notifications.read_at`.

The inbox redesign shows the human's recent notifications as a notices panel; `read_at` records when the
human read them in the browser so "Mark all read" persists. NULL = unread.

Revision ID: 0040
Revises: 0039
"""
import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("read_at", sa.DateTime, nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "read_at")
