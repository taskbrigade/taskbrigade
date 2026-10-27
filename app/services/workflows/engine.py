"""Ordered execution of pinned workflow versions (WF 2).

All mutations lock the run row. Task hooks participate in the caller's transaction, so completing a task and
creating its successor either both commit or both roll back. Unsupported stages and unresolved templates fail
closed. Activation and dry-run share parameter validation and the WF 3 resolver through `rendering`.
"""
import json

from sqlalchemy import func, select

from app.errors import AppError, Conflict, Forbidden, Unprocessable
from app.i18n import _
from app.models import Actor, ProjectMember, Task, WorkflowRun, WorkflowRunStep, WorkflowStep, WorkflowTemplate
from app.services import notifications, state_machine as sm
from app.services.workflows.roles import data_class, resolve

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
COMPLETE = frozenset({"succeeded", "skipped"})
CLASS_RANK = {"cloud_ok": 0, "local_only": 1, "sealed": 2}
FALSY = frozenset({"", "false", "0", "no", "off", "none", "null"})
# Step types that may run in a parallel group; zone boundaries and code steps stay strictly sequential.
PARALLEL_TYPES = frozenset({"agent", "notify"})
# Step types that may carry a `when:` condition; decisions and zone boundaries may not be skipped.
CONDITION_TYPES = frozenset({"agent", "notify", "script", "runner"})


def _lock(session, run):
    session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == run.template_id).with_for_update())
    return session.scalar(select(WorkflowRun).where(WorkflowRun.id == run.id).with_for_update()
                          .execution_options(populate_existing=True))


def lock_for_task(session, task):
    if task.workflow_run_step_id:
        row = session.get(WorkflowRunStep, task.workflow_run_step_id)
        return _lock(session, row.run)


def require_claimable(session, task):
    if task.workflow_run_step_id:
        row = session.get(WorkflowRunStep, task.workflow_run_step_id)
        if row.status not in ("active", "waiting"):
            raise Conflict(_("Workflow step is waiting for recovery"), code="workflow_recovery_pending")


def _require_control(run, actor):
    if not actor.is_active or not (
        (actor.actor_type == "human" and actor.role == "admin") or
        (actor.actor_type == "human" and (actor.id in run.project.owner_ids or actor.id in (run.template.owner_id, run.started_by))) or
        (actor.role == "orchestrator" and actor.id == run.started_by)
    ):
        raise Forbidden(_("You may not control this workflow run"))


def _steps(session, run):
    return list(session.scalars(select(WorkflowStep).where(WorkflowStep.version_id == run.version_id)
                               .order_by(WorkflowStep.position)))


def _groups(steps):
    """First index of each parallel group to its consecutive members (same non-empty `parallel_group`)."""
    groups: dict[int, list] = {}
    i = 0
    while i < len(steps):
        name = steps[i].parallel_group
        if name:
            members = [steps[i]]
            j = i + 1
            while j < len(steps) and steps[j].parallel_group == name:
                members.append(steps[j])
                j += 1
            if len(members) >= 2:
                groups[i] = members
            i = j
        else:
            i += 1
    return groups


def _latest(session, run):
    rows = session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
                           .order_by(WorkflowRunStep.position, WorkflowRunStep.attempt).with_for_update()
                           .execution_options(populate_existing=True))
    return {row.position: row for row in rows}


def _attempt(session, run, step):
    last = session.scalar(select(WorkflowRunStep.attempt).where(
        WorkflowRunStep.run_id == run.id, WorkflowRunStep.step_id == step.id)
        .order_by(WorkflowRunStep.attempt.desc()).limit(1).with_for_update()) or 0
    if last >= 255:
        raise Conflict(_("Workflow step attempt limit reached"), code="workflow_attempt_limit")
    row = WorkflowRunStep(run=run, step=step, position=step.position, attempt=last + 1,
                          status="pending")
    session.add(row)
    session.flush()
    return row


def render_step(session, run, step):
    from app.services.workflows.rendering import render_fields

    return render_fields(session, run, step)


