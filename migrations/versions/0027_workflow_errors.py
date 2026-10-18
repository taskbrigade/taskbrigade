"""Durable working-time clocks, reminders and retry assignees. Revision 0027."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workflow_run_steps", sa.Column("work_us", mysql.BIGINT(unsigned=True), nullable=False,
                                                server_default="0"))
    op.add_column("workflow_run_steps", sa.Column("work_started_at", mysql.DATETIME(fsp=6)))
    for name in ("wait_started_at", "idle_since", "last_reminder_at", "idle_notified_at"):
        op.add_column("workflow_run_steps", sa.Column(name, sa.DateTime))
    op.add_column("workflow_run_steps", sa.Column("assignee_id", mysql.INTEGER(unsigned=True)))
    op.create_foreign_key("fk_wfrs_assignee", "workflow_run_steps", "actors", ["assignee_id"], ["id"])
    # Begin clocks for already running work at upgrade; do not count earlier human/queue waits.
    op.execute("UPDATE workflow_run_steps r JOIN tasks t ON t.id = r.task_id "
               "SET r.assignee_id = t.assigned_to, "
               "r.work_started_at = IF(t.status = 'in_progress', UTC_TIMESTAMP(6), NULL), "
               "r.wait_started_at = IF(t.status IN ('questions','for_review'), UTC_TIMESTAMP(), NULL), "
               "r.idle_since = IF(t.status = 'defined', UTC_TIMESTAMP(), NULL) "
               "WHERE r.status IN ('active','waiting')")


def downgrade():
    op.drop_constraint("fk_wfrs_assignee", "workflow_run_steps", type_="foreignkey")
    for name in ("assignee_id", "idle_notified_at", "last_reminder_at", "idle_since", "wait_started_at",
                 "work_started_at", "work_us"):
        op.drop_column("workflow_run_steps", name)
