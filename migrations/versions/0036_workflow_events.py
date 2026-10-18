"""Event triggers: outbox, trigger config and exactly-once deliveries.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
UBIG = mysql.BIGINT(unsigned=True)
OPTS = {"mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci", "mariadb_engine": "InnoDB"}
KIND = sa.Enum("dataset_ready", "workflow_succeeded", "workflow_failed", name="workflow_trigger_event_kind")
OUTCOME = sa.Enum("started", "queued", "skipped_overlap", "skipped_misfire", "failed",
                  name="workflow_trigger_fire_outcome")


def upgrade():
    op.create_table(
        "workflow_triggers",
        sa.Column("id", UINT, primary_key=True, autoincrement=True),
        sa.Column("template_id", UINT, nullable=False),
        sa.Column("kind", KIND, nullable=False),
        sa.Column("filter_json", sa.Text, nullable=False),
        sa.Column("params_json", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", UINT, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["template_id"], ["workflow_templates.id"], name="fk_wftr_template",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["actors.id"], name="fk_wftr_creator"),
        sa.Index("idx_workflow_triggers_template", "template_id", "enabled"),
        **OPTS,
    )
    op.create_table(
        "workflow_events",
        sa.Column("id", UBIG, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processed_at", sa.DateTime),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_wfe_project"),
        sa.UniqueConstraint("dedupe_key", name="uq_workflow_events_dedupe"),
        sa.Index("idx_workflow_events_processed", "processed_at"),
        **OPTS,
    )
    op.add_column("workflow_runs", sa.Column("trigger_id", UINT))
    op.create_foreign_key("fk_wfr_trigger", "workflow_runs", "workflow_triggers", ["trigger_id"], ["id"],
                          ondelete="SET NULL")
    op.create_table(
        "workflow_trigger_fires",
        sa.Column("id", UBIG, primary_key=True, autoincrement=True),
        sa.Column("trigger_id", UINT, nullable=False),
        sa.Column("event_id", UBIG, nullable=False),
        sa.Column("fired_at", sa.DateTime, nullable=False),
        sa.Column("outcome", OUTCOME, nullable=False),
        sa.Column("run_id", UINT),
        sa.Column("note", sa.String(200)),
        sa.ForeignKeyConstraint(["trigger_id"], ["workflow_triggers.id"], name="fk_wftf_trigger", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["workflow_events.id"], name="fk_wftf_event", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], name="fk_wftf_run"),
        sa.UniqueConstraint("trigger_id", "event_id", name="uq_workflow_trigger_fires_once"),
        **OPTS,
    )


def downgrade():
    op.drop_table("workflow_trigger_fires")
    op.drop_constraint("fk_wfr_trigger", "workflow_runs", type_="foreignkey")
    op.drop_column("workflow_runs", "trigger_id")
    op.drop_table("workflow_events")
    op.drop_table("workflow_triggers")
