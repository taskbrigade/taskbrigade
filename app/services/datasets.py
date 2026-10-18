"""Data store outside Gitea (research/konzept-datenzonen.md §3/§4, guide docs/data-store.md).

Datasets are files encrypted on arrival (`app/data_crypto.py`): one DEK per dataset, wrapped with the KEK from
`TBR_DATA_KEY_FILE`; the ciphertext lives in `TBR_DATA_DIR/store`. Tasks and texts name a dataset only by its key
(`DS-0003`); its name, file name and contents never reach an agent. Every action is written to `data_audit`.

Rights (§3): project members see the list and metadata; the project owner, admins and members with the data right
(`actors.data_access`) upload; owner and admins delete; only admins with the data right may download the raw
file (emergency, audited). The web UI is for humans only.
"""
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app import data_crypto
from app.config import get_settings
from app.errors import Forbidden, NotFound, Unprocessable
from app.i18n import N_, _
from app.models import DATASET_CLASSES, Actor, Dataset, Project, ProjectMember
from app.services import data_audit, settings_store, zones

log = logging.getLogger(__name__)

STATUS_LABELS = {"receiving": N_("Receiving"), "quarantine": N_("Stored – awaits profile"),
                 "profiling": N_("Profiling"), "ready": N_("Ready"), "archived": N_("Archived (replaced)"),
                 "deleted": N_("Deleted")}
CLASS_LABELS = {"sealed": N_("Sealed – runner only"), "local_only": N_("Local only – local models may read")}
LIVE_STATUSES = ("receiving", "quarantine", "profiling", "ready", "archived")
UPLOAD_PROJECT_STATUSES = ("idea", "active", "paused")
WARN_DAYS = 7  # Reminder before the retention date (§3)
MIN_GROUP_MAX = 10000
RETENTION_LOCK = "tbr_data_retention"
STORE, INCOMING = "store", "incoming"


# --- Configuration -------------------------------------------------------------------------------------------

def enabled(session: Session) -> bool:
    return settings_store.get_bool(session, "data_enabled")


def data_dir() -> Path:
    return Path(get_settings().tbr_data_dir)


def kek() -> bytes:
    return data_crypto.load_kek(Path(get_settings().tbr_data_key_file))


def max_upload_bytes(session: Session) -> int:
    return int(settings_store.get(session, "data_max_upload_mb") or 200) * 1024 * 1024


def retention_days(session: Session) -> int:
    return int(settings_store.get(session, "data_retention_days") or 90)


def _subdir(name: str) -> Path:
    path = data_dir() / name
    path.mkdir(mode=0o750, parents=True, exist_ok=True)
    return path


def problem(session: Session) -> str | None:
    """Why uploads cannot work right now (setting off, key or directory missing), else None."""
    if not enabled(session):
        return _("The data store is switched off (Admin → Settings → Data store enabled).")
    try:
        kek()
    except data_crypto.DataKeyError as e:
        return str(e)
    try:
        for name in (STORE, INCOMING):
            path = _subdir(name)
            if not os.access(path, os.W_OK | os.X_OK):
                return _("%(path)s is not writable for the TaskBrigade service", path=path)
    except OSError as e:
        return _("%(path)s cannot be created: %(error)s", path=data_dir(), error=e.strerror or e)
    return None


def resolve(storage_path: str) -> Path:
    """Absolute path of a stored file; refuses anything outside TBR_DATA_DIR."""
    root = data_dir().resolve()
    path = (root / storage_path).resolve()
    if root not in path.parents:
        raise Unprocessable(_("Invalid storage path"), code="invalid_storage_path")
    return path


# --- Rights ----------------------------------------------------------------------------------------------------

def _human(actor: Actor | None) -> bool:
    return actor is not None and actor.actor_type == "human" and actor.is_active


def is_member(session: Session, project: Project, actor: Actor | None) -> bool:
    if not _human(actor):
        return False
    if actor.role == "admin" or project.owner_id == actor.id:
        return True
    return session.get(ProjectMember, (project.id, actor.id)) is not None


def can_view(session: Session, project: Project, actor: Actor | None) -> bool:
    return is_member(session, project, actor)


def can_upload(session: Session, project: Project, actor: Actor | None) -> bool:
    if not is_member(session, project, actor):
        return False
    return actor.role == "admin" or project.owner_id == actor.id or bool(actor.data_access)


