"""Import service for the native SFTP inbox (research/konzept-datenzonen.md §4).

A deliverer drops a finished export into `<TBR_SFTP_DIR>/<user>/upload/`. A systemd path unit
(`deploy/systemd/taskbrigade-data-import.{path,service}`) starts `tbr-admin data import`; that command calls
`scan()` here. `scan()` encrypts every finished file into the data store (`app/services/datasets.py`,
`app/data_crypto.py`), writes the `dataset_uploaded` audit entry with `upload_channel = "sftp"`, removes the
plaintext and records the delivery on the account. The plaintext never leaves the upload folder except into the
encrypting writer – the ciphertext goes to `TBR_DATA_DIR/incoming`, the dataset row to the store.

"Finished" = the file has not changed for `SETTLE_SECONDS` or its name ends in `.done` (concept §4). Import runs
outside a request (CLI/timer), so it may use the KEK and the store directly. It never raises on a single bad file:
the file is left in place and reported, so the next run retries.
"""
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import data_crypto
from app.errors import Unprocessable
from app.i18n import _
from app.models import Actor, DataSftpAccount, Project
from app.services import data_audit, data_sftp, datasets, notifications

log = logging.getLogger(__name__)

SETTLE_SECONDS = 60
DONE_SUFFIXES = (".done", ".ready")
IMPORT_LOCK = "tbr_data_import"
COPY_CHUNK = data_crypto.CHUNK_SIZE
# Media types guessed from the extension; the import never reads the file to decide.
MEDIA_TYPES = {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".txt": "text/plain",
               ".json": "application/json", ".xml": "application/xml", ".xlsx":
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               ".xls": "application/vnd.ms-excel", ".zip": "application/zip",
               ".gz": "application/gzip", ".parquet": "application/vnd.apache.parquet"}


@dataclass
class Imported:
    """The result of one file import; `dataset_key` is empty when the file was skipped or failed."""
    username: str
    filename: str
    dataset_key: str | None = None
    size_bytes: int = 0
    duplicate: str | None = None
    error: str | None = None
    skipped: str | None = None


class ImportedUpload(datasets.PendingUpload):
    """A `PendingUpload` fed from a plaintext file on disk: the source is encrypted into `incoming/` record by
    record (nothing in plaintext is copied into `TBR_DATA_DIR`), then the caller finishes and `store()`s it."""

    def __init__(self, source: Path, filename: str | None, media_type: str | None, limit: int):
        super().__init__(filename, media_type, limit)
        self.source = source

    def read_from_source(self) -> None:
        # EncryptingWriter keeps back a full chunk, so a file read in `COPY_CHUNK` steps fits.
        with open(self.source, "rb") as f:
            while True:
                chunk = f.read(COPY_CHUNK)
                if not chunk:
                    break
                self.write(chunk)
        self.finish()


def upload_dir(account: DataSftpAccount) -> Path:
    return data_sftp.upload_path(account.username)


def _is_done(path: Path, now: float) -> bool:
    if path.name.endswith(DONE_SUFFIXES):
        return True
    try:
        return now - path.stat().st_mtime >= SETTLE_SECONDS
    except OSError:
        return False


def _plain_name(name: str) -> str:
    for suffix in DONE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _media_type(filename: str) -> str | None:
    return MEDIA_TYPES.get(Path(filename).suffix.lower())


