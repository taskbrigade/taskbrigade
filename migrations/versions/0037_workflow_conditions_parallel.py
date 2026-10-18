"""Workflow step conditions and parallel groups (WF 13).

`condition_tpl` is the `when:` template: an empty/whitespace result, or one of the falsy literals, skips the step
with `status=skipped` and `error_code=condition_false`. `parallel_group` groups consecutive steps that activate at
once and only advance the run when every member is done.

Revision ID: 0037
Revises: 0036
"""
import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_steps", sa.Column("parallel_group", sa.String(40), nullable=True))
    op.add_column("workflow_steps", sa.Column("condition_tpl", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_steps", "condition_tpl")
    op.drop_column("workflow_steps", "parallel_group")
