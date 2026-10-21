"""Tasks: creation, lookup, editing, claiming with leases, and lease sweeping."""
import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, exists, func, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, aliased

from app import i18n
from app.i18n import N_, _, ngettext
from app.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.logging_setup import audit
from app.models import Actor, Project, Sprint, Task, TaskComment, TaskDependency, TaskEvent, WorkflowRunStep
from app.services import access, attachments, members, model_choice, projects, reviews, settings_store, sprints, zones
from app.services.gitea import GiteaError
from app.services import result_mail, state_machine
from app.services.state_machine import add_comment, add_event, transition, utcnow

log = logging.getLogger(__name__)

OPEN_STATUSES = ("draft", "defined", "in_progress", "questions", "aborted", "for_review", "changes_requested")
CLAIMABLE = ("defined", "changes_requested")
LOCK_WAIT_TIMEOUT_S = 10


def get(session: Session, task_nr: str, for_update: bool = False) -> Task:
    stmt = select(Task).where(Task.task_nr == task_nr.upper())
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    task = session.scalar(stmt)
    if task is None:
        raise NotFound(_("Task '%(task_nr)s' not found", task_nr=task_nr))
    return task


def _actor_by_name(session: Session, name: str) -> Actor:
    actor = session.scalar(select(Actor).where(Actor.name == name))
    if actor is None:
        raise Unprocessable(_("Actor '%(name)s' does not exist", name=name))
    return actor


def _check_assignment(session: Session, project: Project, assignee: Actor, task: Task | None = None) -> None:
    if not assignee.is_active:
        raise Unprocessable(_("Actor '%(name)s' is inactive", name=assignee.name))
    members.check_privacy(project, assignee, task)
    zones.check_kind(assignee, task.task_kind if task is not None else None)


def _assign(session: Session, task: Task, project: Project, assignee: Actor | None, by: Actor) -> None:
    old = task.assigned_to
    if assignee is not None:
        if assignee.id != old:  # A task keeps an assignee its editor cannot choose (other fields stay editable).
            access.require_may_use_agent(by, assignee)
        _check_assignment(session, project, assignee, task)
        if assignee.actor_type == "agent":
            members.ensure_member(session, project, assignee, task)
    task.assigned_to = assignee.id if assignee else None
    if old != task.assigned_to:
        old_name = session.get(Actor, old).name if old else None
        add_event(session, task, by, "assigned", old_name, assignee.name if assignee else None)


def _next_task_nr(session: Session, project_id: int) -> str:
    """Allocate a sequence number under the project row lock (spec section 7)."""
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update()
                             .execution_options(populate_existing=True))
    seq = project.next_task_seq
    project.next_task_seq = seq + 1
    return f"{project.project_key}-{seq:04d}"


def create(session: Session, actor: Actor, *, project: str, title: str, description: str | None = None,
           acceptance_criteria: str | None = None, prio: int = 3, assigned_to: str | None = None,
           parent_task_nr: str | None = None, depends_on: list[str] | None = None,
           detail_task_git_url: str | None = None, due_date: date | None = None,
           create_spec_file: bool = False, status: str = "defined", sprint: int | None = None,
           follow_up_of: str | None = None, model_tier: str | None = None, model: str | None = None,
           reviewer: str | None = None, data_class: str | None = None, task_kind: str = "work",
           run: dict | None = None) -> Task:
    """Create a task. In a planned sprint it always starts as a draft, whatever `status` asks for.
    `follow_up_of` links the new task to its origin task in the same project (both detail views show the link).
    `model_tier`/`model`: optional model choice; a concrete model must be on the assignee's allow list.
    `reviewer`: human or reviewer agent; None = the project's default reviewer, `none`/'' = nobody (owner/admins).
    `data_class`: the task's own data class (`zones`, rules 1–3); None/`inherit` = the project's privacy.
    `task_kind` `run`/`profile` (runner tasks, always `sealed`) take `run`: `commit_sha`, `script_path`, `dataset`
    (`runner_jobs.normalize_run`)."""
    from app.services import runner_jobs
    if status not in ("draft", "defined"):
        raise Unprocessable(_("New tasks must start as 'draft' or 'defined'"))
    proj = projects.get(session, project)
    title = (title or "").strip()
    if not title:
        raise Unprocessable(_("Title is required"))
    if not 1 <= prio <= 5:
        raise Unprocessable(_( 'prio must be between 1 (highest) and 5 (lowest)' ))
    assignee = _actor_by_name(session, assigned_to) if assigned_to else None
    if actor.role in ("worker", "reviewer") and assignee is not None and assignee.id != actor.id:
        raise Forbidden(_( 'Workers can only create unassigned tasks or tasks assigned to themselves' ))
    access.require_may_use_agent(actor, assignee)
    if actor.role == "member" and not access.owns(proj, actor):
        raise Forbidden(_("Project %(project)s: only the owner and admins can create tasks", project=proj.project_key))
    if proj.status == "archived":
        raise Unprocessable(_( 'Project is archived' ))
    parent = get(session, parent_task_nr) if parent_task_nr else None
    if parent is not None and parent.project_id != proj.id:
        raise Unprocessable(_( 'Parent task must be in the same project' ))
    origin = get(session, follow_up_of) if follow_up_of else None
    if origin is not None and origin.project_id != proj.id:
        raise Unprocessable(_("A follow-up task must be in the same project as %(task_nr)s", task_nr=origin.task_nr))
    deps = [get(session, nr) for nr in dict.fromkeys(depends_on or [])]
    model_tier, model = model_choice.normalize_tier(model_tier), model_choice.normalize_model(model)
    model_choice.check_allowed(assignee, model)
    task_kind = runner_jobs.normalize_kind(task_kind)
    run_fields = runner_jobs.normalize_run(session, proj, task_kind, run, draft=status == "draft")
    data_class = zones.normalize_class(data_class)
    if task_kind in zones.RUNNER_TASK_KINDS and data_class is None:
        data_class = "sealed"  # Runner tasks are always sealed (rule 3); no need to say it each time.
    probe = Task(task_nr=None, data_class=data_class, status=status,
                 task_kind=task_kind)  # Class rules before anything is written.
    zones.check_class(proj, probe, data_class, actor)
    reviewer_actor = reviews.find(session, reviewer) if reviewer is not None else None
    if reviewer_actor is not None:  # Checked before anything is written.
        access.require_may_use_agent(actor, reviewer_actor)
        reviews.check(proj, reviewer_actor, assignee.id if assignee else None, probe)
    target = sprints.target_for(session, proj, sprint) if sprint is not None else None
    if target is not None:
        sprints.require_plan(proj, actor)
        if target.status == "planned":
            status = "draft"

    if status == "defined":  # Direct creation as defined has the same requirements as publishing.
        state_machine.require_definition(Task(task_nr=None, description=description,
                                               acceptance_criteria=acceptance_criteria, task_kind=task_kind,
                                               **run_fields))
    task = Task(task_nr=_next_task_nr(session, proj.id), project_id=proj.id, title=title[:200],
                description=description or None, acceptance_criteria=acceptance_criteria or None, prio=prio,
                created_by=actor.id, parent_task_id=parent.id if parent else None,
                follow_up_of_id=origin.id if origin else None,
                detail_task_git_url=detail_task_git_url or None, due_date=due_date, status=status,
                model_tier=model_tier, model=model, data_class=data_class, task_kind=task_kind, row_version=1,
                **run_fields)
    session.add(task)
    session.flush()
    add_event(session, task, actor, "created", None, status)
    if data_class is not None:
        _class_event(session, task, proj, actor, None, data_class)
    if target is not None:
        sprints.assign(session, task, target, actor)
    if assignee is not None:
        _assign(session, task, proj, assignee, actor)
    if reviewer is None:
        reviews.apply_default(session, task, proj, actor)
    elif reviewer_actor is not None:
        reviews.set_reviewer(session, task, proj, reviewer_actor, actor)
    for dep in deps:
        session.add(TaskDependency(task_id=task.id, depends_on_task_id=dep.id))
    if task.run_dataset_id:
        from app.services import data_refs

        data_refs.sync_run_input(session, task, actor)  # The input dataset as a `task_data_refs` row (DZ 9)
    if create_spec_file:
        _create_spec_file(session, proj, task)
    session.flush()
    return task


