"""Reviewed script reuse and bounded repairs. All callers hold the template and run locks."""
import json

from sqlalchemy import or_, select

from app.errors import Conflict, Unprocessable
from app.i18n import _
from app.models import Dataset, DatasetProfile, RunResult, Task, WorkflowRun, WorkflowRunStep, WorkflowScriptPin
from app.services import data_audit, data_refs, datasets, runner_jobs, state_machine as sm


def config(step):
    from app.services.workflows.engine import _config

    value = _config(step)
    if not isinstance(value.get("path"), str):
        raise Unprocessable(_("Invalid workflow script configuration"), code="workflow_script_invalid")
    value["path"] = runner_jobs._script_path(value["path"])
    limit = value.get("max_repairs_per_run", 1)
    if (step.review_mode != "manual" or step.data_class not in (None, "cloud_ok") or step.optional
            or not isinstance(value.get("profile_from"), str)
            or isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 254):
        raise Unprocessable(_("Invalid workflow script configuration"), code="workflow_script_invalid")
    return value


def profile_can_stay_local(session, run, source):
    """Allow harmless intervening steps, but never skip approval needed by an earlier data consumer."""
    from app.services.workflows import engine, publish
    from app.services.workflows.placeholders import Placeholder, classify_path, parse_template

    for step in engine._steps(session, run):
        if step.position <= source.position:
            continue
        if step.step_type == "script" and engine._config(step).get("profile_from") == source.step.step_key:
            return True
        definition = publish._steps({"steps": [publish._step_dict(step)]}, [])[0]
        for location, text in publish._texts(definition):
            for part in parse_template(text):
                if isinstance(part, Placeholder):
                    ref = classify_path(part.path)
                    if ref.step == source.step.step_key and ref.builtin not in ("task_nr", "status"):
                        return False
    return False


def active_pin(session, template_id, step):
    return session.scalar(select(WorkflowScriptPin).where(
        WorkflowScriptPin.template_id == template_id, WorkflowScriptPin.step_key == step.step_key,
        WorkflowScriptPin.repo_path == config(step)["path"], WorkflowScriptPin.retired_at.is_(None))
        .order_by(WorkflowScriptPin.id.desc()).limit(1).with_for_update()
        .execution_options(populate_existing=True))


def profile_for(session, run, step):
    from app.services.workflows import engine

    key = config(step)["profile_from"]
    source = next((r for r in engine._latest(session, run).values()
                   if r.position < step.position and r.step.step_key == key
                   and r.step.step_type == "runner" and r.task and r.task.task_kind == "profile"), None)
    value = json.loads(source.output_json or "{}").get("profile") if source else None
    profile = session.get(DatasetProfile, value) if value else None
    if (profile is None or profile.task_id != source.task_id
            or profile.dataset_id != source.task.run_dataset_id
            or session.get(Dataset, profile.dataset_id).project_id != run.project_id):
        raise Unprocessable(_("Invalid workflow script configuration"), code="workflow_script_invalid")
    return profile


def schema_diff(old, new):
    """Only structural names/types/precision; never include values or distribution metadata."""
    def columns(profile):
        return {(sheet["name"], col["name"]): (col["type"], col.get("precision"))
                for sheet in json.loads(profile.schema_json)["sheets"] for col in sheet["columns"]}
    before, after = columns(old), columns(new)
    return {"added": [list(key) for key in sorted(after.keys() - before.keys())],
            "removed": [list(key) for key in sorted(before.keys() - after.keys())],
            "changed": [{"column": list(key), "before": before[key], "after": after[key]}
                        for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]}


def _open_repair(session, pin, row):
    attempts = session.scalars(select(WorkflowRunStep).join(Task, WorkflowRunStep.task_id == Task.id)
        .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id).where(
            WorkflowRun.template_id == pin.template_id,
            or_(Task.status.not_in(("done", "cancelled", "aborted")), WorkflowRunStep.next_retry_at.is_not(None)),
            WorkflowRunStep.id != row.id).with_for_update())
    # follow_up_of_id is editable task metadata; the engine's pin ID is authoritative.
    return next((r.task_id for r in attempts
                 if json.loads(r.output_json or "{}").get("repair_pin_id") == pin.id), None)