def condition_false(session, run, step):
    """True when the step's `when:` renders to a falsy value (skip). Raises `PlaceholderError` when unresolved."""
    if not step.condition_tpl or not step.condition_tpl.strip():
        return False
    from app.services.workflows.placeholders import render
    from app.services.workflows.rendering import step_context

    text = render(step.condition_tpl, step_context(session, run)).text
    return text.strip().lower() in FALSY


def _config(step):
    try:
        config = json.loads(step.config_json or "{}")
        if not isinstance(config, dict):
            raise ValueError
        return config
    except (ValueError, TypeError) as exc:
        raise Unprocessable(_("Invalid workflow step configuration"), code="workflow_config_invalid") from exc


def _fail(session, run, row, code):
    from app.services.workflows.errors import fail

    fail(session, run, row, code)


def _boundary(session, run, step):
    from app.services.workflows.data_steps import check_reads
    from app.services.workflows.placeholders import PlaceholderError

    try:
        check_reads(session, run, step)
    except PlaceholderError as exc:
        raise Unprocessable(_("Workflow placeholders could not be resolved"), code=exc.code) from exc


def _crosses_boundary(session, run, step):
    earlier_restricted = any(data_class(run, s) != "cloud_ok" for s in _steps(session, run)
                             if s.position < step.position)
    if step.step_type == "approval":
        earlier_restricted |= any(r.output_class in ("local_only", "sealed")
                                  for r in _latest(session, run).values() if r.position < step.position)
    if step.step_type == "approval" and earlier_restricted:
        return any(data_class(run, s) == "cloud_ok" for s in _steps(session, run) if s.position >= step.position)
    return any(source.id == step.id and CLASS_RANK[data_class(run, source)] > CLASS_RANK[data_class(run, target)]
               for source, target in zip(_steps(session, run), _steps(session, run)[1:]))


def check_decision(session, task, actor):
    if task.workflow_run_step_id:
        row = session.get(WorkflowRunStep, task.workflow_run_step_id)
        if row.step.step_type == "script" and (not actor or actor.actor_type != "human"):
            raise Forbidden(_("Only humans may approve workflow scripts"))
    if task.task_kind != "approval" or not task.workflow_run_step_id:
        return
    row = session.get(WorkflowRunStep, task.workflow_run_step_id)
    if _crosses_boundary(session, row.run, row.step) and not (
        actor and actor.actor_type == "human" and (actor.role == "admin" or actor.id in row.run.project.owner_ids)
    ):
        raise Forbidden(_("Only a human project owner or admin may release a zone boundary"))


def _auto_allowed(session, run, step):
    return all(data_class(run, later) in CLASS_RANK and
               CLASS_RANK[data_class(run, later)] >= CLASS_RANK[data_class(run, step)] and
               later.step_type not in ("runner", "script")
               for later in _steps(session, run) if later.position > step.position)


def require_start(session, actor, template):
    """Shared permission/version checks for start and previews."""
    is_member = session.get(ProjectMember, (template.project_id, actor.id)) is not None
    permitted = (actor.actor_type == "human" and (
        actor.role in ("admin", "orchestrator") or actor.id in template.project.owner_ids | {template.owner_id})) or (
        actor.actor_type == "agent" and actor.role == "orchestrator" and is_member)
    if not actor.is_active or not permitted:
        raise Forbidden(_("You may not start this workflow"))
    if template.status == "archived" or template.project.status == "archived":
        raise Conflict(_("Archived workflows or projects cannot start runs"), code="workflow_archived")
    version = template.current_version
    if version is None or version.template_id != template.id or version.state != "published":
        raise Unprocessable(_("Workflow has no published version"), code="workflow_not_published")
    steps = version.steps
    if not steps or [s.position for s in steps] != list(range(1, len(steps) + 1)):
        raise Unprocessable(_("Workflow steps must be consecutive"), code="workflow_steps_invalid")
    return version


