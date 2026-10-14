"""Task status 'draft' (not delivered until published as 'defined')

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

OLD = "'defined','in_progress','questions','aborted','for_review','changes_requested','done','cancelled'"
NEW = "'draft'," + OLD


def upgrade() -> None:
    op.execute(f"ALTER TABLE tasks MODIFY status ENUM({NEW}) NOT NULL DEFAULT 'defined'")


def downgrade() -> None:
    op.execute("UPDATE tasks SET status = 'defined' WHERE status = 'draft'")
    op.execute(f"ALTER TABLE tasks MODIFY status ENUM({OLD}) NOT NULL DEFAULT 'defined'")
