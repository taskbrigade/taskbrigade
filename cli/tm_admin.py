"""Emergency and operations CLI on the app server (run as user `taskbrigade`)."""
import os
import sys
import tempfile

import typer

from app import crypto, dbconfig
from app.auth import hash_password
from app.config import get_settings
from app.db import make_engine, probe, upgrade_schema
from cli import admin_config as cfgfile
from cli.i18n import _

app = typer.Typer(help="TaskBrigade-Administration", no_args_is_help=True)
config_app = typer.Typer(help=_("Read and change the server configuration (env file of the service)"),
                         no_args_is_help=True)
app.add_typer(config_app, name="config")


@app.command("init-key")
def init_key():
    """Create master.key if absent."""
    s = get_settings()
    created = crypto.create_master_key(s.tbr_state_dir)
    typer.echo(f"{crypto.key_path(s.tbr_state_dir)} {'erzeugt' if created else 'existiert bereits'}")


@app.command("hash-password")
def hash_pw():
    """Generate an Argon2 hash for BOOTSTRAP_ADMIN_PASSWORD_HASH."""
    pw = typer.prompt("Passwort", hide_input=True, confirmation_prompt=True)
    typer.echo(hash_password(pw))


@app.command("set-db-password")
def set_db_password(
    host: str = typer.Option(None), port: int = typer.Option(None),
    database: str = typer.Option(None), user: str = typer.Option(None),
    force: bool = typer.Option(False, help="Auch speichern, wenn der Verbindungstest fehlschlägt"),
):
    """Set DB credentials in db.json (prompt for the password; never accept it as an argument)."""
    s = get_settings()
    cfg = dbconfig.load(s)
    cfg.host, cfg.port = host or cfg.host, port or cfg.port
    cfg.database, cfg.user = database or cfg.database, user or cfg.user
    cfg.password = typer.prompt(f"Passwort für {cfg.user}@{cfg.host}:{cfg.port}", hide_input=True)
    engine = make_engine(cfg.url())
    try:
        result = probe(engine)
    finally:
        engine.dispose()
    if result.ok:
        typer.echo(f"Verbindung ok: {result.server_version}, Schema {result.current_revision or 'leer'}")
    else:
        typer.echo(f"Verbindung fehlgeschlagen: {result.error}", err=True)
        if not force:
            raise typer.Exit(1)
    dbconfig.save(s, cfg)
    typer.echo("db.json gespeichert. Die laufende App übernimmt die Daten beim nächsten DB-Health-Check (≤ 1 min).")


@app.command("reset-admin-password")
def reset_admin_password(name: str = typer.Argument(None, help="Akteur (Default: BOOTSTRAP_ADMIN_ACTOR)")):
    """Reset a human admin's UI password and reactivate them if needed."""
    from app.db import db
    from app.services import actors

    s = get_settings()
    db.init_from_settings(s)
    if not db.ready:
        typer.echo(f"DB nicht bereit: {db.last_probe.error}", err=True)
        raise typer.Exit(1)
    pw = typer.prompt("Neues Passwort", hide_input=True, confirmation_prompt=True)
    with db.session() as session:
        actor = actors.get(session, name or s.bootstrap_admin_actor)
        if actor.role != "admin":
            typer.echo(f"{actor.name} ist kein Admin", err=True)
            raise typer.Exit(1)
        actors.set_password(session, actor, pw)
        actor.is_active = True
        session.commit()
        typer.echo(f"Passwort für {actor.name} gesetzt.")
    db.dispose()


@app.command("print-db-cnf")
def print_db_cnf():
    """Write a temporary MariaDB option file (0600) and print its path for backup.sh."""
    cfg = dbconfig.load(get_settings())
    if not cfg.password:
        typer.echo("Kein DB-Passwort gesetzt", err=True)
        raise typer.Exit(1)
    pw = cfg.password.replace("\\", "\\\\").replace('"', '\\"')
    fd, path = tempfile.mkstemp(prefix="tm-db-", suffix=".cnf")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"[client]\nhost={cfg.host}\nport={cfg.port}\nuser={cfg.user}\npassword=\"{pw}\"\n")
    typer.echo(path)


@app.command("db-name")
def db_name():
    """Print the configured database name for backup.sh; it does not belong in the option file."""
    typer.echo(dbconfig.load(get_settings()).database)


@app.command("db-status")
def db_status():
    """Check the connection and Alembic revision."""
    s = get_settings()
    cfg = dbconfig.load(s)
    typer.echo(f"Konfiguration: {cfg.public()}")
    if not (cfg.password or s.database_url):
        raise typer.Exit(1)
    engine = make_engine(s.database_url or cfg.url())
    try:
        r = probe(engine)
    finally:
        engine.dispose()
    typer.echo(f"ok={r.ok} version={r.server_version} skip_locked={r.skip_locked} "
               f"schema={r.current_revision} head={r.head_revision} error={r.error}")
    raise typer.Exit(0 if r.schema_current else 1)


