"""Project owner and 'member' role (multiple humans working with TaskBrigade)

Existing projects get DEFAULT_PROJECT_OWNER (from .env, if set and present) as their owner;
otherwise their creator.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.config import get_settings

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)


def upgrade() -> None:
    op.execute("ALTER TABLE actors MODIFY role ENUM('admin','orchestrator','worker','member') "
               "NOT NULL DEFAULT 'worker'")
    op.add_column("projects", sa.Column("owner_id", UINT, nullable=True))
    op.execute(sa.text("""UPDATE projects SET owner_id = COALESCE(
        (SELECT id FROM actors WHERE name = :owner AND actor_type = 'human'), created_by)""")
               .bindparams(owner=get_settings().default_project_owner.strip()))
    op.alter_column("projects", "owner_id", existing_type=UINT, nullable=False)
    op.create_foreign_key("fk_projects_owner", "projects", "actors", ["owner_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_projects_owner", "projects", type_="foreignkey")
    op.drop_column("projects", "owner_id")
    op.execute("UPDATE actors SET role = 'worker' WHERE role = 'member'")
    op.execute("ALTER TABLE actors MODIFY role ENUM('admin','orchestrator','worker') NOT NULL DEFAULT 'worker'")
