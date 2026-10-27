"""Several owners per project: `project_owners` holds the co-owners next to `projects.owner_id`.

`projects.owner_id` stays the primary owner (required, shown first, default for follow-up drafts); every row in
`project_owners` gives one more human the same owner rights, owner views and owner notifications.

Revision ID: 0041
Revises: 0040
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)
OPTS = {"mariadb_charset": "utf8mb4", "mariadb_collate": "utf8mb4_unicode_ci", "mariadb_engine": "InnoDB"}


def upgrade() -> None:
    op.create_table(
        "project_owners",
        sa.Column("project_id", UINT, primary_key=True),
        sa.Column("actor_id", UINT, primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_po_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"], name="fk_po_actor", ondelete="CASCADE"),
        sa.Index("idx_project_owners_actor", "actor_id"),
        **OPTS,
    )


def downgrade() -> None:
    op.drop_table("project_owners")
