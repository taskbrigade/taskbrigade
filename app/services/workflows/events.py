"""Outbox for workflow event triggers (research/konzept-workflows.md §1.5, §5.1, §6.2).

Producers only insert a row. The scheduler tick matches triggers and starts runs. A duplicate producer
call is ignored (`dedupe_key`). A repeated tick does not start a second run (`workflow_trigger_fires`).
"""
import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.errors import Conflict
from app.models import (
    DataSftpAccount, Dataset, Project, WorkflowEvent, WorkflowRun, WorkflowRunStep, WorkflowTemplate,
    WorkflowTrigger, WorkflowTriggerFire,
)
from app.services.workflows import api
from app.services.workflows.schedule import utc

log = logging.getLogger(__name__)

FILTER_KEYS = frozenset({"project", "sftp_account", "name_glob", "supersedes", "workflow"})
_FILENAME_DATE = re.compile(r"(20\d{2})[-_]?(\d{2})(?:[-_]?(\d{2}))?")
BATCH = 500


def filename_date(name):
    """First ISO-like date in a filename: `Buchungen_2026-09.xlsx` → `2026-09`, with a day when present."""
    match = _FILENAME_DATE.search(name or "")
    if not match:
        return None
    year, month, day = match.group(1), match.group(2), match.group(3)
    if not 1 <= int(month) <= 12:
        return None
    if day and not 1 <= int(day) <= 31:
        return None
    return f"{year}-{month}-{day}" if day else f"{year}-{month}"


def _insert(session, *, kind, project_id, payload, dedupe_key):
    row = WorkflowEvent(kind=kind, project_id=project_id, payload_json=json.dumps(payload, default=str),
                        dedupe_key=dedupe_key)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None
    return row


def emit_dataset_ready(session, ds):
    """Call when a dataset becomes `ready` (after profiling). Never raises into the caller."""
    if ds is None or ds.status != "ready":
        return None
    try:
        with session.begin_nested():
            project = session.get(Project, ds.project_id)
            account = None
            if ds.upload_channel == "sftp":
                account = session.scalar(select(DataSftpAccount.username).where(
                    DataSftpAccount.project_id == ds.project_id, DataSftpAccount.owner_id == ds.uploaded_by)
                    .order_by(DataSftpAccount.id.desc()).limit(1))
            superseded = session.get(Dataset, ds.supersedes_id) if ds.supersedes_id else None
            payload = {
                "project": project.project_key if project else None,
                "dataset": ds.id,
                "dataset_key": ds.dataset_key,
                "filename": ds.original_filename,
                "name": ds.name,
                "filename_date": filename_date(ds.original_filename or ds.name or ""),
                "upload_channel": ds.upload_channel,
                "sftp_account": account,
                "supersedes": superseded.dataset_key if superseded else None,
            }
            return _insert(session, kind="dataset_ready", project_id=ds.project_id, payload=payload,
                           dedupe_key=f"dataset_ready:{ds.id}")
    except Exception:
        log.exception("workflow_dataset_ready_failed", extra={"dataset_id": ds.id})
        return None


def emit_run_end(session, run, outcome=None):
    """`succeeded` when the run finishes. `failed` when a final step failure pauses it (WF 8 keeps it paused)."""
    kind = "workflow_succeeded" if (outcome or run.status) == "succeeded" else None
    if outcome == "failed" or run.status == "failed":
        kind = "workflow_failed"
    if kind is None or run.id is None:
        return None
    try:
        with session.begin_nested():
            template, project = run.template, run.project
            steps = {}
            rows = session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
                                   .order_by(WorkflowRunStep.position, WorkflowRunStep.attempt))
            for row in rows:
                raw = json.loads(row.output_json or "{}")
                output = raw.get("output") if isinstance(raw.get("output"), dict) else {}
                # Zone L/R values stay out of another run's stored parameters.
                if row.output_class in ("local_only", "sealed"):
                    output = {}
                steps[row.step.step_key] = {"status": row.status, "output": output}
            payload = {
                "project": project.project_key,
                "run": {
                    "id": run.id,
                    "seq": run.run_seq,
                    "key": template.workflow_key,
                    "project": project.project_key,
                    "workflow": f"{project.project_key}/{template.workflow_key}",
                    "status": "failed" if kind == "workflow_failed" else run.status,
                    "params": json.loads(run.params_json or "{}"),
                    "steps": steps,
                },
            }
            return _insert(session, kind=kind, project_id=run.project_id, payload=payload,
                           dedupe_key=f"{kind}:{run.id}")
    except Exception:
        log.exception("workflow_run_end_failed", extra={"run_id": run.id})
        return None