def follow_up_draft(task: Task) -> dict:
    """Prefilled form values for a follow-up of `task`; the open points are offered separately."""
    return {"title": _("Follow-up to %(task_nr)s: %(title)s", task_nr=task.task_nr, title=task.title)[:200],
            "description": _("Follow-up to %(task_nr)s (%(title)s).", task_nr=task.task_nr, title=task.title),
            "acceptance_criteria": "", "points": open_points(task)}


def with_points(origin: Task, text: str | None, points: list[str], checklist: bool = False) -> str:
    """Append the chosen open points of `origin` to a description (list) or acceptance criteria (checklist)."""
    text = (text or "").strip()
    if not points:
        return text
    if checklist:
        block = "\n".join(f"- [ ] {p}" for p in points)
    else:
        block = (_("Open points from %(task_nr)s:", task_nr=origin.task_nr) + "\n\n"
                 + "\n".join(f"- {p}" for p in points))
    return f"{text}\n\n{block}" if text else block


def create_follow_up(session: Session, actor: Actor, origin: Task, *, title: str, description: str | None = None,
                     acceptance_criteria: str | None = None, points: list[str] | None = None,
                     points_into: str = "description", **kw) -> Task:
    """Create a follow-up of `origin` in the same project (default: draft); chosen open points go into the
    description or, with `points_into="acceptance_criteria"`, into the acceptance criteria as a checklist."""
    points = [p for p in (points or []) if p.strip()]
    if points_into == "acceptance_criteria":
        acceptance_criteria = with_points(origin, acceptance_criteria, points, checklist=True)
    else:
        description = with_points(origin, description, points)
    kw.setdefault("status", "draft")
    project = session.get(Project, origin.project_id).project_key
    return create(session, actor, project=project, title=title, description=description or None,
                  acceptance_criteria=acceptance_criteria or None, follow_up_of=origin.task_nr, **kw)


DUPLICATE_PREFIX = N_("(duplicate)")


def can_duplicate(project: Project | None, actor: Actor | None) -> bool:
    """Duplicate a task: whoever may plan the project's tasks (admins, orchestrators, the project owner)."""
    return project is not None and sprints.can_plan(project, actor)


def _duplicate_prefixes() -> list[str]:
    """The prefix in every UI language, so a duplicate of a duplicate never gets a second one."""
    return list(dict.fromkeys([DUPLICATE_PREFIX] + [i18n.catalog(lang).get(DUPLICATE_PREFIX) or DUPLICATE_PREFIX
                                                    for lang in i18n.LANGUAGES]))


def duplicate_title(title: str) -> str:
    """`(duplicate) <title>` in the current language; an existing prefix (any language) is replaced, not stacked."""
    base = title.strip()
    stripped = True
    while stripped:
        stripped = False
        for prefix in _duplicate_prefixes():
            if base.lower().startswith(prefix.lower()):
                base, stripped = base[len(prefix):].strip(), True
    return f"{_(DUPLICATE_PREFIX)} {base}"[:200]


def duplicate(session: Session, actor: Actor, origin: Task, *, assign: str | None = None,
              title: str | None = None) -> Task:
    """Copy `origin` into a new draft of the same project: description, criteria, prio, due date, model tier/model,
    dependencies, parent and sprint (unless closed). Thread, review, branch/PR, lease, history, result and uploads
    stay behind; unassigned unless `assign` names an actor. The new task links back via `duplicate_of_id`, the
    original gets a system comment."""
    project = session.get(Project, origin.project_id)
    if not can_duplicate(project, actor):
        raise Forbidden(_("Project %(project)s: only admins, orchestrators and the owner can duplicate tasks",
                          project=project.project_key))
    sprint = session.get(Sprint, origin.sprint_id) if origin.sprint_id else None
    deps = list(session.scalars(select(Task.task_nr).join(TaskDependency, TaskDependency.depends_on_task_id == Task.id)
                                .where(TaskDependency.task_id == origin.id).order_by(Task.id)))
    parent = session.get(Task, origin.parent_task_id) if origin.parent_task_id else None
    task = create(session, actor, project=project.project_key,
                  title=title.strip() if title and title.strip() else duplicate_title(origin.title),
                  description=origin.description, acceptance_criteria=origin.acceptance_criteria, prio=origin.prio,
                  assigned_to=assign or None, parent_task_nr=parent.task_nr if parent else None, depends_on=deps,
                  due_date=origin.due_date, status="draft",
                  sprint=sprint.id if sprint is not None and sprint.status != "closed" else None,
                  model_tier=origin.model_tier, reviewer=_duplicate_reviewer(session, actor, origin, assign),
                  data_class=_duplicate_class(project, actor, origin))
    task.duplicate_of_id = origin.id
    if origin.model:  # Without an assignee the model is kept; a later assignment drops it if not allowed.
        assignee = session.get(Actor, task.assigned_to) if task.assigned_to else None
        if assignee is None or origin.model in model_choice.allowed(assignee):
            task.model = origin.model
        else:
            add_event(session, task, actor, "model_dropped", origin.model, None,
                      _("not allowed for %(agent)s", agent=assignee.name))
    add_event(session, task, actor, "duplicated_from", None, origin.task_nr)
    add_comment(session, origin, actor, "system", _("Duplicated as %(task_nr)s", task_nr=task.task_nr))
    session.flush()
    return task


