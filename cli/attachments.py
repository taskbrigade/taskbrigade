"""Task attachments from the agent machine: upload via `POST /tasks/{nr}/attachments` (`tbr attach`, MCP) and the
path rules of the MCP tools `attach_files`/`create_task(attachments=…)`.

The MCP server reads files on the host on behalf of a chat model, so a prompt injection could ask it to attach
`~/.ssh/id_ed25519`. Files are therefore read only below allowed roots (default: the agent's work directory,
~/Downloads, ~/Documents, ~/Desktop; `[mcp] attach_roots` in `.taskbrigade.toml` replaces the list), after resolving
symlinks, and never from hidden paths or files that look like keys or secrets.
"""
import fnmatch
import os
import stat
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from cli.i18n import _

MAX_FILES = 20  # Same limit as the server (app/services/attachments.py).
DEFAULT_MAX_MB = 10  # Server default `MAX_ATTACHMENT_MB`; `[mcp] max_attachment_mb` follows a raised server limit.
MAX_TEXT_BYTES = 1024 * 1024  # attach_text: small text attachments only.
DEFAULT_HOME_ROOTS = ("Downloads", "Documents", "Desktop")
HIDDEN_ALLOWED = (".worktrees",)  # Task working folders in the work directory (<KEY>/.worktrees/<NR>).
SECRET_PATTERNS = ("id_*", "*.pem", "*.key", "*.p12", "*.pfx", "*.ppk", "*.kdbx", "*.keychain", "*.keychain-db",
                   "credentials*", "*.netrc", "netrc", "*.gpg")


class AttachError(Exception):
    """A file was refused before uploading; the message names the reason."""


@dataclass
class AttachPolicy:
    roots: list[Path]
    max_bytes: int = DEFAULT_MAX_MB * 1024 * 1024
    base: Path | None = None  # Relative paths are resolved against it (the work directory).


@dataclass
class Checked:
    files: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # "<given path>: <reason>"


def mcp_settings(work_root: Path | None) -> dict:
    """The `[mcp]` section of the agent's configuration ({} without file or section)."""
    from cli.client import agent_config

    if work_root is None:
        return {}
    path = agent_config(work_root)
    if not path.exists():
        return {}
    try:
        section = tomllib.loads(path.read_text()).get("mcp")
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return section if isinstance(section, dict) else {}


def policy(work_root: Path | None, settings: dict | None = None, home: Path | None = None) -> AttachPolicy:
    """Allowed roots and size limit: `[mcp] attach_roots` (`~` expanded, relative to the work directory) or the
    defaults; roots that do not exist are left out."""
    settings = mcp_settings(work_root) if settings is None else settings
    home = home or Path.home()
    configured = settings.get("attach_roots")
    if isinstance(configured, list):
        raw = [Path(str(r)).expanduser() for r in configured if str(r).strip()]
        raw = [r if r.is_absolute() or work_root is None else work_root / r for r in raw]
    else:
        raw = ([work_root] if work_root else []) + [home / name for name in DEFAULT_HOME_ROOTS]
    roots = []
    for r in raw:
        try:
            resolved = r.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    try:
        max_mb = float(settings.get("max_attachment_mb") or DEFAULT_MAX_MB)
    except (TypeError, ValueError):
        max_mb = DEFAULT_MAX_MB
    return AttachPolicy(roots, int(max_mb * 1024 * 1024), work_root)


def _hidden_or_secret(parts: Iterable[str]) -> str | None:
    parts = list(parts)
    for part in parts:
        if part.startswith(".") and part not in HIDDEN_ALLOWED:
            return _("hidden files and folders (%(name)s) are never attached", name=part)
    for part in parts:
        if any(fnmatch.fnmatch(part.lower(), p) for p in SECRET_PATTERNS):
            return _("%(name)s looks like a key or secret and is never attached", name=part)
    return None


def check_path(raw: str, pol: AttachPolicy) -> Path:
    """The resolved file for `raw`, or AttachError with the reason it is refused."""
    given = Path(str(raw).strip()).expanduser()
    if not str(raw).strip():
        raise AttachError(_("empty path"))
    if not given.is_absolute():
        if pol.base is None:
            raise AttachError(_("give an absolute path"))
        given = pol.base / given
    given = Path(os.path.abspath(given))  # Normalises `..` without following symlinks.
    try:
        real = given.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AttachError(_("file not found")) from None
    root = next((r for r in pol.roots if real.is_relative_to(r)), None)
    if root is None:
        raise AttachError(_("outside the allowed folders (%(roots)s)",
                            roots=", ".join(str(r) for r in pol.roots) or _("none configured")))
    # The resolved path below its root and the name as given (a link called `.env` or `id_rsa` is refused too).
    given_parts = next((given.relative_to(r).parts for r in pol.roots if given.is_relative_to(r)), (given.name,))
    reason = _hidden_or_secret(real.relative_to(root).parts) or _hidden_or_secret(given_parts)
    if reason:
        raise AttachError(reason)
    st = real.stat()
    if stat.S_ISDIR(st.st_mode):
        raise AttachError(_("is a folder – attach the files in it"))
    if not stat.S_ISREG(st.st_mode):
        raise AttachError(_("not a regular file (device, pipe or socket)"))
    if st.st_size > pol.max_bytes:
        raise AttachError(_("larger than %(size)s MB", size=f"{pol.max_bytes / 1024 / 1024:.3g}"))
    if not st.st_size:
        raise AttachError(_("file is empty"))
    if not os.access(real, os.R_OK):
        raise AttachError(_("not readable"))
    return real


def check_paths(paths: list[str], pol: AttachPolicy) -> Checked:
    result = Checked()
    if len(paths) > MAX_FILES:
        result.errors.append(_("%(count)s files given – at most %(max)s at once", count=len(paths), max=MAX_FILES))
        return result
    for raw in paths:
        try:
            path = check_path(raw, pol)
        except AttachError as e:
            result.errors.append(f"{raw}: {e}")
            continue
        if path not in result.files:
            result.files.append(path)
    return result


def text_filename(name: str) -> str:
    """Basename without path, `..` or leading dots for attach_text; AttachError if nothing is left."""
    base = PurePosixPath((name or "").replace("\\", "/")).name.strip().lstrip(".").strip()
    if not base:
        raise AttachError(_("filename is empty after removing path parts and leading dots"))
    return base


def upload(client, task_nr: str, files: list[tuple[str, object]]) -> list[dict]:
    """POST the (name, bytes or binary file) pairs to the task; returns the server's attachment entries."""
    return client.request("POST", f"/tasks/{task_nr.upper()}/attachments",
                          files=[("files", (name, content)) for name, content in files]).json()


def upload_paths(client, task_nr: str, paths: list[Path]) -> list[dict]:
    handles = [(p.name, p.open("rb")) for p in paths]
    try:
        return upload(client, task_nr, handles)
    finally:
        for _name, fh in handles:
            fh.close()


def result_lines(uploaded: list[dict]) -> list[str]:
    return [_("%(path)s  (%(size)s bytes)", path=a["path"], size=a["size_bytes"]) for a in uploaded]