def _filter_ok(filt, payload):
    if "project" in filt and filt["project"] != payload.get("project"):
        return False
    if "sftp_account" in filt and filt["sftp_account"] != (payload.get("sftp_account") or ""):
        return False
    if "name_glob" in filt:
        import fnmatch
        filename = payload.get("filename") or ""
        if not fnmatch.fnmatchcase(filename, filt["name_glob"]):
            return False
    if "supersedes" in filt and filt["supersedes"] != (payload.get("supersedes") or ""):
        return False
    if "workflow" in filt:
        run = payload.get("run") or {}
        wanted = filt["workflow"]
        if wanted not in (run.get("workflow"), run.get("key")):
            return False
    return True


def matches(trigger, event, template, payload):
    if not trigger.enabled or trigger.kind != event.kind or template.status != "active":
        return False
    if template.project.status == "archived":
        return False
    try:
        filt = json.loads(trigger.filter_json or "{}")
        if not isinstance(filt, dict):
            return False
    except ValueError:
        return False
    same = template.project_id == event.project_id
    if trigger.kind == "dataset_ready" and not same:
        return False
    wanted = str(filt.get("workflow") or "")
    if not same and not (filt.get("project") or "/" in wanted):
        return False
    return _filter_ok(filt, payload)


def _params(trigger):
    try:
        raw = json.loads(trigger.params_json or "{}")
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _claim(session, trigger, event, now):
    try:
        with session.begin_nested():
            row = WorkflowTriggerFire(trigger_id=trigger.id, event_id=event.id, fired_at=now, outcome="failed")
            session.add(row)
            session.flush()
            return row
    except IntegrityError:
        return None


def _deliver(session, trigger, event, payload, now):
    template = session.get(WorkflowTemplate, trigger.template_id)
    if template is None or not matches(trigger, event, template, payload):
        return None
    source = session.get(Project, event.project_id)
    row = _claim(session, trigger, event, now)
    if row is None:
        return None
    if source is None or not api.can_read(session, template.owner, source):
        row.note = "forbidden"
        return row
    params = _params(trigger)
    if params is None:
        row.note = "params"
        return row
    parent = None
    if trigger.kind.startswith("workflow_"):
        parent_id = (payload.get("run") or {}).get("id")
        parent = session.get(WorkflowRun, parent_id) if parent_id else None
        if parent is None:
            row.note = "missing_parent"
            return row
    try:
        from app.services.workflows import engine
        with session.begin_nested():
            run = engine.start(session, template.owner, template, params=params,
                               trigger_kind="workflow" if parent is not None else "event",
                               trigger=trigger, parent_run=parent, event_payload=payload, now=now)
        row.run_id = run.id
        row.outcome = "queued" if run.status == "queued" else "started"
    except Conflict as exc:
        row.outcome = "skipped_overlap" if exc.code == "workflow_overlap" else "failed"
        row.note = (exc.code or "")[:200] or None
    except Exception as exc:
        row.outcome, row.note = "failed", type(exc).__name__[:200]
        log.error("workflow_trigger_failed", extra={"trigger_id": trigger.id, "event_id": event.id,
                                                    "error_type": type(exc).__name__})
    return row


def process(session, now=None):
    """Deliver unprocessed outbox rows. One transaction per event; a bad trigger does not block the rest."""
    now = utc(now or datetime.now(UTC))
    ids = list(session.scalars(select(WorkflowEvent.id).where(WorkflowEvent.processed_at.is_(None))
                               .order_by(WorkflowEvent.id).limit(BATCH)))
    session.commit()
    processed = failed = 0
    for event_id in ids:
        try:
            event = session.get(WorkflowEvent, event_id)
            if event is None or event.processed_at is not None:
                session.commit()
                continue
            try:
                payload = json.loads(event.payload_json or "{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                event.processed_at = now
                session.commit()
                processed += 1
                failed += 1
                continue
            triggers = list(session.scalars(select(WorkflowTrigger).where(
                WorkflowTrigger.kind == event.kind, WorkflowTrigger.enabled.is_(True))
                .order_by(WorkflowTrigger.id)))
            outcomes = []
            for trigger in triggers:
                fire = _deliver(session, trigger, event, payload, now)
                if fire is not None:
                    outcomes.append(fire.outcome)
            event.processed_at = now
            session.commit()
            processed += 1
            failed += "failed" in outcomes
        except Exception:
            session.rollback()
            failed += 1
            processed += 1
            log.exception("workflow_event_tick_failed", extra={"event_id": event_id})
    return processed, failed
