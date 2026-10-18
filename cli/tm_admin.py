"""Emergency and operations CLI on the app server (run as user `taskbrigade`)."""
import os
import sys
import tempfile
from contextlib import contextmanager

import typer

from app import crypto, dbconfig
from app.auth import hash_password
from app.config import get_settings
from app.db import make_engine, probe, upgrade_schema
from cli import admin_config as cfgfile
from cli.i18n import _
from cli.gitea_hook import register as register_gitea_hook
from cli.runner_admin import register as register_runner
from cli.workflow_admin import register as register_workflows

app = typer.Typer(help="TaskBrigade-Administration", no_args_is_help=True)
register_workflows(app)
config_app = typer.Typer(help=_("Read and change the server configuration (env file of the service)"),
                         no_args_is_help=True)
app.add_typer(config_app, name="config")
data_app = typer.Typer(help=_("Data zones: reports, data store retention and audit"), no_args_is_help=True)
app.add_typer(data_app, name="data")


@app.command("init-key")
def init_key():
    """Create master.key if absent."""
    s = get_settings()
    created = crypto.create_master_key(s.tbr_state_dir)
    typer.echo(f"{crypto.key_path(s.tbr_state_dir)} {'erzeugt' if created else 'existiert bereits'}")


@app.command("init-data-key", help=_("Create the data store key (KEK, TBR_DATA_KEY_FILE) if absent. Never "
                                      "overwrites a key: a new key would make every dataset unreadable."))
def init_data_key():
    """Create the KEK for the data store (docs/data-store.md)."""
    from pathlib import Path

    from app import data_crypto

    path = Path(get_settings().tbr_data_key_file)
    try:
        created = data_crypto.create_kek(path)
    except OSError as e:
        _fail(_("%(path)s cannot be written: %(error)s", path=path, error=e.strerror or e))
    if created:
        typer.echo(_("%(path)s created (mode 0640). Give the service group read access (e.g. chgrp taskbrigade) "
                     "and back the key up separately from the data and the database.", path=path))
    else:
        typer.echo(_("%(path)s already exists – left unchanged", path=path))


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
    if r.schema_current:
        from sqlalchemy.orm import Session
        from app.services.workflows.tick import status as tick_status

        with Session(engine) as session:
            state = tick_status(session)
        if state["enabled"] and state["stale"]:
            typer.echo(f"Warnung: Workflows aktiv, aber letzter Tick älter als 5 Minuten "
                       f"(zuletzt: {state['last'] or '–'})")
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


def _format_scan(rows: list[dict]) -> str:
    if not rows:
        return _("No spreadsheet or archive attachments in local-only projects.")
    lines = [_("Spreadsheet and archive attachments in local-only projects (report only, nothing deleted):")]
    for row in rows:
        lines.append(_("%(project)s %(nr)s %(filename)s %(path)s %(repo)s",
                       project=row["project"], nr=row["task_nr"], filename=row["filename"], path=row["path"],
                       repo=row["repo"] or "-"))
    lines.append(_("%(count)d attachment(s). They are already in Git history: clean the history, keep the "
                   "repository private, or check who can clone it.", count=len(rows)))
    return "\n".join(lines)


@data_app.command(
    "scan-attachments",
    help=_("List spreadsheet and archive task attachments in local-only projects. Report only; nothing is deleted."))
def scan_attachments():
    """List spreadsheet/archive attachments already stored for local-only projects."""
    from app.db import db
    from app.services.attachments import scan_blocked_attachments

    result = db.init_from_settings(get_settings())
    if not db.ready:
        _fail(_("Database not ready: %(error)s", error=result.error))
    with db.session() as session:
        rows = scan_blocked_attachments(session)
    typer.echo(_format_scan(rows))


def _data_db():
    from app.db import db

    result = db.init_from_settings(get_settings())
    if not db.ready:
        _fail(_("Database not ready: %(error)s", error=result.error))
    return db


@data_app.command("retention", help=_("Run the data store retention job now (reminders, deletion of datasets past "
                                      "their retention date). Safe to run from a timer next to the app's own job."))
def data_retention():
    from app.services.datasets import retention_sweep

    with _data_db().session() as session:
        result = retention_sweep(session)
        session.commit()
    typer.echo(_("Retention: %(warned)d reminder(s), %(deleted)d deleted, %(failed)d failed%(note)s",
                 warned=result.get("warned", 0), deleted=result.get("deleted", 0), failed=result.get("failed", 0),
                 note=_(" (another run holds the lock)") if result.get("skipped") else ""))
    if result.get("failed"):
        raise typer.Exit(1)


