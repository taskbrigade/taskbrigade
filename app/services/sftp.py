"""SFTP storage: global settings (`app_settings` keys `sftp_*`), one SFTP login per agent (password Fernet-encrypted
like Gitea tokens), delivery to the agent (`GET /agents/me/sftp-credentials`), connection tests and the task note
for uploads. The transfer itself runs on the agent machine (`cli/sftp.py`, `tbr sftp`, MCP `sftp_put`)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crypto
from app.config import get_settings
from app.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.i18n import _
from app.models import Actor, Task
from app.services import settings_store
from app.services.state_machine import add_comment, utcnow
from cli import sftp as client

MAX_NOTE_FILES = 200


def _fernet():
    return crypto.load_fernet(get_settings().tbr_state_dir)


def config(session: Session) -> dict:
    """Global settings with defaults; `port` as int."""
    get = settings_store.get
    port = get(session, "sftp_port") or ""
    return {"enabled": settings_store.get_bool(session, "sftp_enabled"), "host": get(session, "sftp_host") or "",
            "port": int(port) if port.isdigit() else client.DEFAULT_PORT,
            "host_key": get(session, "sftp_host_key") or "",
            "base_dir": get(session, "sftp_base_dir") or client.DEFAULT_BASE_DIR,
            "pattern": get(session, "sftp_path_pattern") or client.DEFAULT_PATTERN}


def task_dir(cfg: dict, task: Task) -> str:
    return client.task_dir(cfg["pattern"], cfg["base_dir"], task.project.project_key, task.task_nr)


# --- Per agent ----------------------------------------------------------------------------------

def set_credentials(session: Session, actor: Actor, username: str, password: str) -> None:
    username, password = (username or "").strip(), password or ""
    if actor.actor_type != "agent":
        raise Unprocessable(_("Only agents have an SFTP login"))
    if not username or not password:
        raise Unprocessable(_("SFTP user and password are required"))
    other = session.scalar(select(Actor).where(Actor.sftp_username == username, Actor.id != actor.id))
    if other:
        raise Conflict(_("SFTP user '%(username)s' is already assigned to '%(actor)s'", username=username,
                         actor=other.name))
    actor.sftp_username = username
    actor.sftp_password_enc = crypto.encrypt(_fernet(), password)
    actor.sftp_password_last4 = password[-4:]
    actor.sftp_checked_at = None
    session.flush()


def clear_credentials(session: Session, actor: Actor) -> None:
    actor.sftp_username = actor.sftp_password_enc = actor.sftp_password_last4 = actor.sftp_checked_at = None
    session.flush()


def _target(cfg: dict, actor: Actor, with_password: bool) -> client.Target:
    password = crypto.decrypt(_fernet(), actor.sftp_password_enc) if with_password else ""
    return client.Target(cfg["host"], cfg["port"], cfg["host_key"], cfg["base_dir"], actor.sftp_username or "",
                         password)


def info(session: Session, actor: Actor, task: Task | None = None) -> dict:
    """What `tbr sftp info` shows – never the password. 404 `sftp_disabled`/`sftp_not_configured`."""
    cfg = config(session)
    if not cfg["enabled"] or not cfg["host"]:
        raise NotFound(_("SFTP storage is not enabled on this TaskBrigade server"), code="sftp_disabled")
    if not actor.sftp_username or not actor.sftp_password_enc:
        raise NotFound(_("No SFTP login is stored for %(name)s – an admin adds it under Admin → Users & "
                         "agents", name=actor.name), code="sftp_not_configured")
    out = {"host": cfg["host"], "port": cfg["port"], "host_key": cfg["host_key"], "base_dir": cfg["base_dir"],
           "pattern": cfg["pattern"], "username": actor.sftp_username}
    if task is not None:
        out["task_dir"] = task_dir(cfg, task)
    return out


def credentials(session: Session, actor: Actor) -> dict:
    out = info(session, actor)
    out["password"] = crypto.decrypt(_fernet(), actor.sftp_password_enc)
    return out


# --- Tests from the web UI ----------------------------------------------------------------------

def test_server(session: Session) -> dict:
    """Read the server's host key without logging in: `{fingerprint, stored, matches}`."""
    cfg = config(session)
    if not cfg["host"]:
        raise Unprocessable(_("Enter the SFTP host first"))
    try:
        seen = client.server_fingerprint(cfg["host"], cfg["port"])
    except client.SftpError as e:
        raise Unprocessable(_("SFTP check failed: %(error)s", error=e), code="sftp_check_failed") from e
    return {"fingerprint": seen, "stored": cfg["host_key"], "matches": bool(cfg["host_key"]) and seen == cfg["host_key"]}


def test_access(session: Session, actor: Actor) -> str:
    """Log in as the agent, write a test file into the base folder and delete it again."""
    if not actor.sftp_username or not actor.sftp_password_enc:
        raise NotFound(_("No SFTP login configured"))
    cfg = config(session)
    if not cfg["host"]:
        raise Unprocessable(_("Enter the SFTP host first"))
    if not cfg["host_key"]:
        raise Unprocessable(_("Confirm the SFTP host key fingerprint first (Admin → Settings → Test SFTP connection)"))
    try:
        detail = client.check(_target(cfg, actor, with_password=True), write=True)
    except client.SftpError as e:
        raise Unprocessable(_("SFTP check failed: %(error)s", error=e), code="sftp_check_failed") from e
    actor.sftp_checked_at = utcnow()
    session.flush()
    return detail


# --- Task note ----------------------------------------------------------------------------------

def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def record_upload(session: Session, actor: Actor, task: Task, files: list[dict]) -> str:
    """System comment listing the uploaded files (path, size, SHA-256), so the review sees what was stored."""
    if task.assigned_to != actor.id and actor.role != "admin":
        raise Forbidden(_("Only the assigned actor"))
    if not files:
        raise Unprocessable(_("No files"))
    base = config(session)["base_dir"]
    lines = []
    for f in files[:MAX_NOTE_FILES]:
        try:
            path = client.inside(base, f["path"])
        except client.SftpError as e:
            raise Unprocessable(str(e)) from e
        lines.append(f"- `{path}` ({_size(f['size'])}, SHA-256 `{f['sha256']}`)")
    if len(files) > MAX_NOTE_FILES:
        lines.append(_("- … and %(count)s more files", count=len(files) - MAX_NOTE_FILES))
    body = _("SFTP upload by %(name)s:", name=actor.name) + "\n\n" + "\n".join(lines)
    add_comment(session, task, actor, "system", body)
    session.flush()
    return body
