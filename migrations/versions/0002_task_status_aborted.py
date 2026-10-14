"""Task status 'aborted' (agent failed; awaiting reassignment)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

OLD = "'defined','in_progress','questions','for_review','changes_requested','done','cancelled'"
NEW = "'defined','in_progress','questions','aborted','for_review','changes_requested','done','cancelled'"


def upgrade() -> None:
    op.execute(f"ALTER TABLE tasks MODIFY status ENUM({NEW}) NOT NULL DEFAULT 'defined'")


def downgrade() -> None:
    op.execute("UPDATE tasks SET status = 'defined' WHERE status = 'aborted'")
    op.execute(f"ALTER TABLE tasks MODIFY status ENUM({OLD}) NOT NULL DEFAULT 'defined'")