audit_app = typer.Typer(help=_("Data audit (append-only, hash-chained)"), no_args_is_help=True)
data_app.add_typer(audit_app, name="audit")


@audit_app.command("verify", help=_("Check the hash chain of the data audit; exit code 1 if it is broken."))
def audit_verify():
    from app.services import data_audit

    with _data_db().session() as session:
        verdict = data_audit.verify(session)
        triggers = data_audit.triggers_present(session)
    if verdict.ok:
        typer.echo(_("Data audit chain ok: %(count)d entries, last hash %(hash)s", count=verdict.count,
                     hash=verdict.last_hash))
    else:
        reasons = {"hash": _("the entry was changed"), "prev_hash": _("an entry before it was removed or inserted"),
                   "head": _("entries at the end were removed")}
        typer.echo(_("Data audit chain BROKEN at entry %(id)s: %(reason)s (%(count)d entries before it are ok)",
                     id=verdict.bad_id or "-", reason=reasons.get(verdict.reason, verdict.reason),
                     count=verdict.count), err=True)
    missing = sorted(set(data_audit.TRIGGERS) - set(triggers))
    if missing:
        typer.echo(_("Warning: append-only triggers missing (%(names)s) – see docs/data-store.md",
                     names=", ".join(missing)), err=True)
    if not verdict.ok:
        raise typer.Exit(1)


register_gitea_hook(data_app, _data_db)
register_runner(app, _data_db)


# --- Native SFTP inbox (research/konzept-datenzonen.md §4, §9) ---------------------------------------------------

sftp_app = typer.Typer(help=_("Native SFTP inbox: deliverers, public keys and the import from the upload "
                              "folders"), no_args_is_help=True)
data_app.add_typer(sftp_app, name="sftp-user")


@contextmanager
def _guard():
    """Turn an `AppError` from the service layer into a clear message and exit code 1 (like `_fail`), instead of
    letting Typer print a traceback."""
    from app.errors import AppError

    try:
        yield
    except AppError as e:
        _fail(str(e))
    except OSError as e:
        _fail(str(e))


@sftp_app.command("add", help=_("Create a chrooted SFTP deliverer for a project (system user, no shell). Needs "
                                "root and TBR_DATA_ZONES=1 on the host."))
def sftp_user_add(
    username: str = typer.Argument(..., help=_("System user name, e.g. demo-export")),
    project: str = typer.Option(..., "--project", help=_("Project key the deliveries belong to")),
    key: list[str] = typer.Option(None, "--key", help=_("Public key line or path to a .pub file (repeatable)")),
    name: str = typer.Option(None, "--name", help=_("Label shown in the list, e.g. \"Accounting export\"")),
    no_provision: bool = typer.Option(False, "--no-provision", help=_("Only write the database row (tests, "
                                                                     "prepared hosts)")),
):
    """Create an SFTP deliverer and, unless --no-provision, the system user with the chroot directory."""
    from app.services import data_sftp

    with _guard(), _data_db().session() as session:
        account = data_sftp.create(session, username, project, name=name, key=None)
        for value in key or []:
            data_sftp.add_key(session, account, value)
        if not no_provision:
            data_sftp.ensure_provisioned(session, account, data_sftp.keys_of(session, account))
        session.commit()
        folder = data_sftp.upload_path(account.username)
    typer.echo(_("SFTP user %(user)s created for project %(project)s (upload folder %(folder)s)",
                 user=username, project=project.upper(), folder=folder))
    if not key:
        typer.echo(_("No key yet – add one with: tbr-admin data sftp-user key add %(user)s <pubkey>", user=username))


@sftp_app.command("remove", help=_("Remove an SFTP deliverer. Without --purge only the database row goes away; "
                                   "--purge also deletes the system user and the chroot folder."))
def sftp_user_remove(
    username: str = typer.Argument(...),
    purge: bool = typer.Option(False, "--purge", help=_("Also delete the system user and the chroot directory")),
):
    from app.services import data_sftp

    steps: list[str] = []
    with _guard(), _data_db().session() as session:
        data_sftp.remove(session, username, purge=purge)
        session.commit()
    if purge:
        with _guard():
            steps = data_sftp.purge_system(username)
    typer.echo(_("SFTP user %(user)s removed%(steps)s", user=username,
                 steps=_(" (system: %(steps)s)", steps=", ".join(steps)) if steps else ""))
    if not purge:
        typer.echo(_("The system user and %(folder)s were kept – clean them up with --purge or by hand",
                     folder=data_sftp.account_dir(username)))