def pending_files(account: DataSftpAccount, now: float | None = None) -> list[Path]:
    """Finished, importable files of one account: regular files, not hidden, no partial `.*part`/`.*tmp`."""
    folder = upload_dir(account)
    if not folder.is_dir():
        return []
    now = time.time() if now is None else now
    out = []
    for entry in sorted(folder.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.name.endswith((".part", ".tmp", ".crdownload", ".swp")):
            continue
        if _is_done(entry, now):
            out.append(entry)
    return out


def import_file(session: Session, account: DataSftpAccount, path: Path) -> Imported:
    """Import one finished file for one account. Commits the dataset in the caller's transaction; on error the
    row is rolled back and the file is left in place (the caller commits/rolls back and unlinks on success)."""
    name = _plain_name(path.name)
    project = session.get(Project, account.project_id)
    actor = session.get(Actor, account.owner_id)
    if project is None or actor is None:
        return Imported(account.username, name, error=_("Project or owner of %(user)s is missing", user=account.username))
    problem = datasets.problem(session)
    if problem:
        return Imported(account.username, name, error=problem)
    try:
        upload = ImportedUpload(path, name, _media_type(name), datasets.max_upload_bytes(session))
        try:
            upload.read_from_source()
        except Unprocessable as e:
            upload.abort()
            return Imported(account.username, name, error=str(e))
        if upload.size == 0:
            upload.abort()
            return Imported(account.username, name, error=_("The file is empty"))
        form = datasets.UploadForm(name=name[:200] or name, data_class="sealed")
        stored = datasets.store(session, actor, project, upload, form, via="sftp")
    except Exception as e:  # noqa: BLE001 – one bad file must not stop the others
        session.rollback()
        # If the ciphertext was already moved into the store but the commit failed, remove the orphan.
        datasets.discard(locals().get("upload").stored_path if "upload" in locals() else None)
        log.exception("data_import_failed", extra={"user": account.username, "file": name})
        return Imported(account.username, name, error=str(e))
    # The plaintext is only removed once the ciphertext is in the store and the row is created.
    try:
        path.unlink()
    except OSError:
        log.warning("data_import_plaintext_not_removed", extra={"user": account.username, "file": str(path)})
    return Imported(account.username, name, dataset_key=stored.dataset.dataset_key,
                    size_bytes=stored.dataset.size_bytes, duplicate=stored.duplicate_of)


def _touch_account(session: Session, account: DataSftpAccount) -> None:
    account.last_import_at = data_audit.now().replace(microsecond=0)
    session.flush()


def scan(session: Session, *, now: float | None = None, notify: bool = True) -> dict:
    """Import every finished file of every enabled account. Guarded by the database lock `tbr_data_import`, so
    two triggers (path unit + timer) never import the same file at once. Each file commits on its own."""
    if not datasets.enabled(session):
        return {"skipped": "disabled"}
    lock = session.get_bind().connect()
    if lock.scalar(text("SELECT GET_LOCK(:n, 0)"), {"n": IMPORT_LOCK}) != 1:
        lock.close()
        return {"skipped": "locked"}
    result = {"imported": 0, "failed": 0, "skipped": 0, "users": 0}
    imported: list[Imported] = []
    try:
        for account in data_sftp.all_accounts(session):
            if not account.enabled:
                continue
            files = pending_files(account, now)
            if not files:
                continue
            result["users"] += 1
            for path in files:
                outcome = import_file(session, account, path)
                if outcome.error:
                    result["failed"] += 1
                    session.rollback()
                else:
                    result["imported"] += 1
                    imported.append(outcome)
                    _touch_account(session, account)
                    if notify and outcome.dataset_key:
                        notifications.dataset_event(session, session.get(Project, account.project_id),
                                                    outcome.dataset_key, "dataset_uploaded")
                    session.commit()
    finally:
        lock.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": IMPORT_LOCK})
        lock.close()
    if imported:
        result["files"] = [{"user": i.username, "name": i.filename, "dataset": i.dataset_key,
                            "size_bytes": i.size_bytes, "duplicate": i.duplicate} for i in imported]
    return result


def status(session: Session) -> dict:
    """Report for `tbr-admin data sftp-user list --pending`: accounts and their waiting files (no DB writes, no
    decryption)."""
    users = []
    for account in data_sftp.all_accounts(session):
        project = session.get(Project, account.project_id)
        users.append({"user": account.username, "project": project.project_key if project else "?",
                      "enabled": bool(account.enabled), "provisioned": data_sftp.provisioned(account),
                      "pending": [p.name for p in pending_files(account)] if account.enabled else [],
                      "last_import_at": account.last_import_at})
    return {"users": users}