def _duplicate_class(project: Project, actor: Actor, origin: Task) -> str | None:
    """The original's own class; a class looser than the project's only if the actor may set it (rule 2), otherwise
    the copy inherits the project's stricter class."""
    cls = origin.data_class
    if cls and zones.CLASS_RANK[cls] < zones.CLASS_RANK[project.privacy] and not zones.may_loosen(project, actor):
        return None
    return cls


def _duplicate_reviewer(session: Session, actor: Actor, origin: Task, assign: str | None) -> str | None:
    """The original's reviewer for the copy, unless it would review its own task or `actor` may not choose that
    agent (then the project default)."""
    reviewer = session.get(Actor, origin.reviewer_id) if origin.reviewer_id else None
    if reviewer is None or not reviews.eligible(reviewer) or (assign and reviewer.name == assign.strip()) \
            or not access.may_use_agent(actor, reviewer):
        return None
    return reviewer.name


def _create_spec_file(session: Session, project: Project, task: Task) -> None:
    if not project.gitea_repo:
        raise Unprocessable(_( 'Spec file requires a project with a Gitea repository' ))
    path = f"tasks/{task.task_nr}.md"
    try:
        settings_store.gitea_client(session).create_files(
            project.gitea_owner, project.gitea_repo, {path: projects.render_repo_file("task_spec.md", task=task)},
            _("[%(task_nr)s] Spec created", task_nr=task.task_nr), project.default_branch)
    except GiteaError as e:
        raise Unprocessable(_("Could not create spec file: %(error)s", error=e), code="gitea_error") from e
    task.detail_task_git_url = f"{project.gitea_url}/src/branch/{project.default_branch}/{path}"


# --- Read --------------------------------------------------------------------------------

def _names(session: Session, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict(session.execute(select(Actor.id, Actor.name).where(Actor.id.in_(ids))).all())


def summary(task: Task, project_key: str, names: dict[int, str], workflow: dict | None = None) -> dict:
    """`workflow`: card badge data of a workflow task (`workflows.ui.badges`), else None."""
    return {"task_nr": task.task_nr, "project": project_key, "title": task.title, "status": task.status,
            "prio": task.prio, "assigned_to": names.get(task.assigned_to), "reviewer": names.get(task.reviewer_id),
            "review_claimed": task.review_claimed_at is not None, "due_date": task.due_date,
            "branch_name": task.branch_name, "pr_url": task.pr_url, "created_at": task.created_at,
            "updated_at": task.updated_at, "done_at": task.done_at, "sprint": task.sprint_id,
            "model_tier": task.model_tier, "model": task.model, "row_version": task.row_version,
            "waiting": waiting_info(task), "task_kind": task.task_kind, "workflow": workflow,
            # `data_class`: the task's own class (None = inherited); `data_class_effective`: the class that applies.
            "data_class": task.data_class, "data_class_effective": zones.effective_class(task.project, task)}


def waiting_info(task: Task) -> dict | None:
    """Conscious waiting (reason, until, watched file) while it is valid, otherwise None."""
    if task.status != "in_progress" or not task.is_waiting(utcnow()):
        return None
    return {"reason": task.waiting_reason, "until": task.waiting_until, "watch_file": task.waiting_file}


def open_points(task: Task) -> list[str]:
    """Open points stored by submit (JSON list); unreadable values count as none."""
    try:
        points = json.loads(task.open_points) if task.open_points else []
    except ValueError:
        return []
    return [str(p) for p in points if str(p).strip()] if isinstance(points, list) else []


def required_outputs(session: Session, task: Task) -> list[dict] | None:
    """Declared outputs of a workflow step, for the TASK.md section. None when this task is not one."""
    if not task.workflow_run_step_id:
        return None
    from app.models import WorkflowRunStep
    from app.services.workflows.contracts import output_contract_view

    run_step = task.workflow_run_step or session.get(WorkflowRunStep, task.workflow_run_step_id)
    if run_step is None or run_step.step is None or not run_step.step.outputs_json:
        return None
    rows = output_contract_view(run_step.step.outputs_json)
    return rows or None


def workflow_outputs(session: Session, task: Task) -> dict | None:
    """Snapshot stored on the run step after submit (`output_json`), if this task has one."""
    if not task.workflow_run_step_id:
        return None
    from app.models import WorkflowRunStep

    run_step = task.workflow_run_step or session.get(WorkflowRunStep, task.workflow_run_step_id)
    if run_step is None or not run_step.output_json:
        return None
    try:
        data = json.loads(run_step.output_json)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def payload(session: Session, task: Task) -> dict:
    """Full task object for GET /tasks/{nr} and claim responses."""
    from app.services import data_refs, runner_jobs

    project = session.get(Project, task.project_id)
    comments = list(session.scalars(select(TaskComment).where(TaskComment.task_id == task.id)
                                    .order_by(TaskComment.created_at, TaskComment.id)))
    events = list(session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)
                                  .order_by(TaskEvent.created_at, TaskEvent.id)))
    dep_task = aliased(Task)
    deps = session.execute(select(dep_task.task_nr, dep_task.status, dep_task.title)
                           .join(TaskDependency, TaskDependency.depends_on_task_id == dep_task.id)
                           .where(TaskDependency.task_id == task.id)).all()
    subtasks = session.execute(select(Task.task_nr, Task.status, Task.title)
                               .where(Task.parent_task_id == task.id).order_by(Task.id)).all()
    parent = session.get(Task, task.parent_task_id) if task.parent_task_id else None
    origin = session.get(Task, task.follow_up_of_id) if task.follow_up_of_id else None
    follow_ups = session.execute(select(Task.task_nr, Task.status, Task.title)
                                 .where(Task.follow_up_of_id == task.id).order_by(Task.id)).all()
    duplicate_of = session.get(Task, task.duplicate_of_id) if task.duplicate_of_id else None
    duplicates = session.execute(select(Task.task_nr, Task.status, Task.title)
                                 .where(Task.duplicate_of_id == task.id).order_by(Task.id)).all()
    sprint = session.get(Sprint, task.sprint_id) if task.sprint_id else None
    names = _names(session, {task.assigned_to, task.created_by, task.reviewer_id} | {c.author_id for c in comments}
                   | {e.actor_id for e in events})
    reviewer = session.get(Actor, task.reviewer_id) if task.reviewer_id else None
    agents = set(session.scalars(select(Actor.name).where(Actor.id.in_(list(
        {c.author_id for c in comments} | {e.actor_id for e in events if e.actor_id})), Actor.actor_type == "agent")))
    return {
        **summary(task, project.project_key, names, workflow_badges(session, [task]).get(task.id)),
        "description": task.description, "acceptance_criteria": task.acceptance_criteria,
        "created_by": names.get(task.created_by), "detail_task_git_url": task.detail_task_git_url,
        "pr_number": task.pr_number, "result_commit_sha": task.result_commit_sha,
        "merge_commit_sha": task.merge_commit_sha,
        "result_summary": task.result_summary, "open_points": open_points(task),
        "required_outputs": required_outputs(session, task),
        "workflow_outputs": workflow_outputs(session, task), "claimed_at": task.claimed_at,
        "lease_expires_at": task.lease_expires_at, "submitted_at": task.submitted_at, "done_at": task.done_at,
        # Reviewer: `reviewer_kind` human/agent; `reviewer_effective` false = it cannot review this task now (the
        # owner and admins do).
        "reviewer_kind": reviewer.actor_type if reviewer else None,
        "reviewer_effective": reviews.effective(session, task) is not None,
        "review_claimed_at": task.review_claimed_at, "review_lease_expires_at": task.review_lease_expires_at,
        "parent_task_nr": parent.task_nr if parent else None,
        "follow_up_of": origin.task_nr if origin else None,
        "follow_ups": [{"task_nr": n, "status": s, "title": t} for n, s, t in follow_ups],
        "duplicate_of": duplicate_of.task_nr if duplicate_of else None,
        "duplicates": [{"task_nr": n, "status": s, "title": t} for n, s, t in duplicates],
        "sprint_name": sprint.name if sprint else None, "sprint_status": sprint.status if sprint else None,
        "sprint_goal": sprint.goal if sprint else None,
        # Model the assignee's runner passes to the agent CLI (`applies` false: the agent chooses itself).
        "model_effective": model_choice.resolve(task, session.get(Actor, task.assigned_to) if task.assigned_to
                                                else None),
        "project_info": projects.info(session, project),
        "attachments": attachments.listing(session, task, project),
        # Allowed recipients of result mails (TASK.md section "Sending results by email" when > 0).
        "mail_recipients": result_mail.recipient_count(session, project),
        # Runner tasks: script commit/path and the input dataset's key; work tasks: TASK.md shows the sandbox
        # script contract when their code may run on data (`runner_jobs.contract_applies`).
        "run": runner_jobs.run_view(session, task),
        # Data references (`task_data_refs`, DZ 9): type, id, label and status only; released content via
        # `GET /tasks/{nr}/artifacts` (TASK.md section "Approved data artifacts").
        "data_refs": data_refs.listing(session, task),
        "script_contract": runner_jobs.contract_applies(session, task),
        "dependencies": [{"task_nr": n, "status": s, "title": t,
                          "resolved": s in state_machine.RESOLVED_DEPENDENCY_STATUSES} for n, s, t in deps],
        "subtasks": [{"task_nr": n, "status": s, "title": t} for n, s, t in subtasks],
        "comments": [{"id": c.id, "type": c.comment_type, "author": names.get(c.author_id), "body": c.body,
                      "created_at": c.created_at, "by_agent": names.get(c.author_id) in agents} for c in comments],
        "events": [{"type": e.event_type, "actor": names.get(e.actor_id), "from": e.from_value,
                    "to": e.to_value, "note": e.note, "created_at": e.created_at} for e in events],
    }


