"""Logic behind Admin → Database: test, save, and change the MariaDB password."""
from dataclasses import dataclass

from sqlalchemy import text

from app import dbconfig
from app.i18n import _
from app.config import Settings
from app.db import ProbeResult, db, make_engine, probe


@dataclass
class DbForm:
    host: str
    port: int
    database: str
    user: str
    password: str  # Empty means unchanged.
    change_in_db: bool = False


def effective_config(settings: Settings, form: DbForm) -> dbconfig.DbConfig:
    """Connection configuration: use the old password when changing it in MariaDB;
    otherwise use the new or stored password."""
    stored = dbconfig.load(settings)
    password = stored.password if (form.change_in_db or not form.password) else form.password
    return dbconfig.DbConfig(form.host.strip(), form.port, form.database.strip(), form.user.strip(), password)


def test(settings: Settings, form: DbForm) -> ProbeResult:
    cfg = effective_config(settings, form)
    if not cfg.password:
        return ProbeResult(False, _("No password provided"))
    engine = make_engine(cfg.url())
    try:
        return probe(engine)
    finally:
        engine.dispose()


@dataclass
class SaveOutcome:
    ok: bool
    message: str
    probe: ProbeResult | None = None


def save(settings: Settings, form: DbForm) -> SaveOutcome:
    """Save only after a successful connection test, then rebuild the engine."""
    if dbconfig.from_env(settings):
        return SaveOutcome(False, _("The connection is set by TBR_DB_* environment variables (TBR_DB_PASSWORD) – "
                                    "change it there and restart."))
    cfg = effective_config(settings, form)
    result = test(settings, form)
    if not result.ok:
        return SaveOutcome(False, _("Not saved – connection test failed: %(error)s", error=result.error), result)

    if form.change_in_db:
        if not form.password:
            return SaveOutcome(False, _( 'Enter a new password to change the MariaDB password.' ), result)
        engine = make_engine(cfg.url())
        try:
            with engine.begin() as conn:
                conn.execute(text("SET PASSWORD = PASSWORD(:pw)"), {"pw": form.password})
        except Exception as e:
            return SaveOutcome(False, _("MariaDB password change failed: %(error)s", error=type(e).__name__), result)
        finally:
            engine.dispose()
        new_cfg = dbconfig.DbConfig(cfg.host, cfg.port, cfg.database, cfg.user, form.password)
        engine = make_engine(new_cfg.url())
        try:
            new_result = probe(engine)
        finally:
            engine.dispose()
        if not new_result.ok:
            return SaveOutcome(
                False,
                _("WARNING: The password changed in MariaDB, but sign-in with the new password fails (%(error)s). db.json still has the old password – check it manually (for example, multiple host accounts for the same user).", error=new_result.error),
                new_result,
            )
        cfg = new_cfg

    dbconfig.save(settings, cfg)
    applied = db.configure(cfg.url())
    return SaveOutcome(True, _("Saved and connection reestablished."), applied)
