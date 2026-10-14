"""SFTP storage: `tbr sftp`, the MCP tools `sftp_put`/`sftp_ls` and the server's connection tests.

Only the server configured in TaskBrigade is used (no host/port input), its host key must match the pinned SHA-256
fingerprint before the password is sent, and every remote path must stay inside the base folder. The password
stays in this process: it is never printed, logged or written to a file. Shared by `cli/` and `app/services/sftp.py`,
so this module must not import `app`.
"""
import asyncio
import hashlib
import hmac
import logging
import posixpath
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from cli.i18n import _

DEFAULT_PORT = 22
DEFAULT_BASE_DIR = "/agent exchange"
DEFAULT_PATTERN = "{base}/{project}/{task_nr}"
PATTERN_FIELDS = ("base", "project", "task_nr")
TIMEOUT = 20
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
logging.getLogger("asyncssh").setLevel(logging.WARNING)  # Its INFO lines (per channel) would flood server and CLI logs.


class SftpError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    """Connection data from `GET /agents/me/sftp-credentials` (or the server's own settings)."""
    host: str
    port: int
    host_key: str
    base_dir: str
    username: str
    password: str = field(default="", repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "Target":
        return cls(host=data["host"], port=int(data.get("port") or DEFAULT_PORT), host_key=data.get("host_key") or "",
                   base_dir=data["base_dir"], username=data.get("username") or "", password=data.get("password") or "")


# --- Validation (also used by the server's settings) -------------------------------------------

def normalize_fingerprint(value: str | None) -> str:
    """`SHA256:<base64>` as printed by `ssh-keygen -lf` (the prefix may be left out); empty stays empty."""
    value = (value or "").strip().rstrip("=")
    if not value:
        return ""
    if not value.upper().startswith("SHA256:"):
        value = "SHA256:" + value
    value = "SHA256:" + value[7:]
    if not _FINGERPRINT.match(value):
        raise ValueError(_("expected a SHA-256 fingerprint like SHA256:AbC… (ssh-keygen -lf <key>)"))
    return value


def _has_parent_ref(path: str) -> bool:
    return any(part == ".." for part in path.replace("\\", "/").split("/"))


def normalize_base(value: str | None) -> str:
    """Absolute base folder without `..` and without a trailing slash."""
    value = (value or "").strip()
    if not value.startswith("/") or _has_parent_ref(value) or "\0" in value:
        raise ValueError(_("must be an absolute path without '..', e.g. /agent exchange"))
    return posixpath.normpath(value).replace("//", "/")


def inside(base: str, path: str | None) -> str:
    """Resolve `path` (absolute, or relative to `base`) and refuse everything outside `base`."""
    path = (path or "").strip()
    if not path:
        return base
    if _has_parent_ref(path) or "\0" in path:
        raise SftpError(_("Path %(path)s is refused: '..' is not allowed", path=path))
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(base, path)).replace("//", "/")
    if full != base and not full.startswith(base.rstrip("/") + "/"):
        raise SftpError(_("Path %(path)s is outside the SFTP base folder %(base)s", path=path, base=base))
    return full


def task_dir(pattern: str, base: str, project: str, task_nr: str) -> str:
    """Folder of a task from the storage pattern, e.g. `{base}/{project}/{task_nr}` → `/agent exchange/DEMO/DEMO-0001`."""
    try:
        raw = pattern.format(base=base, project=project, task_nr=task_nr)
    except (KeyError, IndexError, ValueError) as e:
        raise SftpError(_("Invalid storage pattern %(pattern)s", pattern=pattern)) from e
    return inside(base, raw)


def check_pattern(pattern: str | None) -> str:
    pattern = (pattern or "").strip().rstrip("/")
    if not pattern.startswith("{base}"):
        raise ValueError(_("must start with {base}, e.g. {base}/{project}/{task_nr}"))
    fields = set(re.findall(r"{([^{}]*)}", pattern))
    if fields - set(PATTERN_FIELDS):
        raise ValueError(_("allowed placeholders: %(fields)s", fields=", ".join("{%s}" % f for f in PATTERN_FIELDS)))
    try:
        task_dir(pattern, "/base", "DEMO", "DEMO-0001")
    except SftpError as e:
        raise ValueError(str(e)) from e
    return pattern


# --- Connection ---------------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine – also from code that already runs in an event loop (the MCP server)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _options() -> dict:
    """Never use the user's ~/.ssh (config, known_hosts, keys, agent): only the configured server and password."""
    return {"config": None, "client_keys": None, "agent_path": None, "connect_timeout": TIMEOUT,
            "preferred_auth": ("keyboard-interactive", "password")}