SORTABLE = {"task_nr": Task.task_nr, "title": Task.title, "status": Task.status, "prio": Task.prio,
            "created_at": Task.created_at, "updated_at": Task.updated_at,
            "done_at": func.coalesce(Task.done_at, Task.updated_at)}


def search(session: Session, me: Actor, *, project: str | None = None, assigned_to: str | None = None,
           status: list[str] | None = None, prio: list[int] | None = None, q: str | None = None,
           parent: str | None = None, limit: int = 50, offset: int = 0, sort: str = "prio",
           desc: bool = False, owner: str | None = None, sprint: str | None = None,
           done_since: datetime | None = None, reviewer: str | None = None,
           workflow: str | None = None) -> tuple[list[dict], int]:
    """`done_since` (naive UTC) keeps tasks finished at or after that time (`done_at`, fallback `updated_at`).
    `reviewer`: `me`, `none`, `agents` (any reviewer agent) or an actor name. `workflow`: see `_workflow_filter`."""
    stmt = _search_statement(session, me, project=project, assigned_to=assigned_to, status=status, prio=prio,
                             q=q, parent=parent, owner=owner, sprint=sprint, done_since=done_since,
                             reviewer=reviewer, workflow=workflow)
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    col = SORTABLE.get(sort, Task.prio)
    order = [col.desc() if desc else col.asc()]
    if col is not Task.created_at:
        order.append(Task.created_at.asc())
    rows = session.execute(stmt.order_by(*order).limit(min(max(limit, 1), 500)).offset(max(offset, 0))).all()
    names = _names(session, {t.assigned_to for t, _ in rows} | {t.reviewer_id for t, _ in rows})
    badges = workflow_badges(session, [t for t, _ in rows])
    return [summary(t, key, names, badges.get(t.id)) for t, key in rows], total


def _search_statement(session, me, *, project=None, assigned_to=None, status=None, prio=None, q=None,
                      parent=None, owner=None, sprint=None, done_since=None, reviewer=None, workflow=None):
    stmt = select(Task, Project.project_key).join(Project, Project.id == Task.project_id)
    conds = []
    owner_id = owner_filter_id(session, me, owner)
    if owner_id is not None:
        conds.append(Project.owner_id == owner_id)
    if project:
        conds.append(Project.project_key.in_([p.strip().upper() for p in project.split(",") if p.strip()]))
    if assigned_to:
        if assigned_to == "me":
            conds.append(Task.assigned_to == me.id)
        elif assigned_to == "none":
            conds.append(Task.assigned_to.is_(None))
        else:
            conds.append(Task.assigned_to == _actor_by_name(session, assigned_to).id)
    if reviewer:
        conds.append(_reviewer_filter(session, me, reviewer))
    if status:
        conds.append(Task.status.in_(status))
    if prio:
        conds.append(Task.prio.in_(prio))
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(Task.title.like(like), Task.task_nr.like(like), Task.description.like(like)))
    if parent:
        conds.append(Task.parent_task_id == get(session, parent).id)
    if sprint:
        conds.append(_sprint_filter(sprint))
    if workflow and workflow != "all":
        conds.append(_workflow_filter(workflow))
    if done_since is not None:
        conds.append(func.coalesce(Task.done_at, Task.updated_at) >= done_since)
    if conds:
        stmt = stmt.where(and_(*conds))
    return stmt


def status_counts(session: Session, me: Actor, **filters) -> dict[str, int]:
    """Counts within the other filters, before the status selection or pagination."""
    rows = _search_statement(session, me, **{**filters, "status": None}).subquery()
    return dict(session.execute(select(rows.c.status, func.count()).group_by(rows.c.status)).all())


def workflow_badges(session: Session, tasks: list[Task]) -> dict[int, dict]:
    from app.services.workflows.ui import badges

    return badges(session, tasks)


