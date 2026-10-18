"""Profiles stay local until an authorized human releases them. Callers own transactions."""
import csv
import io
import json
from pathlib import Path

from sqlalchemy import or_, select

from app import data_crypto
from app.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.i18n import _
from app.models import Actor, DataAudit, Dataset, DatasetProfile, Project, Task
from app.runner.profiler import fingerprint
from app.services import data_audit, datasets

MAX_PROFILE_BYTES = 4 * 1024 * 1024
SUPPORTED = (".csv", ".tsv", ".xlsx")


def enqueue(session, ds, actor, via="web"):
    from app.services import tasks
    if Path(ds.original_filename or "").suffix.lower() not in SUPPORTED:
        return None
    project = session.get(Project, ds.project_id)
    # The uploader already passed dataset upload rights; task creation is an internal operation.
    owner = session.get(Actor, project.owner_id)
    task = tasks.create(session, owner, project=project.project_key, title=_("Profile %(key)s", key=ds.dataset_key),
                        description=_("Generate a schema and synthetic sample in the local sandbox.")
                        + f"\n\n[{ds.dataset_key}](/data/{ds.dataset_key})",
                        acceptance_criteria=_("A human must release the profile before cloud use."),
                        task_kind="profile", data_class="sealed", run={"dataset": ds.dataset_key})
    ds.status = "profiling"
    data_audit.append(session, "profile_queued", "dataset", ds.id, actor=actor, via=via,
                      detail={"dataset": ds.dataset_key, "task": task.task_nr})
    return task


def latest(session, ds):
    return session.scalar(select(DatasetProfile).where(DatasetProfile.dataset_id == ds.id)
                          .order_by(DatasetProfile.id.desc()).limit(1))


def audit_entries(session, ds):
    ids = select(DatasetProfile.id).where(DatasetProfile.dataset_id == ds.id)
    return list(session.scalars(select(DataAudit).where(or_(
        (DataAudit.object_type == "dataset") & (DataAudit.object_id == ds.id),
        (DataAudit.object_type == "profile") & DataAudit.object_id.in_(ids)))
        .order_by(DataAudit.id.desc()).limit(200)))


def ingest(session, result, runner):
    """Read only authenticated encrypted artifacts of the trusted profiler; never parse raw data here."""
    files = {f["name"]: f for f in json.loads(result.files_json or "[]") if f["kind"] == "output"}
    if not {"schema.json", "sample.csv", "result.json"} <= files.keys() or not result.dek_wrapped:
        raise ValueError("missing profile files")
    dek = data_crypto.unwrap_dek(datasets.kek(), result.dek_wrapped, f"run-result/{result.job_id}")

    def read(name):
        import hashlib
        f = files[name]
        if not 0 <= f["size"] <= MAX_PROFILE_BYTES:
            raise ValueError("profile size")
        data = bytearray()
        with datasets.resolve(f["path"]).open("rb") as src:
            for chunk in data_crypto.decrypt_chunks(src, dek):
                data.extend(chunk)
                if len(data) > MAX_PROFILE_BYTES:
                    raise ValueError("profile size")
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ValueError("profile checksum")
        return data.decode("utf-8")

    schema_text, sample_text, meta = read("schema.json"), read("sample.csv"), json.loads(read("result.json"))
    schema = json.loads(schema_text)
    ds = session.get(Dataset, result.dataset_id)
    if schema["format"] != "tbr-schema/1" or schema["dataset"] != ds.dataset_key:
        raise ValueError("profile identity")
    sha = fingerprint(schema)
    if sha != meta["schema_fingerprint"] or meta["format"] != "tbr-profile/1":
        raise ValueError("profile fingerprint")
    row = DatasetProfile(dataset_id=ds.id, task_id=result.task_id, profiler_version=meta["profiler_version"],
                         schema_json=schema_text, sample_csv=sample_text, schema_fingerprint=sha, status="generated")
    session.add(row)
    session.flush()
    data_audit.append(session, "profile_generated", "profile", row.id, actor=runner, via="runner",
                      task_id=result.task_id, detail={"dataset": ds.dataset_key, "fingerprint": sha})
    return row


def apply_aliases(schema, sample_text, aliases):
    """Aliases are indexed by sheet/column positions; original names stay in local aliases_json only."""
    original = {"sheets": [{"name": s["name"], "columns": [{"name": c["name"]} for c in s["columns"]]}
                            for s in schema["sheets"]]}
    reader = list(csv.reader(io.StringIO(sample_text)))
    offset = 0
    for i, sheet in enumerate(schema["sheets"]):
        sheet["name"] = aliases.get(f"s{i}", sheet["name"])
        for j, col in enumerate(sheet["columns"]):
            col["name"] = aliases.get(f"s{i}c{j}", col["name"])
        names = [c["name"] for c in sheet["columns"]]
        if len(set(names)) != len(names):
            raise Unprocessable(_("Aliases must be unique within each sheet."), code="invalid_alias")
        reader[offset] = names
        offset += 21
    if len({s["name"] for s in schema["sheets"]}) != len(schema["sheets"]):
        raise Unprocessable(_("Sheet aliases must be unique."), code="invalid_alias")
    out = io.StringIO(newline="")
    csv.writer(out).writerows(reader)
    return original, out.getvalue()