def can_delete(project: Project, actor: Actor | None) -> bool:
    return _human(actor) and (actor.role == "admin" or project.owner_id == actor.id)


def can_download(actor: Actor | None) -> bool:
    return _human(actor) and actor.role == "admin" and bool(actor.data_access)


def menu_visible(session: Session, actor: Actor | None) -> bool:
    """Menu item "Data" (§8): data store on and the human has the data right, owns a project or is an admin."""
    if not _human(actor) or not enabled(session):
        return False
    if actor.role == "admin" or actor.data_access:
        return True
    return session.scalar(select(Project.id).where(Project.owner_id == actor.id).limit(1)) is not None


def visible_projects(session: Session, actor: Actor) -> list[Project]:
    if not _human(actor):
        return []
    stmt = select(Project).order_by(Project.project_key)
    if actor.role != "admin":
        member = select(ProjectMember.project_id).where(ProjectMember.actor_id == actor.id)
        stmt = stmt.where(or_(Project.owner_id == actor.id, Project.id.in_(member)))
    return list(session.scalars(stmt))


def upload_projects(session: Session, actor: Actor) -> list[Project]:
    return [p for p in visible_projects(session, actor)
            if p.status in UPLOAD_PROJECT_STATUSES and can_upload(session, p, actor)]


def get(session: Session, key: str) -> Dataset:
    ds = session.scalars(select(Dataset).where(Dataset.dataset_key == (key or "").strip().upper())).first()
    if ds is None:
        raise NotFound(_("Dataset %(key)s not found", key=key))
    return ds


def get_visible(session: Session, key: str, actor: Actor) -> tuple[Dataset, Project]:
    """The dataset if the actor may see it; otherwise "not found" (do not reveal that it exists)."""
    ds = get(session, key)
    project = session.get(Project, ds.project_id)
    if not can_view(session, project, actor):
        raise NotFound(_("Dataset %(key)s not found", key=key))
    return ds, project


def search(session: Session, actor: Actor, project_key: str | None = None, status: str | None = None,
           data_class: str | None = None, q: str | None = None) -> list[Dataset]:
    projects = {p.id: p for p in visible_projects(session, actor)}
    if project_key:
        projects = {i: p for i, p in projects.items() if p.project_key == project_key}
    if not projects:
        return []
    stmt = select(Dataset).where(Dataset.project_id.in_(projects)).order_by(Dataset.id.desc())
    if status == "live":
        stmt = stmt.where(Dataset.status != "deleted")
    elif status in STATUS_LABELS:
        stmt = stmt.where(Dataset.status == status)
    if data_class in DATASET_CLASSES:
        stmt = stmt.where(Dataset.data_class == data_class)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Dataset.name.like(like), Dataset.dataset_key.like(like)))
    return list(session.scalars(stmt))


# --- Upload ----------------------------------------------------------------------------------------------------

