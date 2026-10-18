"""Runner jobs and human output gates. References are checked again before every consumer activates."""
import json

from sqlalchemy import select

from app.errors import Unprocessable
from app.i18n import _
from app.models import Dataset, DatasetProfile, RunResult
from app.services import datasets, output_gate, runner_jobs, state_machine as sm
from app.services.workflows import publish
from app.services.workflows.placeholders import Placeholder, classify_path, parse_template, render

RANK = {"cloud_ok": 0, "local_only": 1, "sealed": 2}


def invalid(code):
    raise Unprocessable(_("Workflow data reference is not allowed or has not been released"), code=code)


def check_reads(session, run, step):
    """Check actual reads, including config and bindings; filters/defaults cannot bypass classification."""
    from app.services.workflows import engine

    definitions = publish._steps(publish.definition_from_version(run.version, run.template, run.project), [])
    target = next(s for s in definitions if s["key"] == step.step_key)
    rows = engine._latest(session, run)
    by_key = {s.step.step_key: s for s in rows.values()}
    for location, text in publish._texts(target):
        for part in parse_template(text):
            if not isinstance(part, Placeholder):
                continue
            ref = classify_path(part.path)
            if ref.kind not in ("step_builtin", "step_output"):
                continue
            source = by_key.get(ref.step)
            if source is None or source.position >= step.position:
                invalid("placeholder_unresolved")
            if ref.kind == "step_builtin" and ref.builtin in ("task_nr", "status"):
                continue
            if (step.step_type == "gate" and source.position == step.position - 1
                    and ref.kind == "step_builtin" and ref.builtin == "result"):
                continue
            cls = max((source.output_class or "cloud_ok", source.step.data_class or run.project.privacy,
                       source.task.data_class or run.project.privacy if source.task else "cloud_ok"), key=RANK.get)
            if RANK[cls] <= RANK[step.data_class or run.project.privacy]:
                continue
            if cls == "sealed":
                values = json.loads(source.output_json or "{}")
                if ref.kind != "step_builtin" or ref.builtin not in ("result", "profile"):
                    invalid("zone_boundary_without_gate")
                value = values.get(ref.builtin)
                obj = session.get(RunResult if ref.builtin == "result" else DatasetProfile, value) if value else None
                if (obj is None or obj.task_id != source.task_id
                        or (ref.builtin == "result" and obj.status != "released")
                        or (ref.builtin == "profile" and obj.status != "released")):
                    invalid("workflow_data_unreleased")
                if ref.builtin == "result":
                    gate = rows.get(source.position + 1)
                    if (gate is None or gate.step.step_type != "gate" or gate.status != "succeeded"
                            or gate.step.review_mode != "manual"):
                        invalid("zone_boundary_without_gate")
            else:
                approvals = [r for r in rows.values() if source.position < r.position < step.position
                             and r.step.step_type == "approval" and r.step.review_mode == "manual"
                             and r.status == "succeeded"]
                # An approval may inspect the local data before it authorizes the downstream class change.
                if step.step_type == "approval" and step.review_mode == "manual":
                    continue
                if not approvals:
                    invalid("zone_boundary_without_gate")


def runner_fields(session, run, step):
    from app.services.workflows.engine import _config
    from app.services.workflows.rendering import step_context
    from app.services.workflows.contracts import ContractError, resolve_bindings
    from app.services.workflows.placeholders import PlaceholderError

    if not datasets.enabled(session):
        invalid("runner_without_data_zones")
    config = _config(step)
    kind = config.get("task_kind", "run")
    if kind not in ("run", "profile") or (step.data_class or run.project.privacy) != "sealed":
        invalid("workflow_runner_invalid")
    ctx = step_context(session, run)
    fields = config.get("run", {})
    if not isinstance(fields, dict):
        invalid("workflow_runner_invalid")
    try:
        fields = {k: render(v, ctx).text if isinstance(v, str) else v for k, v in fields.items()}
        bindings = resolve_bindings(step.inputs_json, ctx)
    except (PlaceholderError, ContractError) as exc:
        raise Unprocessable(_("Workflow placeholders could not be resolved"), code=exc.code) from exc
    for binding in bindings:
        if binding["purpose"] == "input":
            value = binding["value"]
            ds = session.get(Dataset, int(value)) if str(value).isdigit() else None
            fields["dataset"] = ds.dataset_key if ds and ds.project_id == run.project_id else value
        elif binding["purpose"] == "script":
            fields.update(commit_sha=binding["commit"], script_path=binding["path"])
    if str(fields.get("dataset", "")).isdigit():
        ds = session.get(Dataset, int(fields["dataset"]))
        fields["dataset"] = ds.dataset_key if ds and ds.project_id == run.project_id else None
    return kind, runner_jobs.normalize_run(session, run.project, kind, fields)