@sftp_app.command("list", help=_("List SFTP deliverers with project, keys and (with --pending) waiting files."))
def sftp_user_list(pending: bool = typer.Option(False, "--pending", help=_("Also show files waiting in the "
                                                                               "upload folders"))):
    from app.models import Project
    from app.services import data_import, data_sftp

    with _guard(), _data_db().session() as session:
        accounts = data_sftp.all_accounts(session)
        if not accounts:
            typer.echo(_("No SFTP users configured"))
            return
        for account in accounts:
            project = session.get(Project, account.project_id)
            keys = data_sftp.keys_of(session, account)
            marker = "*" if account.enabled else "-"
            typer.echo(f"{marker} {account.username}  "
                       f"{project.project_key if project else '?'}  "
                       f"{account.name or '-'}  "
                       f"{len(keys)} key(s)")
            for key in keys:
                typer.echo(f"    {key.fingerprint}  {key.comment or ''}")
        if pending:
            typer.echo("")
            for report in data_import.status(session)["users"]:
                files = ", ".join(report["pending"]) or "-"
                typer.echo(_("%(user)s (%(project)s): %(count)d file(s) waiting: %(files)s",
                             user=report["user"], project=report["project"], count=len(report["pending"]),
                             files=files))


key_app = typer.Typer(help=_("Public keys of an SFTP deliverer"), no_args_is_help=True)
sftp_app.add_typer(key_app, name="key")


@key_app.command("add")
def sftp_key_add(username: str = typer.Argument(...), key: str = typer.Argument(..., help=_("Key line or .pub "
                                                                                             "file path")),
                 no_provision: bool = typer.Option(False, "--no-provision")):
    from app.services import data_sftp

    with _guard(), _data_db().session() as session:
        account = data_sftp.get(session, username)
        added = data_sftp.add_key(session, account, key)
        if not no_provision:
            data_sftp.ensure_provisioned(session, account, data_sftp.keys_of(session, account))
        session.commit()
    typer.echo(_("Key %(fingerprint)s added for %(user)s", fingerprint=added.fingerprint, user=username))


@key_app.command("remove")
def sftp_key_remove(username: str = typer.Argument(...), fingerprint: str = typer.Argument(...,
                                                                                          help=_("SHA256:… (or "
                                                                                                 "its end)"))):
    from app.services import data_sftp

    with _guard(), _data_db().session() as session:
        account = data_sftp.get(session, username)
        data_sftp.remove_key(session, account, fingerprint)
        data_sftp.write_authorized_keys(account, data_sftp.keys_of(session, account))
        session.commit()
    typer.echo(_("Key removed from %(user)s", user=username))


@key_app.command("list")
def sftp_key_list(username: str = typer.Argument(...)):
    from app.services import data_sftp

    with _guard(), _data_db().session() as session:
        keys = data_sftp.keys_of(session, data_sftp.get(session, username))
    if not keys:
        typer.echo(_("%(user)s has no key yet", user=username))
        return
    for key in keys:
        typer.echo(f"{key.fingerprint}  {key.key_type}  {key.comment or ''}")


@sftp_app.command("provision", help=_("Re-create the system user, the chroot and `authorized_keys` from the "
                                       "database (after an OS reinstall or a hand-edited key file)."))
def sftp_provision(username: str = typer.Option(None, "--user", help=_("One user; default: all accounts"))):
    from app.services import data_sftp

    with _guard(), _data_db().session() as session:
        accounts = [data_sftp.get(session, username)] if username else data_sftp.all_accounts(session)
        for account in accounts:
            data_sftp.ensure_provisioned(session, account, data_sftp.keys_of(session, account))
            typer.echo(_("%(user)s provisioned", user=account.username))
        session.commit()


@sftp_app.command("import", help=_("Import the files waiting in the SFTP upload folders into the data store "
                                   "(encrypt, audit, remove the plaintext). Runs the systemd path unit's job."))
def sftp_import():
    from app.services import data_import

    with _data_db().session() as session:
        result = data_import.scan(session)
    if result.get("skipped") == "disabled":
        typer.echo(_("The data store is switched off – nothing imported"))
        return
    if result.get("skipped") == "locked":
        typer.echo(_("Another import run holds the lock – nothing imported"))
        return
    typer.echo(_("SFTP import: %(imported)d imported, %(failed)d failed, %(users)d user(s) with files",
                 imported=result.get("imported", 0), failed=result.get("failed", 0), users=result.get("users", 0)))
    for item in result.get("files", []):
        note = _(" (duplicate of %(key)s)", key=item["duplicate"]) if item.get("duplicate") else ""
        typer.echo(f"  {item['dataset']}  {item['user']}/{item['name']}  {item['size_bytes']} B{note}")
    if result.get("failed"):
        raise typer.Exit(1)


if __name__ == "__main__":
    sys.exit(app())