class PendingUpload:
    """Ciphertext being written to `TBR_DATA_DIR/incoming` while the request streams in. The plaintext exists only
    in memory, one chunk at a time; `abort` removes the partial file."""

    def __init__(self, filename: str | None, media_type: str | None, limit: int):
        self.filename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1][:255] or None
        self.media_type = (media_type or "")[:100] or None
        self.limit = limit
        self.dek = data_crypto.new_dek()
        self.path = _subdir(INCOMING) / f"{secrets.token_hex(12)}.part"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        self._file = os.fdopen(fd, "wb")
        self._writer = data_crypto.EncryptingWriter(self._file, self.dek)
        self.finished = False
        self.stored_path: str | None = None  # Set once the ciphertext was moved into the store

    @property
    def size(self) -> int:
        return self._writer.size

    @property
    def sha256(self) -> str:
        return self._writer.sha256

    def write(self, data: bytes) -> None:
        if self._writer.size + len(data) > self.limit:
            raise Unprocessable(_("The file is larger than %(size)d MB", size=self.limit // (1024 * 1024)),
                                code="dataset_too_large")
        self._writer.write(data)

    def finish(self) -> None:
        self._writer.close()
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self.finished = True

    def abort(self) -> None:
        try:
            if not self._file.closed:
                self._file.close()
        finally:
            self.path.unlink(missing_ok=True)


def begin_upload(session: Session, filename: str | None, media_type: str | None) -> PendingUpload:
    msg = problem(session)
    if msg:
        raise Unprocessable(msg, code="data_store_unavailable")
    return PendingUpload(filename, media_type, max_upload_bytes(session))


@dataclass
class UploadForm:
    name: str
    data_class: str = "sealed"
    retention_days: int | None = None
    min_group_size: int | None = None
    supersedes: str | None = None
    errors: list[str] = field(default_factory=list)


def _int_field(value: str | None, low: int, high: int, label: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit() or not low <= int(value) <= high:
        raise Unprocessable(_("%(label)s: expected an integer from %(low)d to %(high)d", label=label, low=low,
                              high=high), code="invalid_dataset_field")
    return int(value)


def parse_form(fields: dict[str, str]) -> UploadForm:
    name = " ".join((fields.get("name") or "").split())
    if not name:
        raise Unprocessable(_("Name is required"), code="invalid_dataset_field")
    if len(name) > 200:
        raise Unprocessable(_("Name: at most 200 characters"), code="invalid_dataset_field")
    cls = (fields.get("data_class") or "sealed").strip()
    if cls not in DATASET_CLASSES:
        raise Unprocessable(_("Class must be one of %(values)s", values=", ".join(DATASET_CLASSES)),
                            code="invalid_dataset_field")
    return UploadForm(name=name, data_class=cls,
                      retention_days=_int_field(fields.get("retention_days"), 1, 3650, _("Retention")),
                      min_group_size=_int_field(fields.get("min_group_size"), 2, MIN_GROUP_MAX,
                                                _("Min. group size")),
                      supersedes=(fields.get("supersedes") or "").strip() or None)


def project_for_upload(session: Session, key: str | None, actor: Actor) -> Project:
    project = session.scalars(select(Project).where(Project.project_key == (key or "").strip().upper())).first()
    if project is None or not can_view(session, project, actor):
        raise NotFound(_("Project %(key)s not found", key=key or "–"))
    if not can_upload(session, project, actor):
        raise Forbidden(_("Project %(project)s: only the owner, admins and members with the data right may upload "
                          "datasets", project=project.project_key), code="data_upload_forbidden")
    if project.status not in UPLOAD_PROJECT_STATUSES:
        raise Unprocessable(_("Project %(project)s is archived", project=project.project_key))
    return project


@dataclass
class Stored:
    dataset: Dataset
    duplicate_of: str | None = None


def store(session: Session, actor: Actor, project: Project, upload: PendingUpload, form: UploadForm,
          via: str = "web") -> Stored:
    """Create the dataset row for a finished upload, wrap its DEK and move the ciphertext into the store. The
    caller commits; on any error it must `abort` the upload and remove `storage_path` (see `discard`)."""
    if not upload.finished:
        raise RuntimeError("upload not finished")
    if upload.size == 0:
        raise Unprocessable(_("The file is empty"), code="dataset_empty")
    old = None
    if form.supersedes:
        old = get(session, form.supersedes)
        if old.project_id != project.id or old.status == "deleted":
            raise Unprocessable(_("%(key)s cannot be replaced from this project", key=form.supersedes),
                                code="invalid_dataset_field")
    duplicate = session.scalar(select(Dataset.dataset_key).where(
        Dataset.project_id == project.id, Dataset.sha256 == upload.sha256, Dataset.status != "deleted").limit(1))
    days = form.retention_days or retention_days(session)
    ds = Dataset(dataset_key=f"new-{secrets.token_hex(6)}", project_id=project.id, name=form.name,
                 data_class=form.data_class, status="receiving", original_filename=upload.filename,
                 media_type=upload.media_type, size_bytes=upload.size, sha256=upload.sha256,
                 supersedes_id=old.id if old else None, upload_channel="web" if via == "web" else "sftp",
                 uploaded_by=actor.id, retention_until=date.today() + timedelta(days=days),
                 min_group_size=form.min_group_size)
    session.add(ds)
    session.flush()
    ds.dataset_key = f"DS-{ds.id:04d}"
    ds.dek_wrapped = data_crypto.wrap_dek(kek(), upload.dek, ds.dataset_key)
    ds.storage_path = f"{STORE}/{ds.dataset_key}.enc"
    target = resolve(ds.storage_path)
    os.replace(upload.path, target)  # Same file system (both below TBR_DATA_DIR): atomic
    upload.stored_path = ds.storage_path
    ds.status = "quarantine"  # Stored; the profiler (DZ 7) moves it on to profiling/ready.
    data_audit.append(session, "dataset_uploaded", "dataset", ds.id, actor=actor, via=via, detail={
        "dataset": ds.dataset_key, "project": project.project_key, "data_class": ds.data_class,
        "size_bytes": ds.size_bytes, "sha256": ds.sha256, "retention_until": ds.retention_until.isoformat(),
        "supersedes": old.dataset_key if old else None})
    if old is not None and old.status != "archived":
        old.status = "archived"
        data_audit.append(session, "dataset_archived", "dataset", old.id, actor=actor, via=via,
                          detail={"dataset": old.dataset_key, "replaced_by": ds.dataset_key})
    session.flush()
    from app.services import dataset_profiles
    dataset_profiles.enqueue(session, ds, actor, via=via)
    return Stored(ds, duplicate)


def discard(storage_path: str | None) -> None:
    """Remove a stored file after the DB transaction failed (only ciphertext, no shredding needed)."""
    if storage_path:
        try:
            resolve(storage_path).unlink(missing_ok=True)
        except (OSError, Unprocessable):
            log.exception("dataset_discard_failed", extra={"path": storage_path})


# --- Deletion and retention ------------------------------------------------------------------------------------

def _shred(path: Path) -> bool:
    """Overwrite the ciphertext once, then remove it. The real guarantee is the deleted DEK (crypto-shredding):
    copies in backups or on SSDs stay unreadable without it."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return True
    with open(path, "r+b") as f:
        left = size
        while left > 0:
            n = min(left, data_crypto.CHUNK_SIZE)
            f.write(secrets.token_bytes(n))
            left -= n
        f.flush()
        os.fsync(f.fileno())
    path.unlink()
    return True


def delete(session: Session, ds: Dataset, actor: Actor | None, via: str = "web", reason: str = "manual") -> None:
    """Crypto-shred the dataset: drop the wrapped DEK, mark it deleted, overwrite and remove the file."""
    if ds.status == "deleted":
        return
    project = session.get(Project, ds.project_id)
    if actor is not None and not can_delete(project, actor):
        raise Forbidden(_("Only the project owner and admins may delete datasets"), code="data_delete_forbidden")
    ds.dek_wrapped = None
    ds.status = "deleted"
    ds.deleted_at = data_audit.now().replace(microsecond=0)
    ds.deleted_by = actor.id if actor else None
    session.flush()
    removed = True
    if ds.storage_path:
        try:
            removed = _shred(resolve(ds.storage_path))
        except (OSError, Unprocessable):
            removed = False
            log.exception("dataset_file_not_removed", extra={"dataset": ds.dataset_key})
    data_audit.append(session, "dataset_deleted", "dataset", ds.id, actor=actor, via=via,
                      detail={"dataset": ds.dataset_key, "reason": reason, "file_removed": removed})


def retention_sweep(session: Session, today: date | None = None) -> dict:
    """Scheduler job (APScheduler in the app, or `tbr-admin data retention` from a timer): remind the owner
    `WARN_DAYS` before the retention date, delete datasets past it. Idempotent and guarded by a named DB lock,
    so two triggers never run it at once. Each dataset commits on its own."""
    if not enabled(session):
        return {}
    # Named locks belong to a connection; the session may switch connections at each commit, so the lock is held
    # on a connection of its own.
    lock = session.get_bind().connect()
    if lock.scalar(text("SELECT GET_LOCK(:n, 0)"), {"n": RETENTION_LOCK}) != 1:
        lock.close()
        return {"skipped": "locked"}
    today = today or date.today()
    result = {"warned": 0, "deleted": 0, "failed": 0}
    try:
        due = session.scalars(select(Dataset).where(
            Dataset.status.in_(LIVE_STATUSES), Dataset.retention_until.is_not(None),
            Dataset.retention_until <= today + timedelta(days=WARN_DAYS)).order_by(Dataset.id)).all()
        for ds in due:
            try:
                if ds.retention_until < today:
                    delete(session, ds, None, via="scheduler", reason="retention")
                    result["deleted"] += 1
                elif ds.retention_warned_at is None:
                    _warn(session, ds)
                    result["warned"] += 1
                session.commit()
            except Exception:  # noqa: BLE001 – one broken dataset must not stop the others
                session.rollback()
                result["failed"] += 1
                log.exception("data_retention_failed", extra={"dataset": ds.dataset_key})
    finally:
        lock.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": RETENTION_LOCK})
        lock.close()
    return {k: v for k, v in result.items() if v}


def _warn(session: Session, ds: Dataset) -> None:
    from app.services import notifications

    ds.retention_warned_at = data_audit.now().replace(microsecond=0)
    project = session.get(Project, ds.project_id)
    notifications.dataset_event(session, project, ds.dataset_key, "dataset_retention")
    data_audit.append(session, "dataset_retention_warning", "dataset", ds.id, via="scheduler",
                      detail={"dataset": ds.dataset_key, "retention_until": ds.retention_until.isoformat()})


# --- Emergency download ----------------------------------------------------------------------------------------

def open_download(session: Session, ds: Dataset, actor: Actor, note: str):
    """Audit an emergency download and return (path, dek) for streaming. Admin with data right only."""
    if not can_download(actor):
        raise Forbidden(_("Only admins with the data right may download raw data"), code="data_download_forbidden")
    if ds.status == "deleted" or not ds.dek_wrapped or not ds.storage_path:
        raise Unprocessable(_("%(key)s was deleted", key=ds.dataset_key), code="dataset_deleted")
    note = " ".join((note or "").split())[:300]
    if not note:
        raise Unprocessable(_("Give a reason for the download"), code="download_reason_required")
    try:
        dek = data_crypto.unwrap_dek(kek(), ds.dek_wrapped, ds.dataset_key)
    except (data_crypto.DecryptError, data_crypto.DataKeyError) as e:
        raise Unprocessable(str(e), code="dataset_key_mismatch") from e
    data_audit.append(session, "dataset_downloaded", "dataset", ds.id, actor=actor, via="web",
                      detail={"dataset": ds.dataset_key, "reason": note})
    return resolve(ds.storage_path), dek


def plaintext_chunks(path: Path, dek: bytes):
    with open(path, "rb") as f:
        yield from data_crypto.decrypt_chunks(f, dek)


# --- Agent read (DZ 14) ----------------------------------------------------------------------------------------

def open_agent_read(session: Session, ds: Dataset, actor: Actor, via: str = "api"):
    """DZ 14: audit a local-model agent's read of a `local_only` dataset and return (path, dek) for streaming.
    The rights check goes through the zone helper (`zones.may_read_dataset`); every read is written to the data
    audit before the first byte leaves."""
    if actor.actor_type != "agent" or not actor.is_active:
        raise Forbidden(_("Only agents may read datasets over the API"), code="data_read_forbidden")
    project = session.get(Project, ds.project_id)
    if actor.role != "admin" and session.get(ProjectMember, (project.id, actor.id)) is None:
        raise NotFound(_("Dataset %(key)s not found", key=ds.dataset_key))
    if not zones.may_read_dataset(actor, ds.data_class, session):
        raise Forbidden(_("Dataset %(key)s is '%(cls)s' – only local-model agents may read local_only datasets, "
                          "and sealed datasets are never read by an LLM agent", key=ds.dataset_key,
                          cls=ds.data_class), code="data_read_forbidden")
    if ds.status == "deleted" or not ds.dek_wrapped or not ds.storage_path:
        raise Unprocessable(_("%(key)s was deleted", key=ds.dataset_key), code="dataset_deleted")
    try:
        dek = data_crypto.unwrap_dek(kek(), ds.dek_wrapped, ds.dataset_key)
    except (data_crypto.DecryptError, data_crypto.DataKeyError) as e:
        raise Unprocessable(str(e), code="dataset_key_mismatch") from e
    data_audit.append(session, "dataset_read", "dataset", ds.id, actor=actor, via=via,
                      detail={"dataset": ds.dataset_key, "data_class": ds.data_class})
    return resolve(ds.storage_path), dek


def retention_left(ds: Dataset, today: date | None = None) -> int | None:
    if ds.retention_until is None:
        return None
    return (ds.retention_until - (today or date.today())).days


def uploaded_names(session: Session, rows: list[Dataset]) -> dict[int, str]:
    ids = {r.uploaded_by for r in rows} | {r.deleted_by for r in rows if r.deleted_by}
    return dict(session.execute(select(Actor.id, Actor.name).where(Actor.id.in_(ids))).all()) if ids else {}


def audit_actor_names(session: Session, rows) -> dict[int, str]:
    ids = {r.actor_id for r in rows if r.actor_id}
    return dict(session.execute(select(Actor.id, Actor.name).where(Actor.id.in_(ids))).all()) if ids else {}