def _workflow_filter(value: str):
    """`workflow=` filter (kanban/backlog, research/konzept-workflows.md §8.6): `hide` = no workflow tasks, `only` =
    only those, a template id = tasks of that template's runs."""
    from app.models import WorkflowRun, WorkflowRunStep

    if value == "hide":
        return Task.workflow_run_step_id.is_(None)
    if value == "only":
        return Task.workflow_run_step_id.is_not(None)
    if not value.isdigit():
        raise Unprocessable(_("workflow must be 'hide', 'all', 'only' or a workflow id"))
    return Task.workflow_run_step_id.in_(select(WorkflowRunStep.id).join(
        WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id).where(WorkflowRun.template_id == int(value)))


def _reviewer_filter(session: Session, me: Actor, value: str):
    if value == "me":
        return Task.reviewer_id == me.id
    if value == "none":
        return Task.reviewer_id.is_(None)
    if value == "agents":
        return Task.reviewer_id.in_(select(Actor.id).where(Actor.actor_type == "agent"))
    return Task.reviewer_id == _actor_by_name(session, value).id


def _sprint_filter(value: str):
    """`sprint=` filter: `none` = tasks without a sprint, otherwise comma-separated sprint ids."""
    if value.strip().lower() == "none":
        return Task.sprint_id.is_(None)
    try:
        ids = [int(v) for v in value.split(",") if v.strip()]
    except ValueError as e:
        raise Unprocessable(_("sprint must be 'none' or sprint ids")) from e
    return Task.sprint_id.in_(ids)


# --- Edit ----------------------------------------------------------------------------

EDITABLE = {"title", "description", "acceptance_criteria", "prio", "assigned_to", "due_date",
            "detail_task_git_url", "sprint", "model_tier", "model", "parent_task_nr", "depends_on", "reviewer",
            "data_class", "run"}


def update(session: Session, actor: Actor, task: Task, changes: dict, if_match: int | None) -> Task:
    project = session.get(Project, task.project_id)
    if actor.role not in ("admin", "orchestrator") and not access.owns(project, actor):
        raise Forbidden(_( 'Only admins, orchestrators, and the project owner can edit tasks' ))
    if set(changes) - EDITABLE:
        raise Unprocessable(_("Cannot change: %(fields)s", fields=", ".join(sorted(set(changes) - EDITABLE))))
    if if_match is not None and if_match != task.row_version:
        raise Conflict(_("Task changed concurrently (version %(actual)s, expected %(expected)s)", actual=task.row_version, expected=if_match),
                       code="version_conflict")
    if "model_tier" in changes:
        changes["model_tier"] = model_choice.normalize_tier(changes["model_tier"])
    if "model" in changes:
        changes["model"] = model_choice.normalize_model(changes["model"])
    if "data_class" in changes:
        _set_class(session, actor, task, project, zones.normalize_class(changes.pop("data_class")),
                   keep_assignee="assigned_to" not in changes)
    if "run" in changes:
        from app.services import runner_jobs

        runner_jobs.update_run(session, actor, task, project, changes.pop("run"))
    reassigned = False
    if "assigned_to" in changes:
        name = changes.pop("assigned_to")
        new = _actor_by_name(session, name) if name else None
        if (new.id if new else None) != task.assigned_to:
            reassigned = True
            access.require_may_use_agent(actor, new)  # Before the status reset below changes anything.
            if "model" in changes:  # Checked before anything changes, so a refusal leaves the task untouched.
                model_choice.check_allowed(new, changes["model"])
            if task.status == "done":
                raise Conflict(_( 'Completed tasks cannot be reassigned' ))
            if state_machine.reassign_resets_status(task):
                if not state_machine.can_override(session, task, actor):
                    raise Forbidden(_("Reassignment in status '%(status)s' is limited to human admins and the project owner", status=task.status))
                state_machine.reset_for_reassignment(session, task, actor, new.name if new else None)
            _assign(session, task, project, new, actor)
    _apply_model(session, actor, task, changes, reassigned)
    if "reviewer" in changes:
        reviews.set_reviewer(session, task, project, reviews.find(session, changes.pop("reviewer")), actor)
    if reassigned and task.reviewer_id and task.reviewer_id == task.assigned_to:
        raise Unprocessable(_("%(name)s is the task's reviewer and cannot work on it (no self-review) – choose "
                              "another reviewer first", name=session.get(Actor, task.reviewer_id).name),
                            code="self_review")
    if "parent_task_nr" in changes:
        _set_parent(session, actor, task, changes.pop("parent_task_nr"))
    if "depends_on" in changes:
        _set_dependencies(session, actor, task, changes.pop("depends_on") or [])
    if "sprint" in changes:
        sprint_id = changes.pop("sprint")
        if sprint_id != task.sprint_id:
            sprints.assign(session, task, sprints.target_for(session, project, sprint_id)
                           if sprint_id is not None else None, actor)
    if "prio" in changes:
        if not isinstance(changes["prio"], int) or not 1 <= changes["prio"] <= 5:
            raise Unprocessable(_( 'prio must be between 1 and 5' ))
        if changes["prio"] != task.prio:
            add_event(session, task, actor, "prio_change", task.prio, changes["prio"])
    if "title" in changes and not (changes["title"] or "").strip():
        raise Unprocessable(_("Title must not be empty"))
    for k, v in changes.items():
        setattr(task, k, v.strip() if isinstance(v, str) else v)
    task.row_version += 1
    session.flush()
    return task


def _set_class(session: Session, actor: Actor, task: Task, project: Project, new: str | None,
               keep_assignee: bool) -> None:
    """Change the task's own data class (rules 1–4 in `zones.check_class`). The assignee (unless the same request
    reassigns the task, then `_assign` checks the new one) and the entered reviewer must be able to handle it."""
    if new == task.data_class:
        return
    zones.check_class(project, task, new, actor)
    from app.services import data_refs

    data_refs.check_class(session, project, task, new)  # Rule 3: the references must fit the new class
    old = task.data_class
    task.data_class = new
    if keep_assignee and task.assigned_to:
        zones.check(project, session.get(Actor, task.assigned_to), task, session=session)
    if task.reviewer_id and task.status not in ("done", "cancelled"):
        reviews.check(project, session.get(Actor, task.reviewer_id), None, task)
    _class_event(session, task, project, actor, old, new)


def _class_event(session: Session, task: Task, project: Project, actor: Actor, old: str | None,
                 new: str | None) -> None:
    """History entry for a class change (`None` shown as the inherited class); a class looser than the project's
    is also written to the audit log (rule 2)."""
    inherit = _("inherited: %(cls)s", cls=project.privacy)
    add_event(session, task, actor, "data_class", old or inherit, new or inherit)
    if new is not None and zones.CLASS_RANK[new] < zones.CLASS_RANK[project.privacy]:
        audit(actor.name, "data_class_loosened", task=task.task_nr, project=project.project_key,
              data_class=new, project_privacy=project.privacy)
        from app.services import data_audit

        data_audit.append(session, "data_class_lowered", "project", project.id, actor=actor, via="api",
                          task_id=task.id, detail={"task": task.task_nr, "data_class": new,
                                                   "project_privacy": project.privacy})


