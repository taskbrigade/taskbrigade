"""Data store (research/konzept-datenzonen.md §3/§6): `datasets`, append-only `data_audit` with hash chain, the chain
head `data_audit_head` and the data right `actors.data_access`.

`data_audit` has no foreign keys: a cascading delete or SET NULL would rewrite hashed rows. Two triggers refuse
UPDATE and DELETE. Creating triggers can need extra privileges (binary logging without
`log_bin_trust_function_creators`); then the migration logs a warning and continues – the hash chain still detects
changes, and `tbr-admin data audit verify` reports the missing triggers.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-16
"""
import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci", "mariadb_engine": "InnoDB"}
GENESIS = "0" * 64
TRIGGERS = {
    "data_audit_no_update": "BEFORE UPDATE",
    "data_audit_no_delete": "BEFORE DELETE",
}
log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.add_column("actors", sa.Column("data_access", sa.Boolean(), server_default=sa.text("0"), nullable=False))
    op.create_table(
        "datasets",
        sa.Column("id", UINT, autoincrement=True, nullable=False),
        sa.Column("dataset_key", sa.String(16), nullable=False),
        sa.Column("project_id", UINT, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("data_class", sa.Enum("local_only", "sealed", name="dataset_class"), server_default="sealed",
                  nullable=False),
        sa.Column("status", sa.Enum("receiving", "quarantine", "profiling", "ready", "archived", "deleted",
                                    name="dataset_status"), server_default="receiving", nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("media_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.Column("sha256", mysql.CHAR(64), nullable=True),
        sa.Column("storage_path", sa.String(500), nullable=True),
        sa.Column("dek_wrapped", mysql.VARBINARY(128), nullable=True),
        sa.Column("supersedes_id", UINT, nullable=True),
        sa.Column("upload_channel", sa.Enum("web", "sftp", name="upload_channel"), server_default="web",
                  nullable=False),
        sa.Column("uploaded_by", UINT, nullable=False),
        sa.Column("retention_until", sa.Date(), nullable=True),
        sa.Column("retention_warned_at", sa.DateTime(), nullable=True),
        sa.Column("min_group_size", mysql.SMALLINT(unsigned=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", UINT, nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_datasets_project"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["datasets.id"], name="fk_datasets_supersedes",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["actors.id"], name="fk_datasets_uploaded_by"),
        sa.ForeignKeyConstraint(["deleted_by"], ["actors.id"], name="fk_datasets_deleted_by", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_key", name="uq_datasets_key"),
        **OPTS,
    )
    op.create_index("idx_datasets_project", "datasets", ["project_id", "status"])
    op.create_index("idx_datasets_retention", "datasets", ["status", "retention_until"])
    op.create_table(
        "data_audit",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=False, nullable=False),
        sa.Column("at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("actor_id", UINT, nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("object_type", sa.Enum("dataset", "profile", "result", "task", "actor", "project",
                                         name="audit_object_type"), nullable=False),
        sa.Column("object_id", UINT, nullable=False),
        sa.Column("task_id", UINT, nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("via", sa.Enum("web", "api", "sftp", "runner", "scheduler", name="audit_via"), nullable=False),
        sa.Column("prev_hash", mysql.CHAR(64), nullable=False),
        sa.Column("hash", mysql.CHAR(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **OPTS,
    )
    op.create_index("idx_data_audit_object", "data_audit", ["object_type", "object_id"])
    # One row: the end of the chain. Writers lock it (`SELECT … FOR UPDATE` on the primary key) – a record lock
    # without gap locks, so concurrent appends queue instead of deadlocking.
    head = op.create_table(
        "data_audit_head",
        sa.Column("id", mysql.TINYINT(unsigned=True), autoincrement=False, nullable=False),
        sa.Column("last_id", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.Column("last_hash", mysql.CHAR(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **OPTS,
    )
    op.bulk_insert(head, [{"id": 1, "last_id": 0, "last_hash": GENESIS}])
    for name, timing in TRIGGERS.items():
        try:
            op.execute(f"CREATE TRIGGER {name} {timing} ON data_audit FOR EACH ROW "
                       "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'data_audit is append-only'")
        except sa.exc.DBAPIError as e:  # e.g. binary logging without SUPER / log_bin_trust_function_creators
            log.warning("data_audit trigger %s not created (%s) – the hash chain still detects changes; "
                        "see docs/data-store.md", name, e.orig)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("data_audit_head")
    op.drop_table("data_audit")
    op.drop_table("datasets")
    op.drop_column("actors", "data_access")