def start(session, actor, template, *, params=None, trigger_kind="manual", budget_tokens=None, budget_cents=None,
          schedule=None, scheduled_for=None, now=None, queue=False, force_parallel=False, trigger=None,
          parent_run=None, event_payload=None):
    template = session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template.id).with_for_update()
                              .execution_options(populate_existing=True))
    version = require_start(session, actor, template)
    steps = version.steps
    if (trigger_kind not in ("manual", "api", "schedule", "event", "workflow")
            or (trigger_kind == "schedule" and schedule is None)
            or (trigger_kind in ("event", "workflow") and trigger is None)
            or (trigger_kind == "workflow" and parent_run is None)):
        raise Unprocessable(_("Unsupported workflow trigger"), code="workflow_trigger_invalid")
    active = list(session.scalars(select(WorkflowRun).where(
        WorkflowRun.template_id == template.id, WorkflowRun.status.not_in(TERMINAL)).with_for_update()))
    if force_parallel and (schedule is not None or actor.actor_type != "human" or actor.role != "admin"):
        raise Forbidden(_("Only a human admin may force parallel workflow runs"))
    queued = bool(active) and queue and not any(r.status == "queued" for r in active)
    if active and not queued and not force_parallel:
        raise Conflict(_("A workflow run is already active; queue it or wait for completion"), code="workflow_overlap")
    seq = (session.scalar(select(WorkflowRun.run_seq).where(WorkflowRun.template_id == template.id)
                          .order_by(WorkflowRun.run_seq.desc()).limit(1).with_for_update()) or 0) + 1
    system = trigger_kind in ("schedule", "event", "workflow")
    run = WorkflowRun(template_id=template.id, version_id=version.id, project_id=template.project_id,
                      run_seq=seq, started_by=None if system else actor.id, params_json=json.dumps(params or {}),
                      schedule_id=schedule.id if schedule else None, scheduled_for=scheduled_for,
                      schedule_timezone=schedule.timezone if schedule else None,
                      trigger_id=trigger.id if trigger else None,
                      parent_run_id=parent_run.id if parent_run else None,
                      trigger_kind=trigger_kind, status="queued" if queued else "running", current_position=1,
                      started_at=None if queued else (now or sm.utcnow()),
                      budget_tokens=budget_tokens if budget_tokens is not None else template.default_budget_tokens,
                      budget_cents=budget_cents if budget_cents is not None else template.default_budget_cents)
    from app.services.workflows.rendering import context, resolve_params

    # Relationships are needed before the transient run has been flushed.
    run.template, run.version, run.project = template, version, template.project
    session.add(run)
    session.flush()
    ctx = context(run)
    if schedule or event_payload is not None:
        from app.services.workflows.placeholders import render
        if event_payload is not None:
            ctx.allow_event = True
            ctx.event = event_payload
        params = {key: render(value, ctx).text if isinstance(value, str) else value
                  for key, value in (params or {}).items()}
    run.params_json = json.dumps(resolve_params(version, params, ctx, session=session,
                                               project=run.project, actor=template.owner))
    for step in steps:
        _attempt(session, run, step)
    if force_parallel:
        from app.logging_setup import audit
        audit(actor.name, "workflow_force_parallel", run_id=run.id)
    if not queued:
        advance(session, run)
    return run


