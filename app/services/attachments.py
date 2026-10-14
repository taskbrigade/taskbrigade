"""Task attachments are committed by the service user under tasks/<task_nr>/<filename> on the
project repo's default branch; only metadata is stored here."""
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import getenv
from app.i18n import _
from app.errors import Forbidden, Unprocessable
from app.models import Actor, Project, Task, TaskAttachment
from app.services import settings_store
from app.services.gitea import GiteaError
from app.services.state_machine import add_event

MAX_BYTES = int(getenv("MAX_ATTACHMENT_MB", "10")) * 1024 * 1024
MAX_FILES = 20


@dataclass
class Upload:
    filename: str
    content: bytes


def safe_filename(name: str) -> str:
    """Keep only the basename (no path or ..); transliterate umlauts and allow [A-Za-z0-9._-]."""
    base = PurePosixPath((name or "").replace("\\", "/")).name
    base = unicodedata.normalize("NFKD", base.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae",
                                                                       "Ö": "Oe", "Ü": "Ue", "ß": "ss"})))
    base = base.encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
    if not base:
        base = "datei"
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem, ext = stem.strip("-.")[:120] or "datei", ext.strip("-")[:20]
    return f"{stem}.{ext}" if ext else stem


def _unique(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    i = 2
    while True:
        candidate = f"{stem}-{i}.{ext}" if ext else f"{stem}-{i}"
        if candidate not in taken:
            return candidate
        i += 1


def can_attach(actor: Actor, task: Task, owner_id: int | None = None) -> bool:
    return (actor.role in ("admin", "orchestrator") or actor.id in (task.created_by, task.assigned_to)
            or (actor.actor_type == "human" and actor.id == owner_id))


def listing(session: Session, task: Task, project: Project) -> list[dict]:
    rows = session.scalars(select(TaskAttachment).where(TaskAttachment.task_id == task.id)
                           .order_by(TaskAttachment.created_at, TaskAttachment.id))
    names = dict(session.execute(select(Actor.id, Actor.name)).all())
    base = (project.gitea_url or "").rstrip("/")
    branch = quote(project.default_branch or "main")
    return [{"filename": a.filename, "path": a.repo_path, "size_bytes": a.size_bytes, "commit_sha": a.commit_sha,
             "uploaded_by": names.get(a.uploaded_by), "created_at": a.created_at,
             "url": f"{base}/src/branch/{branch}/{quote(a.repo_path)}" if base else None,
             "raw_url": f"{base}/raw/branch/{branch}/{quote(a.repo_path)}" if base else None} for a in rows]


def add(session: Session, actor: Actor, task: Task, uploads: list[Upload]) -> list[TaskAttachment]:
    uploads = [u for u in uploads if u.filename or u.content]
    if not uploads:
        return []
    if not can_attach(actor, task, session.get(Project, task.project_id).owner_id):
        raise Forbidden(_( 'Attachments: only admins, orchestrators, owners, creators, or assignees' ))
    if len(uploads) > MAX_FILES:
        raise Unprocessable(_("At most %(count)d files at once", count=MAX_FILES))
    for u in uploads:
        if len(u.content) > MAX_BYTES:
            raise Unprocessable(_("%(filename)s: larger than %(size)d MB", filename=u.filename, size=MAX_BYTES // (1024 * 1024)), code="attachment_too_large")
        if not u.content:
            raise Unprocessable(_("%(filename)s: file is empty", filename=u.filename))
    project = session.get(Project, task.project_id)
    if not project.gitea_repo:
        raise Unprocessable(_( 'Attachments require a Gitea repository; this project does not have one yet' ), code="no_repo")

    client = settings_store.gitea_client(session)
    taken = set(session.scalars(select(TaskAttachment.filename).where(TaskAttachment.task_id == task.id)))
    files: dict[str, bytes] = {}
    planned: list[tuple[str, str, int]] = []
    for u in uploads:
        name = _unique(safe_filename(u.filename), taken)
        try:  # An existing repo path (e.g. committed by an agent) would make Gitea reject the batch.
            while client.file_exists(project.gitea_owner, project.gitea_repo, f"tasks/{task.task_nr}/{name}",
                                     project.default_branch):
                taken.add(name)
                name = _unique(name, taken)
        except GiteaError as e:
            raise Unprocessable(_("Gitea unreachable: %(error)s", error=e), code="gitea_error") from e
        taken.add(name)
        path = f"tasks/{task.task_nr}/{name}"
        files[path] = u.content
        planned.append((name, path, len(u.content)))
    try:
        sha = client.create_files(
            project.gitea_owner, project.gitea_repo, files,
            _("[%(task_nr)s] Attachment: %(files)s", task_nr=task.task_nr, files=", ".join(n for n, _, _ in planned)), project.default_branch)
    except GiteaError as e:
        raise Unprocessable(_("Could not write attachments to the repository: %(error)s", error=e), code="gitea_error") from e

    rows = []
    for name, path, size in planned:
        row = TaskAttachment(task_id=task.id, filename=name, repo_path=path, size_bytes=size,
                             commit_sha=sha, uploaded_by=actor.id)
        session.add(row)
        rows.append(row)
    add_event(session, task, actor, "attachment_added", None, ", ".join(n for n, _, _ in planned)[:100])
    session.flush()
    return rows
