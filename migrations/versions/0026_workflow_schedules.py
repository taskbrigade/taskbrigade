"""Workflow schedules, calendars and slot audit. Revision 0026, follows 0025."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_engine": "InnoDB", "mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci"}
OUTCOMES = ("started", "queued", "skipped_overlap", "skipped_misfire", "failed")


def upgrade():
    op.create_table(
        "workflow_calendars",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("country", sa.String(2), nullable=False, server_default="DE"),
        sa.Column("subdivision", sa.String(10)),
        sa.Column("workdays", sa.String(30), nullable=False, server_default="mon,tue,wed,thu,fri"),
        **OPTS,
    )
    op.create_table(
        "workflow_calendar_days",
        sa.Column("calendar_id", UINT, sa.ForeignKey("workflow_calendars.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("kind", sa.Enum("closed", "open", name="workflow_calendar_day_kind"), nullable=False),
        sa.Column("note", sa.String(100)),
        **OPTS,
    )
    op.create_table(
        "workflow_schedules",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("template_id", UINT, sa.ForeignKey("workflow_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.Enum("cron", "workday", name="workflow_schedule_kind"), nullable=False),
        sa.Column("cron_expr", sa.String(100)),
        sa.Column("workday_rule_json", sa.String(300)),
        sa.Column("timezone", sa.String(40), nullable=False, server_default="Europe/Berlin"),
        sa.Column("calendar_id", UINT, sa.ForeignKey("workflow_calendars.id")),
        sa.Column("holiday_shift", sa.Enum("skip", "next_workday", "prev_workday", name="workflow_holiday_shift"),
                  nullable=False, server_default="skip"),
        sa.Column("params_json", sa.Text),
        sa.Column("overlap_policy", sa.Enum("skip", "queue", name="workflow_overlap_policy"),
                  nullable=False, server_default="skip"),
        sa.Column("misfire_policy", sa.Enum("run_late", "skip", name="workflow_misfire_policy"),
                  nullable=False, server_default="run_late"),
        sa.Column("misfire_grace_min", UINT, nullable=False, server_default="360"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("next_fire_at", sa.DateTime),
        sa.Column("last_fire_at", sa.DateTime),
        sa.Column("last_outcome", sa.Enum(*OUTCOMES, name="workflow_schedule_outcome")),
        sa.Column("created_by", UINT, sa.ForeignKey("actors.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")),
        sa.Index("idx_workflow_schedules_due", "enabled", "next_fire_at"),
        **OPTS,
    )
    op.add_column("workflow_runs", sa.Column("schedule_id", UINT))
    op.add_column("workflow_runs", sa.Column("schedule_timezone", sa.String(40)))
    op.create_foreign_key("fk_wfr_schedule", "workflow_runs", "workflow_schedules", ["schedule_id"], ["id"])
    op.create_unique_constraint("uq_workflow_runs_slot", "workflow_runs", ["schedule_id", "scheduled_for"])
    op.create_table(
        "workflow_schedule_fires",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("schedule_id", UINT, sa.ForeignKey("workflow_schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime, nullable=False),
        sa.Column("fired_at", sa.DateTime, nullable=False),
        sa.Column("outcome", sa.Enum(*OUTCOMES, name="workflow_fire_outcome"), nullable=False),
        sa.Column("run_id", UINT, sa.ForeignKey("workflow_runs.id")),
        sa.Column("note", sa.String(200)),
        sa.UniqueConstraint("schedule_id", "scheduled_for", name="uq_workflow_fires_slot"),
        **OPTS,
    )


def downgrade():
    op.drop_table("workflow_schedule_fires")
    op.drop_constraint("fk_wfr_schedule", "workflow_runs", type_="foreignkey")
    op.drop_constraint("uq_workflow_runs_slot", "workflow_runs", type_="unique")
    op.drop_column("workflow_runs", "schedule_timezone")
    op.drop_column("workflow_runs", "schedule_id")
    op.drop_table("workflow_schedules")
    op.drop_table("workflow_calendar_days")
    op.drop_table("workflow_calendars")
