"""Engine management with setup mode.

The app starts even without an available DB. Then `db.status` is not `ready`,
and only login, /admin/database, and /healthz are usable. When new credentials
are set, the engine is replaced at runtime without a restart.
"""
import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import dbconfig
from app.i18n import _
from app.config import Settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


class DbUnavailable(RuntimeError):
    pass


@dataclass
class ProbeResult:
    ok: bool
    error: str | None = None
    server_version: str | None = None
    skip_locked: bool = False
    current_revision: str | None = None
    head_revision: str | None = None

    @property
    def schema_current(self) -> bool:
        return self.ok and self.current_revision is not None and self.current_revision == self.head_revision


def make_engine(url: str) -> Engine:
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"connect_timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _utc(dbapi_conn, _record):  # Keep all timestamps in UTC (CURRENT_TIMESTAMP defaults).
        with dbapi_conn.cursor() as cur:
            cur.execute("SET time_zone = '+00:00'")

    return engine


def supports_skip_locked(version: str) -> bool:
    m = re.match(r"(\d+)\.(\d+)", version)
    if not m:
        return False
    major, minor = int(m[1]), int(m[2])
    if "mariadb" in version.lower():
        return (major, minor) >= (10, 6)
    return major >= 8  # MySQL


def alembic_head() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def probe(engine: Engine) -> ProbeResult:
    """Test the connection; return the version, SKIP LOCKED support, and Alembic revision."""
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar_one()
            try:
                current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:
                current = None
        return ProbeResult(True, None, version, supports_skip_locked(version), current, alembic_head())
    except Exception as e:
        return ProbeResult(False, _safe_error(e), head_revision=alembic_head())


def _safe_error(e: Exception) -> str:
    """Return an error message without the connection URL or password."""
    orig = getattr(e, "orig", None)
    if orig is not None and getattr(orig, "args", None):
        args = orig.args
        return f"{args[0]}: {args[1]}" if len(args) > 1 else str(args[0])
    return type(e).__name__


class Database:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.engine: Engine | None = None
        self._sessionmaker: sessionmaker | None = None
        self.last_probe = ProbeResult(False, None)

    @property
    def ready(self) -> bool:
        return self.engine is not None and self.last_probe.schema_current

    @property
    def skip_locked(self) -> bool:
        return self.last_probe.skip_locked

    def init_from_settings(self, settings: Settings) -> ProbeResult:
        if settings.database_url:
            return self.configure(settings.database_url)
        try:
            cfg = dbconfig.load(settings)
        except Exception as e:
            self.last_probe = ProbeResult(False, _("Cannot read db.json: %(error)s", error=e))
            return self.last_probe
        if not cfg.password:
            self.last_probe = ProbeResult(False, _("No database password set (setup mode)"))
            return self.last_probe
        return self.configure(cfg.url())

    def configure(self, url: str) -> ProbeResult:
        """Build a new engine and discard the old one."""
        new_engine = make_engine(url)
        result = probe(new_engine)
        with self._lock:
            old, self.engine = self.engine, new_engine
            self._sessionmaker = sessionmaker(new_engine, expire_on_commit=False)
            self.last_probe = result
        if old is not None:
            old.dispose()
        log.info("db_configured", extra={"ok": result.ok, "schema_current": result.schema_current})
        return result

    def recheck(self) -> ProbeResult:
        if self.engine is None:
            return self.last_probe
        self.last_probe = probe(self.engine)
        return self.last_probe

    def session(self) -> Session:
        if not self.ready or self._sessionmaker is None:
            raise DbUnavailable(self.last_probe.error or _( 'Schema is not up to date' ))
        return self._sessionmaker()

    def dispose(self) -> None:
        if self.engine is not None:
            self.engine.dispose()


db = Database()


def get_session() -> Iterator[Session]:
    """FastAPI-Dependency."""
    session = db.session()
    try:
        yield session
    finally:
        session.close()


def upgrade_schema(engine: Engine) -> str | None:
    """Run `alembic upgrade head` through the supplied engine."""
    from alembic import command

    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    return alembic_head()