async def _fingerprint(host: str, port: int) -> str:
    import asyncssh

    key = await asyncssh.get_server_host_key(host, port, config=None)
    if key is None:
        raise SftpError(_("%(host)s:%(port)s sent no host key", host=host, port=port))
    return key.get_fingerprint("sha256")


def server_fingerprint(host: str, port: int = DEFAULT_PORT) -> str:
    """Host key fingerprint of the server (no login) – shown by "Verbindung testen" for confirmation."""
    try:
        return _run(asyncio.wait_for(_fingerprint(host, port), TIMEOUT))
    except SftpError:
        raise
    except Exception as e:  # asyncssh.Error, DNS, connection refused, timeout.
        raise SftpError(_("%(host)s:%(port)s is not reachable: %(error)s", host=host, port=port,
                          error=_describe(e))) from e


def _describe(e: BaseException) -> str:
    return str(e) or type(e).__name__


class _Session:
    """`async with _Session(target) as sftp:` – connect with a pinned host key and open the SFTP channel."""

    def __init__(self, target: Target):
        if not target.host or not target.username:
            raise SftpError(_("SFTP is not configured completely (host, user)"))
        if not target.host_key:
            raise SftpError(_("No host key fingerprint is stored for the SFTP server – an admin confirms it under "
                              "Admin → Einstellungen → Test SFTP connection"))
        self.target, self.conn, self.sftp, self.seen = target, None, None, None

    def _client_factory(self):
        import asyncssh

        session = self

        class Pinned(asyncssh.SSHClient):
            def validate_host_public_key(self, host, addr, port, key) -> bool:
                session.seen = key.get_fingerprint("sha256")
                return hmac.compare_digest(session.seen, session.target.host_key)

        return Pinned()

    async def __aenter__(self):
        import asyncssh

        t = self.target
        try:
            self.conn = await asyncssh.connect(
                t.host, t.port, username=t.username, password=t.password, known_hosts=([], [], []),
                client_factory=self._client_factory, **_options())
            self.sftp = await self.conn.start_sftp_client()
        except asyncssh.HostKeyNotVerifiable as e:
            await self._close()
            raise SftpError(_("Host key of %(host)s does not match the stored fingerprint (server: %(seen)s, "
                              "stored: %(stored)s) – connection refused", host=t.host, seen=self.seen or "?",
                              stored=t.host_key)) from e
        except asyncssh.PermissionDenied as e:
            await self._close()
            raise SftpError(_("SFTP login as %(user)s was refused", user=t.username)) from e
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as e:
            await self._close()
            raise SftpError(_("SFTP connection to %(host)s:%(port)s failed: %(error)s", host=t.host, port=t.port,
                              error=_describe(e))) from e
        return self.sftp

    async def _close(self):
        if self.sftp is not None:
            self.sftp.exit()
        if self.conn is not None:
            self.conn.close()
            await self.conn.wait_closed()

    async def __aexit__(self, *exc):
        await self._close()


def _sftp_error(e: BaseException, path: str) -> SftpError:
    return SftpError(_("%(path)s: %(error)s", path=path, error=getattr(e, "reason", None) or _describe(e)))


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_upload(files: list[Path], dest: str) -> list[tuple[Path, str]]:
    """(local file, remote path) pairs: files go into `dest`, folders keep their name and inner structure."""
    pairs = []
    for f in files:
        if f.is_dir():
            for sub in sorted(p for p in f.rglob("*") if p.is_file() and not p.is_symlink()):
                pairs.append((sub, posixpath.join(dest, f.name, sub.relative_to(f).as_posix())))
        elif f.is_file():
            pairs.append((f, posixpath.join(dest, f.name)))
        else:
            raise SftpError(_("%(path)s is not a file or folder", path=f))
    if not pairs:
        raise SftpError(_("Nothing to upload"))
    return pairs


async def _put(target: Target, pairs: list[tuple[Path, str]]) -> list[dict]:
    import asyncssh

    pairs = [(local, inside(target.base_dir, remote)) for local, remote in pairs]  # Refuse before connecting.
    done = []
    async with _Session(target) as sftp:
        for local, remote in pairs:
            try:
                await sftp.makedirs(posixpath.dirname(remote), exist_ok=True)
                await sftp.put(str(local), remote)
            except (OSError, asyncssh.SFTPError) as e:
                raise _sftp_error(e, remote) from e
            done.append({"path": remote, "size": local.stat().st_size, "sha256": file_digest(local)})
    return done


