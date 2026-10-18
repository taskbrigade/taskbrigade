"""Native SFTP inbox – account and key management (research/konzept-datenzonen.md §4, §9).

OpenSSH `internal-sftp` with a per-user chroot serves the inbox; this module owns the part TaskBrigade knows: the
mapping system user → project (`data_sftp_accounts`) and the public keys that may log in (`data_sftp_keys`). The
system user, the chroot directory and the sshd drop-in are provisioned by `deploy/install.sh`/`deploy/update.sh`
and `tbr-admin data sftp-user` (`cli/tm_admin.py`).

A deliverer uploads into `<root>/<user>/upload/`; the import service `app/services/data_import.py` encrypts a
finished file into the data store, writes the audit entry and removes the plaintext. The user never gets a shell,
never sees another user's folder and cannot read the store or the KEK.

Everything here runs as root/`taskbrigade` on the app server (`tbr-admin`), never inside a request.

Docker (compose profile `data-sftp`, `TBR_SFTP_PROVISION=spool`): the app container has no root and no sshd, so
instead of `useradd` this module writes the desired state – accounts, fixed uids, `authorized_keys` bodies, users to
purge – into the spool file `TBR_SFTP_SPOOL`. The SFTP container (`deploy/docker/sftp/sftp_sync.py`) applies it.
"""
import json
import base64
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.config import get_settings
from app.errors import Unprocessable
from app.i18n import _
from app.models import Actor, DataSftpAccount, DataSftpKey, Project
from app.services import data_audit

# System user/group names created by the install routines (concept §9).
SFTP_GROUP = "tbr-sftp"
DATA_GROUP = "tbr-data"
DEFAULT_ROOT = "/srv/taskbrigade-sftp"
UPLOAD_DIR = "upload"
# Key types accepted in `authorized_keys`; the length limit keeps a pasted file path or a comment out.
KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
USERNAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
KEY_LINE = re.compile(r"^(?P<type>\S+)\s+(?P<blob>[A-Za-z0-9+/=]+)(?:\s+(?P<comment>.*))?$")
MAX_KEY_BYTES = 16 * 1024
NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")
# Docker spool: deliverer uids are fixed (base + account id), so a recreated SFTP container keeps file ownership.
SPOOL_FORMAT = "tbr-sftp-accounts/1"
SPOOL_UID_BASE = 20000


def root_dir() -> Path:
    return Path(get_settings().tbr_sftp_dir or DEFAULT_ROOT)


def account_dir(username: str) -> Path:
    return root_dir() / username


def upload_path(username: str) -> Path:
    return account_dir(username) / UPLOAD_DIR


# --- Public keys -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SshKey:
    key_type: str
    blob: str
    fingerprint: str
    comment: str | None


def parse_key(text: str) -> SshKey:
    """One public key from an `authorized_keys` line (options like `from=` are refused) or from a `.pub` file."""
    raw = (text or "").strip()
    if len(raw.encode()) > MAX_KEY_BYTES:
        raise Unprocessable(_("The public key is too large"), code="invalid_sftp_key")
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), "")
    if not line:
        raise Unprocessable(_("No public key found"), code="invalid_sftp_key")
    match = KEY_LINE.match(line)
    if not match or match.group("type") not in KEY_TYPES:
        raise Unprocessable(_("Not an OpenSSH public key (expected e.g. \"ssh-ed25519 AAAA… comment\")"),
                            code="invalid_sftp_key")
    blob = match.group("blob").strip()
    try:
        raw_blob = base64.b64decode(blob, validate=True)
    except (ValueError, TypeError) as e:
        raise Unprocessable(_("The public key is not valid Base64"), code="invalid_sftp_key") from e
    if not raw_blob:
        raise Unprocessable(_("The public key is empty"), code="invalid_sftp_key")
    comment = " ".join((match.group("comment") or "").split())[:200] or None
    # OpenSSH stores SHA256 of the raw key blob (without the type and comment) in `authorized_keys`.
    digest = hashlib.sha256(raw_blob).digest()
    fingerprint = f"SHA256:{base64.b64encode(digest).decode().rstrip('=')}"
    return SshKey(match.group("type"), blob, fingerprint, comment)


