"""Container start (Docker image entrypoint): prepare state, wait for MariaDB, migrate, then exec Uvicorn.

The native installation does not use this module; it keeps setup mode, db.json and `tbr-admin upgrade`.
"""
import logging
import os
import secrets
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import crypto, dbconfig
from app.config import Settings, get_settings, getenv
from app.db import ProbeResult, make_engine, probe, upgrade_schema
from app.logging_setup import setup_logging

log = logging.getLogger("taskbrigade.container")

SECRET_FILE = "secret.key"


class StartupError(RuntimeError):
    pass


def ensure_secret_key(settings: Settings, environ: MutableMapping[str, str]) -> bool:
    """Without SECRET_KEY, persist a random key in the state volume so sessions survive restarts.
    Return True if a new key was created."""
    if settings.secret_key:
        return False
    path = settings.tbr_state_dir / SECRET_FILE
    created = False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
        created = True
    environ["SECRET_KEY"] = path.read_text().strip()
    return created


def database_url(settings: Settings) -> str:
    """DATABASE_URL or TBR_DB_* with TBR_DB_PASSWORD; the container has no setup mode."""
    if settings.database_url:
        return settings.database_url
    if not dbconfig.from_env(settings):
        raise StartupError("TBR_DB_PASSWORD is not set – the container reads the database connection from TBR_DB_*")
    return dbconfig.load(settings).url()


def wait_for_db(engine: Engine, timeout: float, interval: float = 2.0, *,
                probe_fn: Callable[[Engine], ProbeResult] = probe,
                sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> ProbeResult:
    """Probe until the server answers; raise StartupError after `timeout` seconds."""
    deadline = clock() + timeout
    attempt = 0
    while True:
        attempt += 1
        result = probe_fn(engine)
        if result.ok:
            log.info("db_reachable", extra={"attempt": attempt, "server_version": result.server_version})
            return result
        if clock() >= deadline:
            raise StartupError(f"Database not reachable after {timeout:.0f} s: {result.error}")
        log.info("db_waiting", extra={"attempt": attempt, "error": result.error})
        sleep(interval)


def migrate(engine: Engine, before: ProbeResult) -> str | None:
    """Run `alembic upgrade head` unless the schema is already current."""
    if before.schema_current:
        log.info("schema_current", extra={"revision": before.current_revision})
        return before.current_revision
    head = upgrade_schema(engine)
    log.info("schema_upgraded", extra={"from": before.current_revision, "to": head})
    return head


def ensure_initial_admin(engine: Engine, settings: Settings) -> bool:
    """Set TBR_INITIAL_ADMIN_PASSWORD on BOOTSTRAP_ADMIN_ACTOR once, while that actor has no password.
    A password changed later in the UI is never overwritten. Return True if the password was set."""
    password = settings.tbr_initial_admin_password
    if not password:
        return False
    from app.errors import AppError
    from app.models import Actor
    from app.services import actors

    with Session(engine) as session:
        actor = session.scalar(select(Actor).where(Actor.name == settings.bootstrap_admin_actor))
        if actor is None:
            log.warning("initial_admin_missing", extra={"actor": settings.bootstrap_admin_actor})
            return False
        if actor.password_hash:
            return False
        try:
            actors.set_password(session, actor, password)
        except AppError as e:
            raise StartupError(f"TBR_INITIAL_ADMIN_PASSWORD rejected: {e.message}") from e
        actor.is_active = True
        session.commit()
    log.info("initial_admin_password_set", extra={"actor": settings.bootstrap_admin_actor})
    return True


def prepare(settings: Settings, environ: MutableMapping[str, str]) -> None:
    """Everything before Uvicorn starts; raises StartupError with an operator-readable message."""
    state: Path = settings.tbr_state_dir
    if crypto.create_master_key(state):
        log.info("master_key_created", extra={"path": str(crypto.key_path(state))})
    if ensure_secret_key(settings, environ):
        log.info("secret_key_created", extra={"path": str(state / SECRET_FILE)})
    engine = make_engine(database_url(settings))
    try:
        before = wait_for_db(engine, settings.tbr_db_wait_seconds)
        migrate(engine, before)
        ensure_initial_admin(engine, settings)
    finally:
        engine.dispose()


def uvicorn_argv(environ: MutableMapping[str, str]) -> list[str]:
    # --no-proxy-headers: the network policy relies on the direct peer, as in the systemd unit.
    return ["uvicorn", "app.main:app", "--host", getenv("BIND_HOST", "0.0.0.0", environ),
            "--port", getenv("PORT", "8090", environ), "--workers", "1", "--no-proxy-headers"]


def main() -> None:
    setup_logging()
    try:
        prepare(get_settings(), os.environ)
    except StartupError as e:
        log.error("startup_failed", extra={"error": str(e)})
        raise SystemExit(1) from e
    argv = uvicorn_argv(os.environ)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