def put(target: Target, pairs: list[tuple[Path, str]]) -> list[dict]:
    """Upload; returns path, size and SHA-256 of every file (for the task note)."""
    return _run(_put(target, pairs))


async def _ls(target: Target, path: str) -> list[dict]:
    import asyncssh

    path = inside(target.base_dir, path)
    async with _Session(target) as sftp:
        try:
            if not await sftp.isdir(path):
                attrs = await sftp.stat(path)
                return [{"name": posixpath.basename(path), "path": path, "size": attrs.size or 0, "is_dir": False,
                         "mtime": attrs.mtime}]
            names = await sftp.readdir(path)
        except (OSError, asyncssh.SFTPError) as e:
            raise _sftp_error(e, path) from e
    out = []
    for n in names:
        if n.filename in (".", ".."):
            continue
        is_dir = n.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY
        out.append({"name": n.filename, "path": posixpath.join(path, n.filename), "size": n.attrs.size or 0,
                    "is_dir": is_dir, "mtime": n.attrs.mtime})
    return sorted(out, key=lambda e: (not e["is_dir"], e["name"]))


def ls(target: Target, path: str | None = None) -> list[dict]:
    return _run(_ls(target, path or target.base_dir))


async def _get(target: Target, remote: str, local: Path) -> None:
    import asyncssh

    remote = inside(target.base_dir, remote)
    async with _Session(target) as sftp:
        try:
            await sftp.get(remote, str(local), recurse=await sftp.isdir(remote))
        except (OSError, asyncssh.SFTPError) as e:
            raise _sftp_error(e, remote) from e


def get(target: Target, remote: str, local: Path) -> None:
    _run(_get(target, remote, local))


async def _check(target: Target, write: bool) -> str:
    import asyncssh

    base = target.base_dir
    async with _Session(target) as sftp:
        try:
            if not await sftp.isdir(base):
                raise SftpError(_("Base folder %(base)s does not exist on the server", base=base))
            if write:
                probe = posixpath.join(base, f".taskbrigade-check-{secrets.token_hex(4)}")
                async with sftp.open(probe, "wb") as f:
                    await f.write(b"TaskBrigade access check\n")
                await sftp.remove(probe)
        except (OSError, asyncssh.SFTPError) as e:
            raise _sftp_error(e, base) from e
    return (_("login as %(user)s, write test in %(base)s ok", user=target.username, base=base) if write
            else _("login as %(user)s ok, %(base)s readable", user=target.username, base=base))


def check(target: Target, write: bool = True) -> str:
    """Log in and check the base folder; with `write` a test file is written and deleted again."""
    return _run(_check(target, write))


# --- With the TaskBrigade API (tbr sftp, MCP tools, doctor) ---------------------------------------

TASK_NR = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def info(client, task_nr: str | None = None) -> dict:
    """`GET /agents/me/sftp` – host, port, host key, base folder, own user (never the password)."""
    return client.get("/agents/me/sftp", params={"task_nr": task_nr.upper()} if task_nr else None)


def target(client) -> Target:
    """Own login incl. password from `GET /agents/me/sftp-credentials` – kept in memory only."""
    return Target.from_api(client.get("/agents/me/sftp-credentials"))


def remote_path(client, arg: str | None) -> str:
    """`ls`/`get` argument: a task number means the task's folder, anything else a path inside the base folder."""
    if arg and TASK_NR.match(arg.strip()):
        return info(client, arg.strip())["task_dir"]
    data = info(client)
    return inside(data["base_dir"], arg)


def upload_for_task(client, task_nr: str, files: list[Path], to: str | None = None) -> list[dict]:
    """Upload into the task's folder (optionally a sub-path of it) and note the files in the task."""
    task_nr = task_nr.upper()
    folder = info(client, task_nr)["task_dir"]
    pairs = plan_upload(files, inside(folder, to) if to else folder)
    done = put(target(client), pairs)
    client.request("POST", f"/tasks/{task_nr}/sftp-uploads", json={"files": done})
    return done


def upload_lines(done: list[dict]) -> list[str]:
    return [f"{d['path']}  ({d['size']} B, sha256 {d['sha256'][:12]}…)" for d in done]


def listing_lines(entries: list[dict]) -> list[str]:
    return [f"{e['name']}/" if e["is_dir"] else f"{e['name']}  ({e['size']} B)" for e in entries]