def read_key(value: str) -> SshKey:
    """Accept either a literal key line or the path of a `.pub` file."""
    candidate = Path(value).expanduser()
    if value and "\n" not in value and " " not in value.strip() and candidate.is_file():
        try:
            return parse_key(candidate.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            raise Unprocessable(_("%(path)s cannot be read: %(error)s", path=candidate, error=e.strerror or e),
                                code="invalid_sftp_key") from e
    return parse_key(value)


def _fingerprint_comment(key: DataSftpKey) -> str:
    return (key.fingerprint or "").strip()

def authorized_keys(account: DataSftpAccount, keys: list[DataSftpKey]) -> str:
    """The `authorized_keys` body: one key per line, the fingerprint as the comment. Restricted to SFTP and the
    upload folder so a key alone cannot open a shell (`ForceCommand` in the drop-in covers too)."""
    lines = []
    for key in keys:
        lines.append(f'restrict,command="/usr/lib/openssh/sftp-server" {key.key_type} {key.key_blob} '
                     f'{_fingerprint_comment(key)}')
    return "".join(line + "\n" for line in lines)


# --- Account queries -------------------------------------------------------------------------------------------

def find(session: Session, username: str) -> DataSftpAccount | None:
    return session.scalars(select(DataSftpAccount).where(DataSftpAccount.username == username)).first()


def get(session: Session, username: str) -> DataSftpAccount:
    account = find(session, username)
    if account is None:
        raise Unprocessable(_("No SFTP user %(user)s", user=username), code="sftp_user_unknown")
    return account


def all_accounts(session: Session) -> list[DataSftpAccount]:
    return list(session.scalars(select(DataSftpAccount).order_by(DataSftpAccount.username)))


def keys_of(session: Session, account: DataSftpAccount) -> list[DataSftpKey]:
    return list(session.scalars(select(DataSftpKey).where(DataSftpKey.account_id == account.id)
                                .order_by(DataSftpKey.id)))


def _project(session: Session, key: str) -> Project:
    project = session.scalars(select(Project).where(Project.project_key == (key or "").strip().upper())).first()
    if project is None:
        raise Unprocessable(_("Project %(key)s not found", key=key or "–"), code="sftp_project_unknown")
    return project


# --- Provisioning (root) --------------------------------------------------------------------------------------

def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def spool_mode() -> bool:
    return get_settings().tbr_sftp_provision == "spool"


def spool_path() -> Path:
    return Path(get_settings().tbr_sftp_spool)


def spool_state(session: Session, purge: set[str] = frozenset()) -> dict:
    """Desired state for the SFTP container: every account with its uid, enabled flag and `authorized_keys`."""
    accounts = []
    for account in all_accounts(session):
        keys = keys_of(session, account) if account.enabled else []
        accounts.append({"username": account.username, "uid": SPOOL_UID_BASE + int(account.id),
                         "enabled": bool(account.enabled), "authorized_keys": authorized_keys(account, keys)})
    names = {a["username"] for a in accounts}
    return {"format": SPOOL_FORMAT, "accounts": accounts, "purge": sorted(set(purge) - names)}


def write_spool(session: Session, purge: set[str] = frozenset()) -> Path:
    """Write the spool atomically (0644: user names and public keys only, read by root in the SFTP container
    without CAP_DAC_OVERRIDE). Keeps earlier purge requests the SFTP container may not have applied yet."""
    path = spool_path()
    earlier: set[str] = set()
    try:
        earlier = set(json.loads(path.read_text()).get("purge") or [])
    except (OSError, ValueError, AttributeError):
        pass
    state = spool_state(session, earlier | set(purge))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
        os.fchmod(f.fileno(), 0o644)
    os.replace(tmp, path)
    return path


def system_available() -> str | None:
    """Why the system side cannot be provisioned right now, else None (used by the CLI for a clear message)."""
    if spool_mode():
        folder = spool_path().parent
        if not os.access(folder, os.W_OK | os.X_OK):
            return _("%(path)s is not writable – start the stack with the compose profile data-sftp",
                     path=folder)
        return None
    if shutil.which("useradd") is None or shutil.which("groupadd") is None:
        return _("useradd/groupadd not found – the SFTP inbox needs a Linux host with shadow-utils")
    if os.geteuid() != 0:
        return _("Root rights are required to create the system user and the chroot directory")
    return None


def _group_exists(name: str) -> bool:
    return _run(["getent", "group", name]).returncode == 0


def _user_info(name: str) -> str | None:
    result = _run(["getent", "passwd", name])
    return result.stdout.strip() if result.returncode == 0 else None


def _user_shell(name: str) -> str | None:
    info = _user_info(name)
    return info.rsplit(":", 1)[-1] if info else None


def ensure_provisioned(session: Session, account: DataSftpAccount, keys: list[DataSftpKey]) -> None:
    """Create the group, the system user (no shell, member of `tbr-sftp`) and the chroot layout. Idempotent; the
    caller must have checked `system_available()`/root. Runs `useradd`, so it never happens in a request."""
    problem = system_available()
    if problem:
        raise Unprocessable(problem, code="sftp_provisioning_unavailable")
    if spool_mode():
        write_spool(session)
        return
    if not _group_exists(SFTP_GROUP):
        result = _run(["groupadd", "--system", SFTP_GROUP])
        if result.returncode != 0:
            raise Unprocessable(_("groupadd %(group)s failed: %(error)s", group=SFTP_GROUP,
                                  error=result.stderr.strip()), code="sftp_provisioning_failed")
    if _user_info(account.username) is None:
        result = _run(["useradd", "--system", "--home", str(account_dir(account.username)),
                       "--shell", "/usr/sbin/nologin", "--gid", SFTP_GROUP,
                       "--groups", SFTP_GROUP, account.username])
        if result.returncode != 0:
            raise Unprocessable(_("useradd %(user)s failed: %(error)s", user=account.username,
                                  error=result.stderr.strip()), code="sftp_provisioning_failed")
    shell = _user_shell(account.username)
    if shell not in NOLOGIN_SHELLS:
        raise Unprocessable(_("User %(user)s already exists with shell %(shell)s – use a system account without a "
                              "shell", user=account.username, shell=shell or "?"), code="sftp_shell_forbidden")
    if not _group_exists(DATA_GROUP):
        raise Unprocessable(_("Group %(group)s is missing – run install.sh/update.sh with TBR_DATA_ZONES=1 first",
                              group=DATA_GROUP), code="sftp_provisioning_failed")
    # Chroot root must stay root:root and not group-writable, otherwise sshd refuses the chroot (§4).
    _ensure_dir(root_dir(), "root:root", 0o755)
    _ensure_dir(account_dir(account.username), "root:root", 0o755)
    # The upload folder is owned by the deliverer; the app imports through the data group.
    _ensure_dir(upload_path(account.username), f"{account.username}:{DATA_GROUP}", 0o730)
    write_authorized_keys(account, keys)


def _ensure_dir(path: Path, owner: str, mode: int) -> None:
    user, _, group = owner.partition(":")
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:  # Changing owner needs root; on the app server (root) it keeps the layout exact.
        os.chown(path, _uid(user), _gid(group or user))
    os.chmod(path, mode)


def _uid(name: str) -> int:
    import pwd
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return 0


def _gid(name: str) -> int:
    import grp
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return 0


def _can_write(path: Path) -> bool:
    """True when we may create/replace files in this existing directory (root may always)."""
    if os.geteuid() == 0:
        return True
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def write_authorized_keys(account: DataSftpAccount, keys: list[DataSftpKey]) -> bool:
    """Write `authorized_keys` atomically (root, mode 0644) below the account's `.ssh`. Returns False when this
    needs root and the chroot does not exist/is not ours (the database change still applies; `sftp-user provision`
    writes the file on the host)."""
    if spool_mode():
        session = object_session(account)
        if session is None or system_available():
            return False
        write_spool(session)
        return True
    directory = account_dir(account.username) / ".ssh"
    if os.geteuid() != 0:
        parent = account_dir(account.username)
        if not parent.is_dir() or not _can_write(parent):
            return False
    _ensure_dir(directory, "root:root", 0o755)
    path = directory / "authorized_keys"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(authorized_keys(account, keys), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def provisioned(account: DataSftpAccount) -> bool:
    """Best-effort check used by `sftp-user list`: the system user and the chroot exist."""
    if spool_mode():  # The users live in the SFTP container; its chroot folders are visible here.
        return upload_path(account.username).is_dir()
    return _user_info(account.username) is not None and account_dir(account.username).is_dir()


# --- Mutations (called by `tbr-admin`) ------------------------------------------------------------------------

def create(session: Session, username: str, project_key: str, *, name: str | None = None,
           key: str | None = None) -> DataSftpAccount:
    username = (username or "").strip().lower()
    if not USERNAME.match(username):
        raise Unprocessable(_("Invalid user name %(user)s (lower-case letters, digits, \"-\" and \"_\")",
                              user=username or "–"), code="invalid_sftp_user")
    if find(session, username) is not None:
        raise Unprocessable(_("SFTP user %(user)s already exists", user=username), code="sftp_user_exists")
    project = _project(session, project_key)
    if project.status == "archived":
        raise Unprocessable(_("Project %(project)s is archived", project=project.project_key))
    owner = session.get(Actor, project.owner_id)
    if owner is None:
        raise Unprocessable(_("Project %(project)s has no owner", project=project.project_key))
    account = DataSftpAccount(username=username, project_id=project.id, owner_id=owner.id,
                              name=(name or "").strip()[:200] or None)
    session.add(account)
    session.flush()
    data_audit.append(session, "sftp_account_created", "project", project.id, actor=owner, via="sftp",
                      detail={"user": username, "project": project.project_key})
    if key:
        add_key(session, account, key)
    return account


def add_key(session: Session, account: DataSftpAccount, value: str) -> DataSftpKey:
    parsed = read_key(value)
    if session.scalars(select(DataSftpKey).where(DataSftpKey.account_id == account.id,
                                                 DataSftpKey.fingerprint == parsed.fingerprint)).first() is not None:
        raise Unprocessable(_("This key is already registered for %(user)s", user=account.username),
                            code="sftp_key_exists")
    key = DataSftpKey(account_id=account.id, key_type=parsed.key_type, key_blob=parsed.blob,
                      fingerprint=parsed.fingerprint, comment=parsed.comment)
    session.add(key)
    session.flush()
    data_audit.append(session, "sftp_key_added", "project", account.project_id, actor=None, via="sftp",
                      detail={"user": account.username, "fingerprint": parsed.fingerprint})
    return key


def remove_key(session: Session, account: DataSftpAccount, fingerprint: str) -> None:
    wanted = (fingerprint or "").strip()
    keys = keys_of(session, account)
    hit = next((k for k in keys if k.fingerprint == wanted or k.fingerprint.endswith(wanted)
                or wanted in (k.comment or "")), None)
    if hit is None:
        raise Unprocessable(_("No key %(fingerprint)s for %(user)s", fingerprint=fingerprint or "–",
                              user=account.username), code="sftp_key_unknown")
    if len(keys) <= 1:
        raise Unprocessable(_("%(user)s would be left without a key – remove the user instead",
                              user=account.username), code="sftp_last_key")
    session.delete(hit)
    session.flush()
    data_audit.append(session, "sftp_key_removed", "project", account.project_id, actor=None, via="sftp",
                      detail={"user": account.username, "fingerprint": hit.fingerprint})


def set_enabled(session: Session, account: DataSftpAccount, enabled: bool) -> None:
    account.enabled = bool(enabled)
    session.flush()
    if spool_mode() and not system_available():
        write_spool(session)
    data_audit.append(session, "sftp_account_enabled" if enabled else "sftp_account_disabled", "project",
                      account.project_id, actor=None, via="sftp", detail={"user": account.username})


def remove(session: Session, username: str, *, purge: bool = False) -> DataSftpAccount:
    """Remove an account. Without root this only removes the database row (the caller decides about the system
    user and the chroot – `cli.tm_admin` does that with `purge_system`)."""
    account = get(session, username)
    data_audit.append(session, "sftp_account_removed", "project", account.project_id, actor=None, via="sftp",
                      detail={"user": account.username, "purge": bool(purge)})
    session.delete(account)
    session.flush()
    if spool_mode() and not system_available():
        write_spool(session, {account.username} if purge else set())
    return account


def purge_system(username: str) -> list[str]:
    """Remove the system user and the chroot directory (root only). Returns the steps performed; missing pieces
    are skipped, so the call is idempotent and safe after a partially removed account."""
    problem = system_available()
    if problem:
        raise Unprocessable(problem, code="sftp_provisioning_unavailable")
    if spool_mode():  # `remove(..., purge=True)` queued it; the SFTP container deletes user and folder.
        return ["queued for the SFTP container"]
    steps: list[str] = []
    directory = account_dir(username)
    if _user_info(username) is not None:
        result = _run(["userdel", username])
        if result.returncode != 0:
            raise Unprocessable(_("userdel %(user)s failed: %(error)s", user=username,
                                  error=result.stderr.strip()), code="sftp_provisioning_failed")
        steps.append("userdel")
    if directory.exists():
        shutil.rmtree(directory)
        steps.append("rmdir")
    return steps