def _set_parent(session: Session, actor: Actor, task: Task, parent_nr: str | None) -> None:
    """Parent task (same project; never the task itself or one of its subtasks). None/'' removes it."""
    parent = get(session, parent_nr.strip()) if parent_nr and parent_nr.strip() else None
    if (parent.id if parent else None) == task.parent_task_id:
        return
    if parent is not None:
        if parent.project_id != task.project_id:
            raise Unprocessable(_( 'Parent task must be in the same project' ))
        node, seen = parent, set()
        while node is not None and node.id not in seen:  # Walk up: the task must not become its own ancestor.
            if node.id == task.id:
                raise Unprocessable(_("%(task_nr)s cannot be its own parent or the parent of its parent",
                                      task_nr=task.task_nr))
            seen.add(node.id)
            node = session.get(Task, node.parent_task_id) if node.parent_task_id else None
    old = session.get(Task, task.parent_task_id) if task.parent_task_id else None
    task.parent_task_id = parent.id if parent else None
    add_event(session, task, actor, "parent", old.task_nr if old else None, parent.task_nr if parent else None)


def _set_dependencies(session: Session, actor: Actor, task: Task, depends_on: list[str]) -> None:
    """Replace the dependencies (task numbers); a task cannot depend on itself."""
    new = [get(session, nr.strip()) for nr in dict.fromkeys(nr.strip().upper() for nr in depends_on if nr.strip())]
    if any(d.id == task.id for d in new):
        raise Unprocessable(_("%(task_nr)s cannot depend on itself", task_nr=task.task_nr))
    seen, todo = set(), [d.id for d in new]
    while todo:  # a dependency chain that leads back to this task would never become claimable
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        todo += list(session.scalars(select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == node)))
    if task.id in seen:
        raise Unprocessable(_("%(task_nr)s would depend on itself through a circle of dependencies",
                              task_nr=task.task_nr))
    current = list(session.scalars(select(TaskDependency).where(TaskDependency.task_id == task.id)))
    old_nrs = sorted(session.scalars(select(Task.task_nr).where(
        Task.id.in_([d.depends_on_task_id for d in current])))) if current else []
    if old_nrs == sorted(d.task_nr for d in new):
        return
    for dep in current:
        session.delete(dep)
    session.flush()
    for dep in new:
        session.add(TaskDependency(task_id=task.id, depends_on_task_id=dep.id))
    add_event(session, task, actor, "dependencies", ", ".join(old_nrs) or None,
              ", ".join(sorted(d.task_nr for d in new)) or None)


def _apply_model(session: Session, actor: Actor, task: Task, changes: dict, reassigned: bool) -> None:
    """Model tier/concrete model of an update (after a reassignment): an explicit model must be allowed for the
    assignee; a model the new assignee does not allow is dropped with an event, the tier stays."""
    assignee = session.get(Actor, task.assigned_to) if task.assigned_to else None
    if "model" in changes:
        model_choice.check_allowed(assignee, changes["model"])
    elif reassigned and task.model and task.model not in model_choice.allowed(assignee):
        add_event(session, task, actor, "model_dropped", task.model, None,
                  _("not allowed for %(agent)s", agent=assignee.name) if assignee else _("no assignee"))
        task.model = None
    for key in ("model_tier", "model"):
        if key in changes and changes[key] != getattr(task, key):
            add_event(session, task, actor, key, getattr(task, key), changes[key])


def record_model_used(session: Session, actor: Actor, task: Task, model: str | None, note: str | None = None) -> Task:
    """The assigned agent's runner started the agent with `model` (None = the agent CLI's default)."""
    if task.assigned_to != actor.id:
        raise Forbidden(_( 'Only the assigned actor' ))
    if task.status != "in_progress":
        raise Conflict(_("%(task_nr)s has status '%(status)s'", task_nr=task.task_nr, status=task.status),
                       code="invalid_transition")
    add_event(session, task, actor, "model_used", None, model_choice.normalize_model(model) or _("agent default"),
              (note or "").strip()[:500] or None)
    session.flush()
    return task


def comment(session: Session, actor: Actor, task: Task, body: str) -> TaskComment:
    if not (body or "").strip():
        raise Unprocessable(_("Comment must not be empty"))
    c = add_comment(session, task, actor, "comment", body.strip())
    session.flush()
    return c


# --- Claim / Lease -------------------------------------------------------------------------

def _deps_done():
    dt = aliased(Task)
    return ~exists().where(TaskDependency.task_id == Task.id, TaskDependency.depends_on_task_id == dt.id,
                           dt.status.not_in(tuple(state_machine.RESOLVED_DEPENDENCY_STATUSES)))


def _own_review_pending(actor_id: int):
    """Another task of the same agent in the same project waits for review (for serialize_reviews)."""
    other = aliased(Task)
    return exists().where(other.assigned_to == actor_id, other.project_id == Task.project_id,
                          other.status == "for_review", other.id != Task.id)