def materialize_released(session, run, row, task):
    """Freeze only approved artifacts into the consumer task; keep scoped IDs for oversized profiles."""
    from app.services.workflows import engine

    target = publish._step_dict(row.step)
    target = publish._steps({"steps": [target]}, [])[0]
    sources = {r.step.step_key: r for r in engine._latest(session, run).values() if r.position < row.position}
    refs = {"input_profiles": [], "input_results": []}
    for location, text in publish._texts(target):
        for part in parse_template(text):
            if not isinstance(part, Placeholder):
                continue
            ref = classify_path(part.path)
            source = sources.get(ref.step)
            if source is None or ref.kind != "step_builtin" or ref.builtin not in ("profile", "result"):
                continue
            value = json.loads(source.output_json or "{}").get(ref.builtin)
            obj = session.get(DatasetProfile if ref.builtin == "profile" else RunResult, value) if value else None
            key = "input_profiles" if ref.builtin == "profile" else "input_results"
            if obj is None or obj.status != "released" or value in refs[key]:
                continue
            refs[key].append(value)
            if ref.builtin == "profile":
                content = obj.schema_json + "\n" + obj.sample_csv
                url = f"/api/v1/data/profiles/{obj.id}"
            else:
                content = obj.released_json or "{}"
                url = f"/api/v1/data/results/{obj.id}"
            header = f"\n\n{_('Released workflow data')}: {ref.builtin}#{obj.id}\n"
            description = (task.description or "") + header + content
            task.description = description if len(description.encode("utf-8")) <= 60000 else (
                (task.description or "") + header + url)
    if any(refs.values()):
        row.output_json = json.dumps(refs)


def activate_gate(session, run, row):
    from app.services.workflows import engine

    previous = engine._latest(session, run).get(row.position - 1)
    paused = run.status == "paused"
    if (row.step.review_mode != "manual" or row.step.optional or previous is None
            or previous.step.step_type != "runner" or not previous.task_id or previous.task.task_kind != "run"):
        invalid("workflow_gate_invalid")
    result_id = json.loads(previous.output_json or "{}").get("result")
    result = session.get(RunResult, result_id) if result_id else None
    if result is None or result.task_id != previous.task_id:
        invalid("workflow_gate_invalid")
    row.output_json = json.dumps({"result": result.id})
    row.output_class = "sealed"
    if result.status == "released" and previous.task.status == "done":
        row.status, row.finished_at = "succeeded", sm.utcnow()
    elif result.status in ("rejected", "kept_sealed", "checks_failed", "deleted"):
        engine._fail(session, run, row, "gate_rejected")
    else:
        row.status = "waiting"
        if not paused:
            run.status = "waiting"
        row.wait_started_at = row.wait_started_at or sm.utcnow()
    session.flush()


def task_transition(session, run, row, task, action, payload):
    """A runner finishes before its task is released. The task still belongs to the runner attempt."""
    from app.services.workflows import engine

    if row.step.step_type != "runner":
        return False
    if row.status not in ("active", "waiting", "succeeded"):
        return False
    from app.services.workflows.errors import sync_clock

    sync_clock(row, sm.utcnow())
    if action == "submit" and task.status == "for_review":
        if task.task_kind == "profile":
            profile = session.scalar(select(DatasetProfile).where(DatasetProfile.task_id == task.id)
                                     .order_by(DatasetProfile.id.desc()).limit(1))
            if profile is None:
                invalid("workflow_runner_invalid")
            values = {"profile": profile.id}
        else:
            result = (payload or {}).get("_run_result")
            if result is None or result.task_id != task.id:
                invalid("workflow_runner_invalid")
            if result.status != "pending_approval":
                if result.status == "checks_failed":
                    from app.services.workflows.scripts import request_repair

                    if request_repair(session, run, row, "gate_checks_failed", result):
                        return True
                engine._fail(session, run, row, "gate_checks_failed")
                return True
            values = {"result": result.id}
        row.output_json, row.output_class = json.dumps(values), "sealed"
        if task.task_kind == "profile":
            from app.services.workflows.scripts import profile_can_stay_local

            if profile_can_stay_local(session, run, row):
                row.status, row.finished_at = "succeeded", sm.utcnow()
                session.flush()
                engine.advance(session, run)
                return True
            row.status = "waiting"
            row.wait_started_at = sm.utcnow()
            if run.status != "paused":
                run.status = "waiting"
            session.flush()
            return True
        row.status, row.finished_at = "succeeded", sm.utcnow()
        session.flush()
        following = engine._latest(session, run).get(row.position + 1)
        if task.task_kind == "run" and (following is None or following.step.step_type != "gate"):
            output_gate.keep_sealed_by_system(session, result, "Workflow result stays in zone R")
        engine.advance(session, run)
        return True
    if task.task_kind == "profile" and action == "reject":
        engine._fail(session, run, row, "gate_rejected")
        return True
    if row.status == "succeeded":
        if task.task_kind == "profile":
            engine.advance(session, run)
            return True
        following = engine._latest(session, run).get(row.position + 1)
        if following and following.step.step_type == "gate" and following.status in ("pending", "waiting"):
            # Decisions during a manual pause are recorded; advance will defer downstream activation.
            activate_gate(session, run, following)
            if following.status == "succeeded":
                engine.advance(session, run)
        return True
    if task.status == "aborted" and task.task_kind == "run":
        result = session.scalar(select(RunResult).where(RunResult.task_id == task.id)
                                .order_by(RunResult.id.desc()).limit(1))
        if (result and result.exit_code == 3) or (payload or {}).get("_workflow_failure_code") == "schema_mismatch":
            from app.services.workflows.scripts import request_repair

            if request_repair(session, run, row, "schema_mismatch", result):
                return True
    return False