@app.command("upgrade")
def upgrade():
    """Run `alembic upgrade head` against the configured DB."""
    s = get_settings()
    cfg = dbconfig.load(s)
    engine = make_engine(s.database_url or cfg.url())
    try:
        typer.echo(f"Schema auf {upgrade_schema(engine)}")
    finally:
        engine.dispose()


def _fail(message: str):
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _config_source() -> tuple:
    """(path, values); path None = container environment."""
    path = cfgfile.env_file()
    if path is None:
        typer.echo(cfgfile.docker_hint(), err=True)
        return None, cfgfile.environment_values()
    try:
        return path, cfgfile.read_values(path)
    except cfgfile.ConfigError as e:
        _fail(str(e))


@config_app.command("list", help=_("Show every setting with its value from the env file or its default; secrets "
                                    "are masked."))
def config_list():
    path, values = _config_source()
    if path:
        typer.echo(f"# {path}")
    for key in sorted(cfgfile.known_keys().values(), key=lambda k: k.name):
        if key.name in values:
            typer.echo(f"{key.name}={cfgfile.shown(key, values[key.name])}")
        else:
            typer.echo(f"{key.name}={cfgfile.shown(key, key.default)}  " + _("(default)"))


@config_app.command("get", help=_("Print one setting; secrets are only shown as ******** or empty."))
def config_get(key: str = typer.Argument(..., help=_("Setting, e.g. GITEA_URL"))):
    try:
        k = cfgfile.resolve_key(key)
    except cfgfile.ConfigError as e:
        _fail(str(e))
    _path, values = _config_source()
    typer.echo(cfgfile.shown(k, values.get(k.name, k.default)))


def _offer_restart(restart: bool | None) -> None:
    if restart is None:
        if not sys.stdin.isatty():
            typer.echo(_("Restart the service to apply it: sudo systemctl restart %(service)s",
                         service=cfgfile.service_name()))
            return
        restart = typer.confirm(_("Restart %(service)s now to apply the change?", service=cfgfile.service_name()),
                                default=True)
    if not restart:
        typer.echo(_("Not restarted. The change applies with: sudo systemctl restart %(service)s",
                     service=cfgfile.service_name()))
        return
    ok, detail = cfgfile.restart_service()
    if ok:
        typer.echo(_("%(service)s restarted.", service=cfgfile.service_name()))
    else:
        _fail(_("Restart failed: %(detail)s", detail=detail))


def _change(key: str, value: str | None, restart: bool | None) -> None:
    try:
        k = cfgfile.resolve_key(key)
    except cfgfile.ConfigError as e:
        _fail(str(e))
    path = cfgfile.env_file()
    if path is None:
        _fail(cfgfile.docker_hint())
    try:
        changed = cfgfile.change(path, k, value)
    except cfgfile.ConfigError as e:
        _fail(str(e))
    if not changed:
        typer.echo(_("%(key)s unchanged in %(path)s.", key=k.name, path=path))
        return
    if value is None and k.default:
        typer.echo(_("%(key)s removed from %(path)s (default: %(default)s).", key=k.name, path=path,
                     default=cfgfile.shown(k, k.default)))
    elif value is None:
        typer.echo(_("%(key)s removed from %(path)s.", key=k.name, path=path))
    else:
        typer.echo(_("%(key)s=%(value)s written to %(path)s.", key=k.name, value=cfgfile.shown(k, value),
                     path=path))
    _offer_restart(restart)


_RESTART_HELP = _("Restart the service afterwards (default: ask when interactive)")


@config_app.command("set", help=_("Set a setting in the env file the service reads (needs sudo on a native "
                                   "installation). Unknown keys and invalid values are refused."))
def config_set(
    key: str = typer.Argument(..., help=_("Setting, e.g. GITEA_URL")),
    value: str = typer.Argument(None, help=_("New value; omit it to type it (hidden for secrets)")),
    restart: bool = typer.Option(None, "--restart/--no-restart", help=_RESTART_HELP),
):
    if value is None:
        try:
            k = cfgfile.resolve_key(key)
        except cfgfile.ConfigError as e:
            _fail(str(e))
        value = typer.prompt(k.name, hide_input=k.secret, default="", show_default=False)
    _change(key, value, restart)


@config_app.command("unset", help=_("Remove a setting from the env file so its default applies again."))
def config_unset(
    key: str = typer.Argument(..., help=_("Setting, e.g. GITEA_URL")),
    restart: bool = typer.Option(None, "--restart/--no-restart", help=_RESTART_HELP),
):
    _change(key, None, restart)


if __name__ == "__main__":
    sys.exit(app())
