"""Offline installation of per-repository Gitea data guards; never calls Gitea."""
import os
import re
import tempfile
from importlib.resources import files
from pathlib import Path

from sqlalchemy import exists, or_, select

from app.models import Dataset, Project, Task
from app.services.attachments import BLOCKED_EXTENSIONS

HOOK_NAME = "taskbrigade-data-guard"
MARKER = "# TaskBrigade managed data-zone pre-receive hook v1\n"


def payload():
    source = files("app.services.hooks").joinpath("pre_receive.py").read_text(encoding="utf-8")
    source = source.replace("BLOCKED_EXTENSIONS = ()", "BLOCKED_EXTENSIONS = "
                            + repr(tuple(ext.encode("ascii") for ext in sorted(BLOCKED_EXTENSIONS))), 1)
    return source.replace("#!/usr/bin/env python3\n", "#!/usr/bin/env python3\n" + MARKER, 1)


def repositories(session, *, protected_only=True):
    """Protect L-default projects, mixed projects with L/R tasks, and dataset projects.

    The installation is opt-in, independent of data_enabled: switching off a UI menu
    must not remove a repository's protection. Include archived tasks/datasets.
    """
    query = select(Project).where(Project.gitea_owner.is_not(None), Project.gitea_repo.is_not(None))
    if protected_only:
        query = query.where(or_(
            Project.privacy == "local_only",
            exists().where(Task.project_id == Project.id, Task.data_class.in_(("local_only", "sealed"))),
            exists().where(Dataset.project_id == Project.id),
        ))
    return list(session.scalars(query.order_by(Project.id)))


def hook_path(repo_root, owner, repo):
    # Gitea stores owner/repository paths lowercase. Refuse traversal and symlinks
    # outside the repository root, including a symlink at the final hook path.
    if not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part or "") for part in (owner, repo)):
        raise ValueError("Invalid Gitea owner/repository name")
    root = Path(repo_root).resolve(strict=True)
    repository = root / owner.lower() / (repo.lower() + ".git")
    path = repository / "hooks" / "pre-receive.d" / HOOK_NAME
    if (not path.resolve().is_relative_to(root)
            or any(component.is_symlink() for component in (path, *path.parents) if component != root)):
        raise ValueError("Hook path escapes the repository root or is a symlink")
    if not (repository / "HEAD").is_file() or not (repository / "objects").is_dir():
        raise ValueError("Gitea repository is missing; refusing to create it")
    dispatcher = repository / "hooks" / "pre-receive"
    if not dispatcher.is_file() or not dispatcher.stat().st_mode & 0o111:
        raise ValueError("Gitea pre-receive dispatcher is missing or not executable; resynchronize Gitea hooks first")
    return path


def status(path):
    if not path.exists():
        return "missing"
    if MARKER not in path.read_text(encoding="utf-8"):
        return "foreign"
    if path.read_text(encoding="utf-8") != payload() or not path.stat().st_mode & 0o111:
        return "outdated"
    return "installed"


def install(path, *, dry_run=False):
    state = status(path)
    if state == "foreign":
        raise ValueError("Refusing to overwrite an unmanaged hook")
    if dry_run or state == "installed":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".taskbrigade-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload())
            stream.flush()
            os.fchmod(stream.fileno(), 0o755)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def uninstall(path, *, dry_run=False):
    if status(path) == "foreign":
        raise ValueError("Refusing to delete an unmanaged hook")
    if not dry_run:
        path.unlink(missing_ok=True)