def advance(session, run):
    run = _lock(session, run)
    if run.status in TERMINAL or run.status == "paused" or run.status == "queued":
        return run
    steps = _steps(session, run)
    groups = _groups(steps)
    i = 0
    while i < len(steps):
        step = steps[i]
        members = groups.get(i)
        if members:
            states = []
            for member in members:
                row = _latest(session, run).get(member.position)
                if row is None:
                    row = _attempt(session, run, member)
                states.append((member, row))
            if all(row.status in COMPLETE for _, row in states):
                i += len(members)
                continue
            run.current_position = members[0].position
            for member, row in states:
                if row.status in COMPLETE:
                    continue
                if row.status == "failed":
                    return run
                if row.status in ("active", "waiting"):
                    continue
                try:
                    _activate(session, run, row, member)
                except Unprocessable as exc:
                    _fail(session, run, row, exc.code)
                if run.status == "paused":
                    return run
            if all(row.status in COMPLETE for _, row in states):
                i += len(members)
                continue
            run.status = "waiting" if any(r.status == "waiting" for _, r in states) else "running"
            session.flush()
            return run
        row = _latest(session, run).get(step.position)
        if row is None:
            row = _attempt(session, run, step)
        if row.status in COMPLETE:
            i += 1
            continue
        run.current_position = step.position
        if row.status in ("active", "waiting"):
            if step.step_type == "script" and row.task_id is None:
                try:
                    _activate(session, run, row, step)
                except Unprocessable as exc:
                    _fail(session, run, row, exc.code)
                if row.status in COMPLETE:
                    i += 1
                    continue
                return run
            if step.step_type == "gate":
                from app.services.workflows.data_steps import activate_gate

                activate_gate(session, run, row)
                if row.status in COMPLETE:
                    i += 1
                    continue
                if row.status == "failed":
                    return run
            run.status = "waiting" if row.status == "waiting" else "running"
            return run
        if row.status != "pending":
            return run
        try:
            _activate(session, run, row, step)
        except Unprocessable as exc:
            _fail(session, run, row, exc.code)
        if row.status not in COMPLETE:
            return run
        i += 1
    run.status, run.finished_at = "succeeded", sm.utcnow()
    session.flush()
    from app.services.workflows.events import emit_run_end
    emit_run_end(session, run)
    return run