def matches(session, profile, pin):
    if profile.status == "rejected":
        return False
    if profile.schema_fingerprint == pin.schema_fingerprint:
        return True
    old = session.get(DatasetProfile, pin.profile_id)
    if profile.status != "generated" or not old.aliases_json:
        return False
    aliases = json.loads(old.aliases_json)
    schema = json.loads(profile.schema_json)
    # Reapply only previously approved aliases to the exact original header shape, locally.
    original = {"sheets": [{"name": s["name"], "columns": [{"name": c["name"]} for c in s["columns"]]}
                            for s in schema["sheets"]]}
    if original != aliases.get("original"):
        return False
    mapping = aliases["aliases"]
    for i, sheet in enumerate(schema["sheets"]):
        sheet["name"] = mapping.get(f"s{i}", sheet["name"])
        for j, col in enumerate(sheet["columns"]):
            col["name"] = mapping.get(f"s{i}c{j}", col["name"])
    from app.runner.profiler import fingerprint

    return fingerprint(schema) == pin.schema_fingerprint


def runner_profile(session, task):
    """Use the exact schema/aliases consumed by this run, including locally reused approved aliases."""
    from app.services.workflows import engine

    row = session.get(WorkflowRunStep, task.workflow_run_step_id)
    for source in engine._latest(session, row.run).values():
        if source.position >= row.position or source.step.step_type != "script":
            continue
        values = json.loads(source.output_json or "{}")
        if values.get("script") != {"path": task.run_script_path, "commit": task.run_commit_sha}:
            continue
        pin = session.get(WorkflowScriptPin, values["pin_id"])
        current = profile_for(session, row.run, source.step)
        if current.dataset_id != task.run_dataset_id or not matches(session, current, pin):
            raise Conflict(_("Workflow script schema changed before execution"), code="workflow_schema_changed")
        return current if current.status == "released" else session.get(DatasetProfile, pin.profile_id)
    return None


def prepare(session, run, row):
    """Return True when waiting/reused; False asks the engine to create a normal reviewed code task."""
    from app.services.workflows import engine

    cfg = config(row.step)
    if (row.step.data_class or run.project.privacy) != "cloud_ok":
        raise Unprocessable(_("Invalid workflow script configuration"), code="workflow_script_invalid")
    if not datasets.enabled(session):
        raise Unprocessable(_("Invalid workflow script configuration"), code="runner_without_data_zones")
    profile = profile_for(session, run, row.step)
    pin = active_pin(session, run.template_id, row.step)
    values = json.loads(row.output_json or "{}")
    forced = values.get("repair_reason")
    if profile.status == "rejected":
        engine._fail(session, run, row, "gate_rejected")
        return True
    ds = session.get(Dataset, profile.dataset_id)
    if ds.status in ("deleted", "archived", "receiving"):
        engine._fail(session, run, row, "dataset_unavailable")
        return True
    # Compare internally; no generated schema/sample is copied to a cloud step.
    if pin and matches(session, profile, pin) and not forced:
        source = session.get(Task, pin.source_task_id)
        if source.status != "done" or source.merge_commit_sha != pin.commit_sha:
            engine._fail(session, run, row, "script_not_merged")
            return True
        row.output_json = json.dumps({"script": {"path": pin.repo_path, "commit": pin.commit_sha}, "pin_id": pin.id})
        row.status, row.finished_at, row.output_class = "succeeded", sm.utcnow(), "cloud_ok"
        producer = session.get(Task, profile.task_id)
        if producer.status == "for_review" and profile.status == "generated":
            engine._internal_status(session, producer, "done", _("Profile kept local; reviewed script pin reused"))
            if ds.status == "profiling":
                ds.status = "ready"
                from app.services.workflows.events import emit_dataset_ready
                emit_dataset_ready(session, ds)
        return True
    if profile.status != "released":
        row.status, row.wait_started_at = "waiting", row.wait_started_at or sm.utcnow()
        # Fingerprints and generated profile IDs stay in the sealed producer row.
        row.output_class = "sealed"
        run.status = "waiting"
        return True
    if pin:
        if _open_repair(session, pin, row):
            engine._fail(session, run, row, "script_repair_open")
            return True
        prior = list(session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)))
        count = len({r.task_id for r in prior if r.id != row.id and r.task_id
                     and json.loads(r.output_json or "{}").get("repair_pin_id")})
        if not values.get("manual_repair") and count >= cfg.get("max_repairs_per_run", 1):
            engine._fail(session, run, row, "script_repair_limit")
            return True
        values.update(repair_pin_id=pin.id, repair_reason=forced or "schema_mismatch")
    values["input_profiles"] = [profile.id]
    row.output_json, row.output_class = json.dumps(values), "cloud_ok"
    return False