def decide(session, ds, actor, decision, aliases=None, profile_id=None):
    project = session.get(Project, ds.project_id)
    if not datasets.can_upload(session, project, actor):
        raise Forbidden(_("Only humans with dataset rights may decide profiles."), code="profile_forbidden")
    # Lock the dataset first to serialize decisions, even if a caller has an older snapshot.
    session.refresh(ds, with_for_update=True)
    row = session.scalar(select(DatasetProfile).where(DatasetProfile.dataset_id == ds.id)
                         .order_by(DatasetProfile.id.desc()).limit(1).with_for_update()
                         .execution_options(populate_existing=True))
    if (row is None or row.status != "generated" or ds.status == "deleted"
            or (profile_id is not None and row.id != profile_id)):
        raise Conflict(_("This profile is no longer awaiting a decision."), code="profile_decided")
    if decision not in ("release", "reject"):
        raise Unprocessable(_("Choose release or reject."), code="invalid_decision")
    if decision == "release":
        from app.services import output_gate  # The project option of DZ 8 (NULL = admin default)

        if ds.data_class == "sealed" and output_gate.four_eyes(session, project) and actor.id == ds.uploaded_by:
            raise Forbidden(_("A second human must release a sealed profile; the uploader cannot release it."),
                            code="profile_four_eyes")
        aliases = aliases or {}
        schema = json.loads(row.schema_json)
        allowed = {f"s{i}" for i, s in enumerate(schema["sheets"])} | {
            f"s{i}c{j}" for i, s in enumerate(schema["sheets"]) for j, c in enumerate(s["columns"])}
        if not isinstance(aliases, dict) or any(k not in allowed or not isinstance(v, str) or not v.strip()
                                               or len(v) > 200 or any(ord(c) < 32 for c in v)
                                               for k, v in aliases.items()):
            raise Unprocessable(_("Invalid column or sheet alias."), code="invalid_alias")
        excel = Path(ds.original_filename or "").suffix.lower() == ".xlsx"
        if excel and any(
                len(v) > 31 or any(c in v for c in "[]:*?/\\") for k, v in aliases.items() if "c" not in k):
            raise Unprocessable(_("Invalid column or sheet alias."), code="invalid_alias")
        original, sample = apply_aliases(schema, row.sample_csv, aliases)
        if excel and len({s["name"].casefold() for s in schema["sheets"]}) != len(schema["sheets"]):
            raise Unprocessable(_("Sheet aliases must be unique."), code="invalid_alias")
        row.aliases_json = json.dumps({"original": original, "aliases": aliases})
        row.schema_json, row.sample_csv = json.dumps(schema), sample
        row.schema_fingerprint = fingerprint(schema)
        row.status, row.released_by, row.released_at = "released", actor.id, data_audit.now()
        from app.services import data_refs  # Draft "Write analysis script" with the schema reference (DZ 9)

        origin = session.get(Task, row.task_id) if row.task_id else None
        if not origin or not origin.workflow_run_step_id:
            draft = data_refs.propose_script(session, row, ds, actor)
            row.code_task_id = draft.id
        ds.status = "ready" if ds.status != "archived" else "archived"
        if ds.status == "ready":
            from app.services.workflows.events import emit_dataset_ready
            emit_dataset_ready(session, ds)
    else:
        row.status = "rejected"
        if ds.status != "archived":
            ds.status = "quarantine"
    data_audit.append(session, "profile_released" if decision == "release" else "profile_rejected", "profile",
                      row.id, actor=actor, detail={"dataset": ds.dataset_key, "fingerprint": row.schema_fingerprint})
    task = session.get(Task, row.task_id) if row.task_id else None
    if task and task.status == "for_review":
        from app.services.state_machine import transition
        transition(session, task, "approve" if decision == "release" else "reject", actor,
                   {"_dataset_profile": row, "body": _("Profile rejected by a human.")})
    elif task and task.workflow_run_step_id:
        from app.services.workflows.engine import on_task_transition

        on_task_transition(session, task, "profile_decision", actor, {"_dataset_profile": row})
    return row


def visible(session, profile_id, actor):
    row = session.get(DatasetProfile, profile_id)
    if row is None:
        raise NotFound(_("Profile not found"))
    ds = session.get(Dataset, row.dataset_id)
    project = session.get(Project, ds.project_id)
    if datasets.can_view(session, project, actor) and (
            row.status == "released" or datasets.can_upload(session, project, actor)):
        return row
    from app.services import data_refs  # Agents: only through an open task of their own that references it

    if row.status == "released" and data_refs.agent_may_read(session, actor, "profile", row.id):
        return row
    if actor.actor_type == "agent" and actor.is_active and row.status == "released":
        from app.models import WorkflowRunStep

        # Engine-written input snapshots scope large released profiles to their active workflow consumer.
        inputs = session.scalars(select(WorkflowRunStep.output_json).join(Task, Task.id == WorkflowRunStep.task_id)
                                 .where(Task.project_id == project.id, Task.assigned_to == actor.id,
                                        Task.status == "in_progress", WorkflowRunStep.status == "active"))
        if any(row.id in json.loads(value or "{}").get("input_profiles", []) for value in inputs):
            return row
    raise NotFound(_("Profile not found"))