def _activate(session, run, row, step):
    from app.services import tasks

    row.activated_at = sm.utcnow()
    if step.condition_tpl and step.condition_tpl.strip():
        from app.services.workflows.placeholders import PlaceholderError

        try:
            skip = condition_false(session, run, step)
        except PlaceholderError as exc:
            _fail(session, run, row, exc.code)
            return
        if skip:
            row.status, row.finished_at, row.error_code = "skipped", sm.utcnow(), "condition_false"
            row.error_detail = step.condition_tpl.strip()[:500]
            session.flush()
            return
    if step.step_type == "script":
        from app.services.workflows.scripts import prepare

        if prepare(session, run, row):
            session.flush()
            return
    if code := _boundary(session, run, step):
        _fail(session, run, row, code)
        return
    if step.step_type not in ("agent", "script", "approval", "notify", "runner", "gate"):
        _fail(session, run, row, "unsupported_step_type")
        return
    if step.step_type in ("agent", "script"):
        from app.services import usage

        if usage.budget_state(run) == "exceeded":
            _fail(session, run, row, "budget_exceeded")
            return
    fields = render_step(session, run, step)
    if step.step_type == "gate":
        from app.services.workflows.data_steps import activate_gate

        activate_gate(session, run, row)
        return
    if step.step_type == "notify":
        config = _config(step)
        channel = config.get("channel", "notification")
        if channel == "email":
            from app.services.workflows.email import enqueue

            previous = [r for r in _latest(session, run).values() if r.position < row.position and r.task_id]
            enqueue(session, run, row, step, config, previous)
            row.status, row.finished_at, row.output_class = "succeeded", sm.utcnow(), data_class(run, step)
            session.flush()
            return
        if channel not in ("notification", "telegram"):
            raise Unprocessable(_("Invalid workflow step configuration"), code="workflow_config_invalid")
        text = config.get("text", fields["description"] or fields["title"])
        if not isinstance(text, str):
            raise Unprocessable(_("Workflow placeholders could not be resolved"), code="placeholder_unresolved")
        from app.services.workflows.placeholders import PlaceholderError, render
        from app.services.workflows.rendering import step_context

        try:
            text = render(text, step_context(session, run)).text
        except PlaceholderError as exc:
            raise Unprocessable(_("Workflow placeholders could not be resolved"), code=exc.code) from exc
        notifications.workflow_event(session, run, "workflow_notify", text, config.get("to", ["owner"]))
        row.status, row.finished_at, row.output_class = "succeeded", sm.utcnow(), data_class(run, step)
        session.flush()
        return
    cls = data_class(run, step)
    # Data class rules (research/konzept-datenzonen.md §1): a looser step class needs an owner/admin template owner.
    try:
        from app.services import zones

        zones.check_class(run.project, Task(task_kind="work"), cls, run.template.owner)
    except AppError as e:
        _fail(session, run, row, e.code)
        return
    if step.step_type == "approval" and step.review_mode == "auto":
        _fail(session, run, row, "zone_boundary_without_gate")
        return
    if step.step_type == "agent" and step.review_mode == "auto" and not _auto_allowed(session, run, step):
        _fail(session, run, row, "auto_review_forbidden")
        return
    assignee = resolve(session, run, step)
    if assignee is None:
        _fail(session, run, row, "no_assignee")
        return
    if step.step_type == "approval" and _crosses_boundary(session, run, step) and (assignee.actor_type != "human" or (
        assignee.role != "admin" and assignee.id not in run.project.owner_ids
    )):
        _fail(session, run, row, "zone_boundary_without_gate")
        return
    run_fields, kind = {}, "work"
    if step.step_type == "runner":
        from app.services.workflows.data_steps import runner_fields

        kind, run_fields = runner_fields(session, run, step)
    if step.step_type in ("agent", "script", "runner"):
        sm.require_definition(Task(**{k: v for k, v in fields.items() if k != "title"}))
    previous = [r for r in _latest(session, run).values() if r.position < row.position and r.task_id]
    task = Task(task_nr=tasks._next_task_nr(session, run.project_id), project_id=run.project_id,
                **fields, created_by=run.template.owner_id, row_version=1,
                status="for_review" if step.step_type == "approval" else "defined",
                task_kind="approval" if step.step_type == "approval" else kind,
                assigned_to=assignee.id if step.step_type in ("agent", "script", "runner") else None,
                reviewer_id=(assignee.id if step.step_type == "approval" else (
                    step.reviewer_id or run.project.default_reviewer_id)), model_tier=step.model_tier,
                workflow_run_step_id=row.id, follow_up_of_id=previous[-1].task_id if previous else None,
                **run_fields)
    task.data_class = cls
    if step.step_type == "agent":
        from app.services.workflows.data_steps import materialize_released

        materialize_released(session, run, row, task)
    if step.step_type in ("agent", "script"):
        from app.services import reviews, zones

        reviewer = session.get(Actor, task.reviewer_id) if task.reviewer_id else None
        if step.review_mode == "auto":
            task.reviewer_id = None
        elif reviewer and (not reviews.eligible(reviewer) or reviewer.id == assignee.id or
                           not zones.may_handle(run.project, reviewer, task, session=session) or
                           (step.review_mode == "manual" and reviewer.actor_type != "human")):
            raise Unprocessable(_("Invalid workflow reviewer"), code="invalid_reviewer")
        elif step.review_mode == "agent" and (reviewer is None or reviewer.actor_type != "agent"):
            raise Unprocessable(_("Invalid workflow reviewer"), code="invalid_reviewer")
        if task.reviewer_id:
            from app.services import access

            if not access.may_use_agent(run.template.owner, reviewer):
                raise Unprocessable(_("Invalid workflow reviewer"), code="invalid_reviewer")
            reviews._ensure_access(session, run.project, reviewer, task)
    if not task.title or not task.title.strip():
        raise Unprocessable(_("Title is required"))
    session.add(task)
    session.flush()
    row.task = task
    from app.services import data_refs

    data_refs.sync_run_input(session, task, run.template.owner)
    if step.step_type == "script":
        from app.services.workflows.scripts import describe_task

        describe_task(session, run, row, task)
    row.assignee_id = assignee.id
    row.status = "waiting" if step.step_type == "approval" else "active"
    run.status = "waiting" if row.status == "waiting" else "running"
    from app.services.workflows.errors import sync_clock

    sync_clock(row, sm.utcnow())
    sm.add_event(session, task, None, "created", None, task.status, "workflow")
    if step.step_type == "approval":
        notifications.task_event(session, task, "review")
    session.flush()


def _internal_status(session, task, status, note, by=None):
    """Audited engine-only transitions: rework may reopen a merged task; run cancel needs no task manager."""
    sm.workflow_status(session, task, status, note, by)


