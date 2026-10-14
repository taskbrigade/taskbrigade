"""Agent owner and public agents; profile switch "Show all agents" for admins

Existing agents get their creator as owner (the human who created their first join code), otherwise
BOOTSTRAP_ADMIN_ACTOR (if it is a human), otherwise the first human admin – and are all set public, so the
assignment choices look the same as before the update.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.config import get_settings

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

UINT = mysql.INTEGER(unsigned=True)


def upgrade() -> None:
    op.add_column("actors", sa.Column("owner_id", UINT, nullable=True))
    op.add_column("actors", sa.Column("is_public", sa.Boolean, nullable=False, server_default=sa.text("0")))
    op.add_column("actors", sa.Column("show_all_agents", sa.Boolean, nullable=False, server_default=sa.text("0")))
    op.create_foreign_key("fk_actors_owner", "actors", "actors", ["owner_id"], ["id"], ondelete="SET NULL")
    conn = op.get_bind()
    fallback = conn.execute(sa.text(
        """SELECT COALESCE(
               (SELECT id FROM actors WHERE name = :admin AND actor_type = 'human'),
               (SELECT MIN(id) FROM actors WHERE role = 'admin' AND actor_type = 'human'))""")
        .bindparams(admin=get_settings().bootstrap_admin_actor.strip())).scalar()
    # Creator = the human who created the agent's first join code (Admin → Neuer Agent); computed here because
    # MariaDB refuses an UPDATE of `actors` that reads `actors` in a subquery.
    creators = dict(conn.execute(sa.text(
        """SELECT j.actor_id, j.created_by FROM join_codes j JOIN actors h ON h.id = j.created_by
           WHERE h.actor_type = 'human' ORDER BY j.id DESC""")).all())
    for (agent_id,) in conn.execute(sa.text("SELECT id FROM actors WHERE actor_type = 'agent'")).all():
        conn.execute(sa.text("UPDATE actors SET owner_id = :owner, is_public = 1 WHERE id = :id"),
                     {"owner": creators.get(agent_id, fallback), "id": agent_id})


def downgrade() -> None:
    op.drop_constraint("fk_actors_owner", "actors", type_="foreignkey")
    op.drop_column("actors", "show_all_agents")
    op.drop_column("actors", "is_public")
    op.drop_column("actors", "owner_id")