def describe_task(session, run, row, task):
    cfg = config(row.step)
    values = json.loads(row.output_json)
    profile = session.get(DatasetProfile, values["input_profiles"][0])
    profiles = [profile]
    description = (task.description or "") + "\n\n" + _("Script path: %(path)s", path=cfg["path"])
    if pin_id := values.get("repair_pin_id"):
        pin = session.get(WorkflowScriptPin, pin_id)
        old = session.get(DatasetProfile, pin.profile_id)
        if old.status != "released":
            raise Unprocessable(_("Invalid workflow script configuration"), code="workflow_data_unreleased")
        profiles.append(old)
        task.title = _("Repair script %(path)s for %(workflow)s #%(run)s", path=cfg["path"],
                       workflow=run.template.workflow_key, run=run.run_seq)[:200]
        task.follow_up_of_id = pin.source_task_id
        values["schema_diff"] = schema_diff(old, profile)
        diff = "\n\n" + _("Schema diff") + ":\n" + json.dumps(values["schema_diff"], ensure_ascii=False)
        description += diff if len((description + diff).encode()) <= 56000 else (
            "\n\n" + _("Schema diff") + f": /api/v1/workflow-runs/{run.id} (steps / outputs / schema_diff)")
        description += f"\n\nPin: {pin.repo_path}@{pin.commit_sha}\nReason: {values['repair_reason']}"
        if values.get("failed_checks"):
            description += "\n\n" + _("Failed gate checks") + ": " + json.dumps(values["failed_checks"])
    for item in profiles:
        data_refs.add(session, task, "profile", item.id, run.template.owner)
        text = f"\n\nprofile#{item.id}\n{item.schema_json}\n{item.sample_csv}"
        if len((description + text).encode()) <= 58000:
            description += text
        else:
            description += f"\n\n/api/v1/data/profiles/{item.id}"
    task.description = description
    row.output_json = json.dumps(values, ensure_ascii=False)


def pin_merged(session, run, row, task, actor):
    from app.services.workflows import engine

    if (not actor or actor.actor_type != "human" or not task.merge_commit_sha
            or not runner_jobs.SHA_RE.fullmatch(task.merge_commit_sha)):
        engine._fail(session, run, row, "script_not_merged")
        return False
    values = json.loads(row.output_json or "{}")
    profile = session.get(DatasetProfile, values.get("input_profiles", [None])[0])
    if profile is None or profile.status != "released":
        engine._fail(session, run, row, "workflow_data_unreleased")
        return False
    cfg = config(row.step)
    # Retire all paths for this key so changing away and back cannot resurrect a stale pin.
    for pin in session.scalars(select(WorkflowScriptPin).where(
            WorkflowScriptPin.template_id == run.template_id, WorkflowScriptPin.step_key == row.step.step_key,
            WorkflowScriptPin.retired_at.is_(None)).with_for_update()):
        pin.retired_at = sm.utcnow()
    pin = WorkflowScriptPin(template_id=run.template_id, step_key=row.step.step_key, repo_path=cfg["path"],
                            commit_sha=task.merge_commit_sha, schema_fingerprint=profile.schema_fingerprint,
                            profile_id=profile.id, source_task_id=task.id, pinned_by=actor.id)
    session.add(pin)
    session.flush()
    values.update(script={"path": pin.repo_path, "commit": pin.commit_sha}, pin_id=pin.id)
    row.output_json = json.dumps(values)
    data_audit.append(session, "script_pinned", "task", task.id, actor=actor, task_id=task.id,
                      detail={"pin": pin.id, "template": run.template_id, "step": row.step.step_key,
                              "commit": pin.commit_sha, "fingerprint": pin.schema_fingerprint})
    return True