def claim(session: Session, actor: Actor, task_nr: str | None = None, skip_locked: bool = False,
          reviews_too: bool = False) -> Task | None:
    """Atomically claim the next task (spec section 8). None means no task (API: 204).

    Locking the agent's `actors` row serializes its claims, keeping `max_parallel_tasks` race-free.
    On MariaDB < 10.6 (without SKIP LOCKED), a parallel claim waits only briefly. A lock timeout
    returns None and the runner retries later.

    `reviews_too` (runners that know review jobs): a reviewer agent first gets a task waiting for its review
    (`is_review_job` tells the result apart). A named task that waits for the agent's review is always claimed as
    a review job; an own review job claimed again only refreshes its lease."""
    if actor.actor_type != "agent" and actor.role != "admin":
        raise Forbidden(_( 'Only agents can claim tasks' ))
    try:
        session.execute(text(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_S}"))
        me = session.scalar(select(Actor).where(Actor.id == actor.id).with_for_update()
                            .execution_options(populate_existing=True))
        # Under REPEATABLE READ, an ordinary SELECT would see the transaction's initial snapshot
        # (e.g. from token validation) and miss tasks just claimed by parallel transactions.
        running = list(session.scalars(select(Task.task_nr).where(Task.assigned_to == me.id,
                                                                  Task.status == "in_progress")
                                       .order_by(Task.id).with_for_update()).all())
        running += list(session.scalars(select(Task.task_nr).where(_own_review_jobs(me.id))
                                        .order_by(Task.id).with_for_update()).all())
        named = get(session, task_nr, for_update=True) if task_nr else None
        if named is not None and named.status == "for_review" and named.reviewer_id == me.id:
            if reviews.claimed_by(named, me):  # Resumed review session: refresh the lease.
                reviews.start(session, named, me)
                return named
            if len(running) >= me.max_parallel_tasks:
                raise parallel_limit(me, running)
            return _claim_review(session, me, named)
        if len(running) >= me.max_parallel_tasks:
            if task_nr:
                if task_nr.upper() in running:
                    _explain_unclaimable(session, me, task_nr)  # Already in progress: invalid_transition
                raise parallel_limit(me, running)
            return None
        classes = zones.allowed_classes(me, zones.enforced(session))
        if reviews_too and not task_nr and me.role == "reviewer":
            job = session.scalar(_review_jobs(me, classes).with_for_update(skip_locked=skip_locked))
            if job is not None:
                reviews.start(session, job, me)
                return job
        stmt = (select(Task).join(Project, and_(Project.id == Task.project_id, Project.status == "active"))
                .where(Task.assigned_to == me.id, Task.status.in_(CLAIMABLE), _deps_done(),
                       or_(Task.workflow_run_step_id.is_(None), exists().where(
                           WorkflowRunStep.id == Task.workflow_run_step_id,
                           WorkflowRunStep.status.in_(("active", "waiting")))),
                       or_(Project.serialize_reviews.is_(False), ~_own_review_pending(me.id)),
                       zones.sql_classes(classes, zones.sql_effective_class()),  # Data zone, also old assignments
                       zones.sql_kind_for(me),  # Runners only profile/run, LLM agents never
                       # A run task the output gate sent back waits for a new script commit
                       or_(Task.task_kind != "run", Task.run_commit_sha.is_not(None))))
        if task_nr:
            stmt = stmt.where(Task.task_nr == task_nr.upper())
        # Rework before new work at the same priority: its branch is already under review
        rework_first = case((Task.status == "changes_requested", 0), else_=1)
        stmt = stmt.order_by(Task.prio.asc(), rework_first, Task.created_at.asc(), Task.id.asc()).limit(1)
        stmt = stmt.with_for_update(skip_locked=skip_locked)
        task = session.scalar(stmt)
        if task is None:
            if task_nr:
                _explain_unclaimable(session, me, task_nr)
            return None
        transition(session, task, "claim", me)
        return task
    except OperationalError as e:
        if getattr(e.orig, "args", [None])[0] in (1205, 1213):  # Lock-Wait-Timeout / Deadlock
            session.rollback()
            return None
        raise


def _own_review_jobs(actor_id: int):
    """Review jobs the agent holds (claimed, not yet decided)."""
    return and_(Task.reviewer_id == actor_id, Task.status == "for_review", Task.review_claimed_at.is_not(None))


def _review_jobs(me: Actor, classes: frozenset[str]):
    """Tasks waiting for `me`'s review: for_review, not claimed, active project, not its own task; with
    `serialize_reviews` only in projects where it holds no other review job. Oldest submission first by prio."""
    held = aliased(Task)
    serial_busy = exists().where(held.reviewer_id == me.id, held.project_id == Task.project_id,
                                 held.status == "for_review", held.review_claimed_at.is_not(None))
    stmt = (select(Task).join(Project, and_(Project.id == Task.project_id, Project.status == "active"))
            .where(Task.reviewer_id == me.id, Task.status == "for_review", Task.review_claimed_at.is_(None),
                   or_(Task.assigned_to.is_(None), Task.assigned_to != me.id),
                   or_(Project.serialize_reviews.is_(False), ~serial_busy)))
    stmt = stmt.where(zones.sql_classes(classes, zones.sql_effective_class()))  # Same rule as `reviews.effective`.
    return stmt.order_by(Task.prio.asc(), Task.submitted_at.asc(), Task.id.asc()).limit(1)


def _claim_review(session: Session, me: Actor, task: Task) -> Task:
    """Claim the named task's review job or explain why not."""
    project = session.get(Project, task.project_id)
    if me.role != "reviewer" or reviews.effective(session, task) is None:
        raise Forbidden(_("%(task_nr)s: %(name)s cannot review it (role reviewer, no self-review, privacy)",
                          task_nr=task.task_nr, name=me.name))
    if project.status != "active":
        raise Conflict(_("Project %(project)s is not active ('%(status)s')", project=project.project_key,
                         status=project.status), code="project_inactive")
    if project.serialize_reviews and session.scalar(select(Task.task_nr).where(
            _own_review_jobs(me.id), Task.project_id == project.id, Task.id != task.id)):
        raise Conflict(_("%(task_nr)s waits: you already review a task in project %(project)s (one review at a "
                         "time)", task_nr=task.task_nr, project=project.project_key), code="awaiting_review")
    reviews.start(session, task, me)
    return task


def is_review_job(task: Task, actor: Actor) -> bool:
    """The claim result is a review job of `actor` (not work on the task)."""
    return reviews.claimed_by(task, actor)


def parallel_limit(me: Actor, running: list[str]) -> Conflict:
    """The agent already has `max_parallel_tasks` tasks in progress (code `parallel_limit`)."""
    return Conflict(ngettext("Limit reached: %(name)s may work on %(num)d task at a time and has %(running)s in "
                             "progress. Finish, submit or release one of them first.",
                             "Limit reached: %(name)s may work on %(num)d tasks at a time and has %(running)s in "
                             "progress. Finish, submit or release one of them first.",
                             me.max_parallel_tasks, name=me.name, running=", ".join(running)),
                    code="parallel_limit")


def _explain_unclaimable(session: Session, me: Actor, task_nr: str) -> None:
    task = get(session, task_nr)
    project = session.get(Project, task.project_id)
    if task.assigned_to != me.id:
        raise Forbidden(_("%(task_nr)s is not assigned to you", task_nr=task.task_nr))
    if task.status not in CLAIMABLE:
        raise Conflict(_("%(task_nr)s has status '%(status)s'", task_nr=task.task_nr, status=task.status), code="invalid_transition")
    if project.status != "active":
        raise Conflict(_("Project %(project)s is not active ('%(status)s')", project=project.project_key, status=project.status), code="project_inactive")
    from app.services.workflows import engine

    engine.require_claimable(session, task)
    zones.check(project, me, task, session=session)  # e.g. assigned before `privacy_enforce_model_location`
    if project.serialize_reviews:
        waiting = session.scalar(select(Task.task_nr).where(
            Task.assigned_to == me.id, Task.project_id == project.id, Task.status == "for_review", Task.id != task.id))
        if waiting:
            raise Conflict(_("%(task_nr)s waits until %(waiting)s has been reviewed (project %(project)s takes one "
                             "review at a time per agent)", task_nr=task.task_nr, waiting=waiting,
                             project=project.project_key), code="awaiting_review")
    raise Conflict(_("%(task_nr)s has open dependencies", task_nr=task.task_nr), code="blocked")


WAIT_DEFAULT_MINUTES = 120
WAIT_MAX_MINUTES = 24 * 60


def heartbeat(session: Session, actor: Actor, task: Task, waiting: str | None = None, for_minutes: int | None = None,
              watch_file: str | None = None, done_waiting: bool = False) -> Task:
    """Extend the lease. `waiting` marks conscious waiting for a long background run (default 2 h, at most 24 h; a
    new call extends it; `watch_file` = a file on the agent machine whose changes count as activity),
    `done_waiting` ends it. A plain heartbeat leaves the waiting state as it is."""
    if reviews.claimed_by(task, actor):  # Review job: extend the review lease.
        return reviews.heartbeat(session, task, actor)
    if task.assigned_to != actor.id:
        raise Forbidden(_( 'Only the assigned actor' ))
    if task.status != "in_progress":
        raise Conflict(_("Heartbeat requires in_progress (current: '%(status)s')", status=task.status), code="invalid_transition")
    now = utcnow()
    if waiting is not None and waiting.strip():
        minutes = min(max(for_minutes or WAIT_DEFAULT_MINUTES, 1), WAIT_MAX_MINUTES)
        task.waiting_reason, task.waiting_until = waiting.strip()[:300], now + timedelta(minutes=minutes)
        task.waiting_file = (watch_file or "").strip()[:500] or None
    elif done_waiting:
        task.clear_waiting()
    task.lease_expires_at = now + timedelta(minutes=actor.lease_minutes)
    session.flush()
    return task


def sweep_expired_leases(session: Session) -> list[str]:
    """Move expired leases to defined (event lease_expired, notification)."""
    expired = list(session.scalars(select(Task).where(Task.status == "in_progress",
                                                      Task.lease_expires_at < utcnow()).with_for_update()))
    for task in expired:
        transition(session, task, "lease_expired", None)
    return [t.task_nr for t in expired] + reviews.sweep_expired(session)


def _agent_reviewed(enforce: bool):
    """SQL condition: the task has a reviewer agent that can review it (role reviewer, active, not the assignee,
    data zone) – the same rule as `reviews.effective`."""
    rv = aliased(Actor)
    return exists().where(rv.id == Task.reviewer_id, rv.actor_type == "agent", rv.role == "reviewer",
                          rv.is_active.is_(True), or_(Task.assigned_to.is_(None), Task.assigned_to != rv.id),
                          zones.sql_may_handle(rv, zones.sql_effective_class(), enforce))


def _agent_decided(rows, gate_free):
    """Inbox rows of tasks whose last status change was a reviewer agent sending them back (`reject`,
    `reject_conflict`) and that wait for the assignee (`changes_requested`); `decision`/`decided_at` per row."""
    latest = (select(TaskEvent.task_id, func.max(TaskEvent.id).label("event_id"))
              .where(TaskEvent.event_type == "status_change",
                     TaskEvent.task_id.in_(select(Task.id).where(Task.status == "changes_requested")))
              .group_by(TaskEvent.task_id).subquery())
    rv = aliased(Actor)
    stmt = (select(Task).join(latest, latest.c.task_id == Task.id).join(TaskEvent, TaskEvent.id == latest.c.event_id)
            .join(rv, rv.id == TaskEvent.actor_id)
            .where(Task.status == "changes_requested", gate_free, TaskEvent.from_value == "for_review",
                   rv.id == Task.reviewer_id, rv.actor_type == "agent")
            .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc()))
    return rows(stmt, cols={"decision": TaskEvent.note, "decided_at": TaskEvent.created_at})