def _reject(session, run, row, task, actor, body):
    step = row.step
    config = _config(step)
    row.status, row.error_code, row.finished_at = "failed", "approval_rejected", sm.utcnow()
    _internal_status(session, task, "cancelled", _("Workflow approval rejected"), actor)
    target = config.get("on_reject")
    earlier = [s for s in _steps(session, run) if s.position < row.position and s.step_type == "agent"]
    if target is None:
        target = f"rework:{earlier[-1].step_key}" if earlier else "fail"
    limit = config.get("max_rework", 3)
    rejected = session.scalar(select(func.count()).select_from(WorkflowRunStep).where(
        WorkflowRunStep.run_id == run.id, WorkflowRunStep.step_id == row.step_id,
        WorkflowRunStep.error_code == "approval_rejected"))
    match = next((s for s in earlier if target == f"rework:{s.step_key}"), None)
    if match is None or not isinstance(limit, int) or limit < 0 or rejected > limit:
        _fail(session, run, row, "approval_rejected")
        return
    rows = _latest(session, run)
    origin = rows[match.position]
    if not origin.task_id:
        _fail(session, run, row, "approval_rejected")
        return
    _internal_status(session, origin.task, "changes_requested", body or _("Workflow approval rejected"), actor)
    origin.status, origin.finished_at, origin.output_json, origin.output_class = "active", None, None, None
    origin.work_us, origin.work_started_at, origin.deadline_at = 0, None, None
    origin.task.done_at = None
    for definition in _steps(session, run):
        if definition.position > match.position:
            old = rows.get(definition.position)
            if old and old.status == "pending":
                old.status, old.finished_at = "cancelled", sm.utcnow()
            _attempt(session, run, definition)
    run.current_position = match.position
    if run.status != "paused":
        run.status = "running"


def on_task_transition(session, task, action, actor=None, payload=None):
    run = lock_for_task(session, task)
    if run is None or run.status in TERMINAL:
        return
    row = session.get(WorkflowRunStep, task.workflow_run_step_id)
    if row.id != _latest(session, run)[row.position].id:
        return  # Stale decisions on an old attempt never advance the run.
    if row.status == "failed" and row.next_retry_at and task.status == "cancelled":
        _fail(session, run, row, "task_cancelled")
        return
    from app.services.workflows.data_steps import task_transition

    if task_transition(session, run, row, task, action, payload):
        return
    if row.status not in ("active", "waiting"):
        return
    from app.services.workflows.errors import sync_clock

    sync_clock(row, sm.utcnow())
    if row.step.step_type in ("agent", "script"):
        row.assignee_id = task.assigned_to
    if action == "submit" and row.step.step_type == "agent" and row.step.review_mode == "auto":
        if not _auto_allowed(session, run, row.step):
            run.status, run.pause_reason = "paused", "auto_review_forbidden"
            notifications.workflow_event(session, run, "workflow_paused")
            return
        try:
            with session.begin_nested():
                # Reuse the exact merge/brain/changelog validation of a human approve. The owner supplies
                # authorship for repository follow-up commits; the status event records the system decision.
                sm._approve(session, task, run.template.owner, {})
        except AppError:
            run.status, run.pause_reason = "paused", "auto_review_failed"
            notifications.workflow_event(session, run, "workflow_paused")
            return
        task.status, task.row_version = "done", task.row_version + 1
        sm.add_event(session, task, None, "status_change", "for_review", "done", "auto_approved")
        sm.add_comment(session, task, run.template.owner, "system", _("Workflow task automatically approved"))
    if row.step.step_type == "approval" and action in ("reject", "reject_conflict"):
        _reject(session, run, row, task, actor, (payload or {}).get("body"))
    elif task.status == "done":
        if row.step.step_type == "script":
            from app.services.workflows.scripts import pin_merged

            if not pin_merged(session, run, row, task, actor):
                return
        row.status, row.finished_at, row.output_class = "succeeded", sm.utcnow(), data_class(run, row.step)
        advance(session, run)
    elif task.status in ("cancelled", "aborted"):
        _fail(session, run, row, "task_cancelled" if task.status == "cancelled" else "task_aborted")
    elif task.status == "questions" or (task.status == "for_review" and row.step.review_mode == "manual"):
        row.status = "waiting"
        if run.status != "paused":
            run.status = "waiting"
    elif row.step.step_type != "approval":
        row.status = "active"
        if run.status != "paused":
            run.status = "running"
    session.flush()