def request_repair(session, run, failed, reason, result=None, *, automatic=True):
    """Restart at the producing script with fresh downstream attempts; preserve every historical output."""
    from app.services.workflows import engine
    from app.services.workflows.errors import stop_clock

    rows = engine._latest(session, run)
    # Use the commit actually consumed by this attempt, even after another run updates the pin.
    script = next((r for r in reversed(list(rows.values())) if r.position < failed.position
                   and r.step.step_type == "script" and json.loads(r.output_json or "{}").get("script", {}).get(
                       "commit") == (result.script_commit_sha if result and result.script_commit_sha
                                     else failed.task.run_commit_sha)), None)
    if script is None:
        return False
    pin_id = json.loads(script.output_json or "{}").get("pin_id")
    pin = session.get(WorkflowScriptPin, pin_id) if pin_id else None
    if pin is None:
        return False
    attempts = session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)).all()
    count = len({r.task_id for r in attempts if r.task_id and json.loads(r.output_json or "{}").get("repair_pin_id")})
    if automatic and count >= config(script.step).get("max_repairs_per_run", 1):
        engine._fail(session, run, failed, "script_repair_limit")
        return True
    if _open_repair(session, pin, script):
        if not automatic:
            raise Conflict(_("A repair task is already open for this script pin"), code="script_repair_open")
        engine._fail(session, run, failed, "script_repair_open")
        return True
    paused = run.status == "paused"
    for row in rows.values():
        if row.position < script.position:
            continue
        stop_clock(row, sm.utcnow())
        row.next_retry_at = None
        if row.status not in engine.COMPLETE:
            row.status, row.finished_at = "failed" if row.id == failed.id else "cancelled", sm.utcnow()
        if row.id == failed.id:
            row.error_code = reason
            if row.task and row.task.status in ("aborted", "changes_requested"):
                engine._internal_status(session, row.task, "cancelled", _("Workflow script repair requested"))
        if row.id != failed.id and row.task and row.task.status not in ("done", "cancelled", "aborted", "changes_requested"):
            engine._internal_status(session, row.task, "cancelled", _("Workflow script repair requested"))
        new = engine._attempt(session, run, row.step)
        if row.id == script.id:
            # Only numbered status codes cross into the repair task, never arbitrary runner strings.
            checks = [{"n": c.get("n"), "status": c.get("status")} for c in
                      json.loads(result.checks_json or "{}").get("checks", [])] if result else []
            new.output_json = json.dumps({"repair_reason": reason, "failed_checks": checks,
                                          "manual_repair": not automatic})
    run.current_position = script.position
    if not paused:
        run.status = "running"
        engine.advance(session, run)
    return True


def repair(session, actor, run):
    from app.services.workflows import engine

    run = engine._lock(session, run)
    engine._require_control(run, actor)
    if actor.actor_type != "human" or run.status != "paused":
        raise Conflict(_("Manual script repair requires a paused run and a human"), code="script_repair_forbidden")
    row = engine._latest(session, run)[run.current_position]
    if row.step.step_type == "gate":
        row = engine._latest(session, run)[row.position - 1]
    if row.step.step_type != "runner" or not row.task or row.task.task_kind != "run":
        raise Conflict(_("This step has no pinned script to repair"), code="script_repair_unavailable")
    result = session.scalar(select(RunResult).where(RunResult.task_id == row.task_id)
                            .order_by(RunResult.id.desc()).limit(1))
    if not request_repair(session, run, row, "manual_repair", result, automatic=False):
        raise Conflict(_("This step has no pinned script to repair"), code="script_repair_unavailable")
    run.status, run.pause_reason = "running", None
    engine.advance(session, run)
    return run


def retire(session, template, step_key, actor):
    from app.services.workflows.api import require_edit
    from app.models import WorkflowTemplate

    require_edit(actor, template.project)
    if actor.actor_type != "human":
        raise Conflict(_("Only humans may recreate workflow scripts"), code="script_human_required")
    session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template.id).with_for_update())
    for pin in session.scalars(select(WorkflowScriptPin).where(WorkflowScriptPin.template_id == template.id,
            WorkflowScriptPin.step_key == step_key, WorkflowScriptPin.retired_at.is_(None)).with_for_update()):
        pin.retired_at = sm.utcnow()
        data_audit.append(session, "script_retired", "task", pin.source_task_id, actor=actor,
                          detail={"pin": pin.id, "template": template.id})
    session.flush()
