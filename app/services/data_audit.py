"""Append-only data audit with a SHA-256 hash chain (research/konzept-datenzonen.md §6, guide docs/data-store.md).

Each row stores `prev_hash` (the hash of the row before it) and `hash = SHA-256(prev_hash || canonical row)`.
Changing, deleting or inserting a row in the middle breaks the chain; `verify` finds the first broken row.
`data_audit_head` holds the end of the chain, so cutting rows off the end is detected too. Rows never hold data
values: names of datasets, file names and cell contents stay out of `detail`.

Writers lock the head row in the caller's transaction; the audit row commits or rolls back with the action it
records.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AUDIT_OBJECT_TYPES, AUDIT_VIA, Actor, DataAudit, DataAuditHead

GENESIS = "0" * 64
HEAD_ID = 1
TRIGGERS = ("data_audit_no_update", "data_audit_no_delete")


def now() -> datetime:
    """Naive UTC with microseconds (`DATETIME(6)` keeps them, so the hash input survives the round trip)."""
    return datetime.now(UTC).replace(tzinfo=None)


def canonical(row) -> str:
    """The hashed form of a row: every column except the hashes, in a fixed order."""
    return json.dumps([row.id, row.at.strftime("%Y-%m-%dT%H:%M:%S.%f"), row.actor_id, row.action, row.object_type,
                       row.object_id, row.task_id, row.detail_json, row.via],
                      ensure_ascii=False, separators=(",", ":"))


def row_hash(prev_hash: str, row) -> str:
    return hashlib.sha256((prev_hash + canonical(row)).encode()).hexdigest()


def _head(session: Session) -> DataAuditHead:
    head = session.scalars(select(DataAuditHead).where(DataAuditHead.id == HEAD_ID).with_for_update()).first()
    if head is None:  # Only if someone removed the seeded row; recreate it at the end of the existing chain.
        last = session.scalars(select(DataAudit).order_by(DataAudit.id.desc()).limit(1)).first()
        head = DataAuditHead(id=HEAD_ID, last_id=last.id if last else 0, last_hash=last.hash if last else GENESIS)
        session.add(head)
        session.flush()
    return head


def append(session: Session, action: str, object_type: str, object_id: int, *, actor: Actor | None = None,
           via: str = "web", task_id: int | None = None, detail: dict | None = None) -> DataAudit:
    """Add one audit row in the caller's transaction. `detail` must never contain data values."""
    if object_type not in AUDIT_OBJECT_TYPES or via not in AUDIT_VIA:
        raise ValueError(f"invalid audit object type or channel: {object_type}/{via}")
    head = _head(session)
    row = DataAudit(id=head.last_id + 1, at=now(), actor_id=actor.id if actor else None, action=action[:40],
                    object_type=object_type, object_id=object_id or 0, task_id=task_id,
                    detail_json=json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
                    via=via, prev_hash=head.last_hash)
    row.hash = row_hash(row.prev_hash, row)
    session.add(row)
    head.last_id, head.last_hash = row.id, row.hash
    session.flush()
    return row


@dataclass
class Verdict:
    ok: bool
    count: int
    last_hash: str
    bad_id: int | None = None
    reason: str | None = None


def verify_rows(rows, head: tuple[int, str] | None = None) -> Verdict:
    """Check the chain over `rows` (ordered by id). `head` = (last_id, last_hash) of `data_audit_head`."""
    prev, count, last_id = GENESIS, 0, 0
    for row in rows:
        if row.prev_hash != prev:
            return Verdict(False, count, prev, row.id, "prev_hash")  # A row before it was removed or inserted
        if row_hash(prev, row) != row.hash:
            return Verdict(False, count, prev, row.id, "hash")  # The row itself was changed
        prev, last_id, count = row.hash, row.id, count + 1
    if head is not None and (head[0] != last_id or head[1] != prev):
        return Verdict(False, count, prev, last_id or None, "head")  # Rows at the end were removed
    return Verdict(True, count, prev)


def verify(session: Session) -> Verdict:
    # The head first: the rows are streamed, and no other query may run on the connection meanwhile.
    head = session.get(DataAuditHead, HEAD_ID)
    expected = (head.last_id, head.last_hash) if head else None
    rows = session.execute(select(DataAudit).order_by(DataAudit.id).execution_options(yield_per=1000)).scalars()
    return verify_rows(rows, expected)


def triggers_present(session: Session) -> list[str]:
    """Names of the append-only triggers that exist (both expected)."""
    found = session.scalars(text("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                                 "WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = 'data_audit'"))
    return sorted(set(found) & set(TRIGGERS))


def for_object(session: Session, object_type: str, object_id: int, limit: int = 200) -> list[DataAudit]:
    return list(session.scalars(select(DataAudit).where(DataAudit.object_type == object_type,
                                                        DataAudit.object_id == object_id)
                                .order_by(DataAudit.id.desc()).limit(limit)))


def detail(row: DataAudit) -> dict:
    try:
        return json.loads(row.detail_json) if row.detail_json else {}
    except ValueError:
        return {}