def pause(session, actor, run):
    run = _lock(session, run)
    _require_control(run, actor)
    if run.status in TERMINAL:
        raise Conflict(_("Workflow run has already finished"), code="workflow_finished")
    run.status, run.pause_reason = "paused", "manual"
    session.flush()
    return run


def resume(session, actor, run, *, from_step=None, skip=False, budget_tokens=None, budget_cents=None):
    from app.services.workflows.errors import stop_clock

    run = _lock(session, run)
    _require_control(run, actor)
    if run.status != "paused":
        raise Conflict(_("Only paused workflow runs can resume"), code="workflow_not_paused")
    if run.pause_reason == "budget_exceeded":
        new_tokens = budget_tokens if budget_tokens is not None else run.budget_tokens
        new_cents = budget_cents if budget_cents is not None else run.budget_cents
        still_exceeded = (new_tokens is not None and run.used_tokens >= new_tokens) or \
                         (new_cents is not None and run.used_cents >= new_cents)
        if still_exceeded:
            raise Conflict(_("Resuming a run that exceeded its budget requires a higher budget"),
                           code="workflow_budget_too_low")
    if budget_tokens is not None:
        run.budget_tokens = budget_tokens
    if budget_cents is not None:
        run.budget_cents = budget_cents
    rows = _latest(session, run)
    current = rows[run.current_position]
    target = next((s for s in _steps(session, run) if s.step_key == from_step), None) if from_step else current.step
    if target is None or target.position > current.position:
        raise Unprocessable(_("Resume requires the current or an earlier step"), code="workflow_resume_invalid")
    if skip and (not target.optional or target.step_type in ("approval", "gate")):
        raise Unprocessable(_("This workflow step cannot be skipped"), code="workflow_skip_forbidden")
    if from_step or current.status == "failed":
        # A failed retry can have no task (no eligible candidate), while an earlier attempt still
        # owns an aborted/released task. Cancel all open tasks in the restarted range, not only latest.
        for previous in session.scalars(select(WorkflowRunStep).where(
                WorkflowRunStep.run_id == run.id, WorkflowRunStep.position >= target.position)):
            previous.next_retry_at = None
            if previous.task_id and previous.task.status not in ("done", "cancelled"):
                _internal_status(session, previous.task, "cancelled", _("Workflow step restarted"), actor)
        for step in _steps(session, run):
            if step.position >= target.position:
                old = rows.get(step.position)
                if old and old.status in ("pending", "active", "waiting"):
                    stop_clock(old, sm.utcnow())
                    old.status, old.finished_at = "cancelled", sm.utcnow()
                new = _attempt(session, run, step)
                if skip and step.id == target.id:
                    new.status, new.finished_at = "skipped", sm.utcnow()
    elif skip:
        if current.task_id and current.task.status not in ("done", "cancelled"):
            _internal_status(session, current.task, "cancelled", _("Workflow step skipped"), actor)
        stop_clock(current, sm.utcnow())
        current.status, current.finished_at = "skipped", sm.utcnow()
    run.status, run.pause_reason, run.error_detail = "running", None, None
    advance(session, run)
    session.flush()
    return run


def cancel(session, actor, run):
    from app.services.workflows.errors import stop_clock

    run = _lock(session, run)
    _require_control(run, actor)
    if run.status in TERMINAL:
        return run
    run.status, run.finished_at = "cancelled", sm.utcnow()
    for row in session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)):
        row.next_retry_at = None
        stop_clock(row, sm.utcnow())
        if row.task_id and row.task.status not in ("done", "cancelled"):
            _internal_status(session, row.task, "cancelled", _("Workflow run cancelled"), actor)
        if row.status in ("pending", "active", "waiting"):
            row.status, row.finished_at = "cancelled", sm.utcnow()
    session.flush()
    return run