def owner_filter_id(session: Session, me: Actor | None, owner: str | None) -> int | None:
    """Owner filter: None/''/'all' = everyone, 'me' = calling actor, otherwise an actor name."""
    if not owner or owner == "all":
        return None
    if owner == "me":
        return me.id if me else -1
    return _actor_by_name(session, owner).id


def inbox(session: Session, owner_id: int | None = None, sprint: str | None = None) -> dict[str, list[dict]]:
    """Inbox data (step 4): questions, reviews, and leases expired in the last 24 hours;
    optionally restrict to projects owned by one actor and to a sprint filter (see `_sprint_filter`).
    Reviews a reviewer agent does are listed separately (`agent_review`), not as waiting for the humans; a human
    entered as reviewer also sees the tasks it reviews in other owners' projects. `agent_decided` holds tasks a
    reviewer agent sent back (reject/conflict) that the assignee has not picked up again yet.
    `cols`: extra columns per row, as {key: column}, merged into each summary."""
    def rows(stmt, reviewer_too: bool = False, cols: dict | None = None):
        cols = cols or {}
        stmt = stmt.join(Project, Project.id == Task.project_id)
        if owner_id is not None:
            stmt = stmt.where(or_(Project.owner_id == owner_id, Task.reviewer_id == owner_id) if reviewer_too
                              else Project.owner_id == owner_id)
        if sprint:
            stmt = stmt.where(_sprint_filter(sprint))
        result = session.execute(stmt.add_columns(Project.project_key, *cols.values())).all()
        names = _names(session, {r[0].assigned_to for r in result} | {r[0].reviewer_id for r in result})
        badges = workflow_badges(session, [r[0] for r in result])
        return [{**summary(r[0], r[1], names, badges.get(r[0].id)), **dict(zip(cols, r[2:]))} for r in result]

    since = utcnow() - timedelta(hours=24)
    by_agent = _agent_reviewed(zones.enforced(session))
    lease_ids = select(TaskEvent.task_id).where(TaskEvent.event_type == "lease_expired", TaskEvent.created_at >= since)
    gate_free = Task.task_kind.not_in(tuple(zones.RUNNER_TASK_KINDS))
    return {
        "questions": rows(select(Task).where(Task.status == "questions").order_by(Task.updated_at)),
        # Runner tasks are decided in the output gate (inbox category "Data releases"), not reviewed here.
        "for_review": rows(select(Task).where(Task.status == "for_review", ~by_agent, gate_free)
                           .order_by(Task.submitted_at), reviewer_too=True),
        "agent_review": rows(select(Task).where(Task.status == "for_review", by_agent, gate_free)
                             .order_by(Task.submitted_at),
                             cols={"submitted_at": Task.submitted_at, "review_claimed_at": Task.review_claimed_at}),
        "agent_decided": _agent_decided(rows, gate_free),
        "aborted": rows(select(Task).where(Task.status == "aborted").order_by(Task.updated_at)),
        # Published without an actor: nobody will claim it.
        "unassigned": rows(select(Task).where(Task.status.in_(CLAIMABLE), Task.assigned_to.is_(None))
                           .order_by(Task.prio, Task.created_at)),
        "lease_expired": rows(select(Task).where(Task.id.in_(lease_ids)).order_by(Task.updated_at.desc())),
    }
