"""Model location of agents (`actors.model_location`) and the switch `privacy_enforce_model_location`

Every existing actor gets `cloud` (conservative, research/konzept-datenzonen.md §9). An existing installation (it has
agents or projects) keeps enforcement off until the admin has worked through the findings; a new installation has no
row and uses the default (on).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

SETTING = "privacy_enforce_model_location"


def upgrade() -> None:
    op.add_column("actors", sa.Column("model_location", sa.Enum("cloud", "local", "none", name="model_location"),
                                      nullable=False, server_default="cloud"))
    conn = op.get_bind()
    existing = conn.execute(sa.text("SELECT EXISTS(SELECT 1 FROM actors WHERE actor_type = 'agent') "
                                    "OR EXISTS(SELECT 1 FROM projects)")).scalar()
    if existing:
        conn.execute(sa.text("INSERT INTO app_settings (setting_key, setting_value, is_secret) "
                             "VALUES (:k, 'false', 0) ON DUPLICATE KEY UPDATE setting_key = setting_key"),
                     {"k": SETTING})


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM app_settings WHERE setting_key = :k").bindparams(k=SETTING))
    op.drop_column("actors", "model_location")
