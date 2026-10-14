"""Alembic environment. Connect like the app: DATABASE_URL, otherwise db.json.
When invoked by the app, use the connection in `config.attributes`."""
from alembic import context

from app import dbconfig
from app.config import get_settings
from app.db import make_engine
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    settings = get_settings()
    if settings.database_url:
        return settings.database_url
    cfg = dbconfig.load(settings)
    if not cfg.password:
        raise SystemExit("Kein DB-Passwort gesetzt – zuerst in der UI oder per `tm-admin set-db-password`.")
    return cfg.url()


def run_migrations_offline() -> None:
    context.configure(url="mariadb+pymysql://", target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = make_engine(_url())
    try:
        with engine.begin() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
