"""Sprints: optional level between project and task (project → sprint → task).

A sprint belongs to exactly one project, whose rights apply. Tasks in a `planned` sprint are drafts: creating one
there always yields a draft, and assigning a `defined` task returns it to draft. Releasing (`planned` → `released`)
publishes every draft with a description and acceptance criteria and lists the others with their missing fields;
releasing again later publishes drafts completed since. Closing (`released` → `closed`) moves open tasks to another
sprint or detaches them without touching their status; finished tasks stay in the sprint. Tasks without a sprint
behave exactly as before.

Rights: create/edit/assign/batch – admins, orchestrators and the project owner; release/close – admins and the
project owner (like publishing a single task as a human).
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.i18n import _
from app.errors import AppError, Conflict, Forbidden, NotFound, Unprocessable
from app.models import SPRINT_STATUSES, Actor, Project, Sprint, Task
from app.services import access
from app.services.state_machine import END_STATUSES, add_event, missing_definition, transition, utcnow

EDITABLE = {"name", "goal", "start_date", "end_date"}


def can_plan(project: Project, actor: Actor | None) -> bool:
    """Create/edit sprints, assign tasks to them, batch creation."""
    return actor is not None and (actor.role in ("admin", "orchestrator") or access.owns(project, actor))


def require_plan(project: Project, actor: Actor) -> None:
    if not can_plan(project, actor):
        raise Forbidden(_("Project %(project)s: only admins, orchestrators and the owner can plan sprints",
                          project=project.project_key))


def planned_notice(sprint: Sprint) -> str:
    return _("Sprint '%(sprint)s' is not released yet – the task was created as a draft.", sprint=sprint.name)


# --- Read --------------------------------------------------------------------------------

def get(session: Session, sprint_id: int, project: Project | None = None, for_update: bool = False) -> Sprint:
    stmt = select(Sprint).where(Sprint.id == sprint_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    sprint = session.scalar(stmt)
    if sprint is None or (project is not None and sprint.project_id != project.id):
        raise NotFound(_("Sprint %(sprint)s not found", sprint=sprint_id))
    return sprint


def list_for_project(session: Session, project: Project, status: list[str] | None = None) -> list[Sprint]:
    stmt = select(Sprint).where(Sprint.project_id == project.id)
    if status:
        stmt = stmt.where(Sprint.status.in_(status))
    return list(session.scalars(stmt.order_by(Sprint.start_date.is_(None), Sprint.start_date, Sprint.id)))


def task_counts(session: Session, sprint_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {i: {} for i in sprint_ids}
    if sprint_ids:
        rows = session.execute(select(Task.sprint_id, Task.status, func.count())
                               .where(Task.sprint_id.in_(sprint_ids)).group_by(Task.sprint_id, Task.status))
        for sid, status, n in rows:
            out[sid][status] = n
    return out


def info(session: Session, sprint: Sprint, counts: dict[str, int] | None = None) -> dict:
    project = session.get(Project, sprint.project_id)
    creator = session.get(Actor, sprint.created_by)
    if counts is None:
        counts = task_counts(session, [sprint.id])[sprint.id]
    return {"id": sprint.id, "project": project.project_key, "name": sprint.name, "goal": sprint.goal,
            "start_date": sprint.start_date, "end_date": sprint.end_date, "status": sprint.status,
            "created_by": creator.name if creator else None, "released_at": sprint.released_at,
            "closed_at": sprint.closed_at, "created_at": sprint.created_at, "updated_at": sprint.updated_at,
            "task_counts": counts}


# --- Create / edit -------------------------------------------------------------------------

def _check_dates(start: date | None, end: date | None) -> None:
    if start and end and end < start:
        raise Unprocessable(_("The end date must not be before the start date"), code="invalid_dates")


def _check_name(session: Session, project: Project, name: str | None, sprint_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise Unprocessable(_("Name is required"), code="missing_field")
    other = session.scalar(select(Sprint.id).where(Sprint.project_id == project.id, Sprint.name == name))
    if other is not None and other != sprint_id:
        raise Conflict(_("Project %(project)s already has a sprint named '%(name)s'", project=project.project_key,
                         name=name), code="sprint_exists")
    return name[:120]


def create(session: Session, actor: Actor, project: Project, *, name: str, goal: str | None = None,
           start_date: date | None = None, end_date: date | None = None) -> Sprint:
    require_plan(project, actor)
    if project.status == "archived":
        raise Unprocessable(_( 'Project is archived' ))
    name = _check_name(session, project, name)
    _check_dates(start_date, end_date)
    sprint = Sprint(project_id=project.id, name=name, goal=(goal or "").strip() or None, start_date=start_date,
                    end_date=end_date, status="planned", created_by=actor.id)
    session.add(sprint)
    session.flush()
    return sprint


def update(session: Session, actor: Actor, sprint: Sprint, changes: dict) -> list[str]:
    """Edit name, goal and dates; the status changes only through release/close."""
    project = session.get(Project, sprint.project_id)
    require_plan(project, actor)
    if set(changes) - EDITABLE:
        raise Unprocessable(_("Cannot change: %(fields)s", fields=", ".join(sorted(set(changes) - EDITABLE))))
    if "name" in changes:
        changes["name"] = _check_name(session, project, changes["name"], sprint.id)
    if "goal" in changes:
        changes["goal"] = (changes["goal"] or "").strip() or None
    _check_dates(changes.get("start_date", sprint.start_date), changes.get("end_date", sprint.end_date))
    changed = [k for k, v in changes.items() if getattr(sprint, k) != v]
    for k in changed:
        setattr(sprint, k, changes[k])
    session.flush()
    return changed


# --- Task assignment -----------------------------------------------------------------------

def target_for(session: Session, project: Project, sprint_id: int) -> Sprint:
    """Sprint a task of `project` may be assigned to: same project, not closed."""
    sprint = session.scalar(select(Sprint).where(Sprint.id == sprint_id))
    if sprint is None:
        raise Unprocessable(_("Sprint %(sprint)s does not exist", sprint=sprint_id), code="sprint_not_found")
    if sprint.project_id != project.id:
        raise Unprocessable(_("Sprint '%(sprint)s' belongs to another project", sprint=sprint.name),
                            code="sprint_other_project")
    if sprint.status == "closed":
        raise Conflict(_("Sprint '%(sprint)s' is closed", sprint=sprint.name), code="sprint_closed")
    return sprint


def assign(session: Session, task: Task, sprint: Sprint | None, actor: Actor) -> None:
    """Change a task's sprint (None detaches). A `defined` task moved into a planned sprint returns to draft
    (status event `to_draft`); every other status is kept. Does not check planning rights."""
    old = session.get(Sprint, task.sprint_id) if task.sprint_id else None
    if (old.id if old else None) == (sprint.id if sprint else None):
        return
    if sprint is not None and sprint.project_id != task.project_id:
        raise Unprocessable(_("Sprint '%(sprint)s' belongs to another project", sprint=sprint.name),
                            code="sprint_other_project")
    task.sprint_id = sprint.id if sprint else None
    add_event(session, task, actor, "sprint", old.name if old else None, sprint.name if sprint else None)
    if sprint is not None and sprint.status == "planned" and task.status == "defined":
        transition(session, task, "to_draft", actor, {})


def batch_create(session: Session, actor: Actor, sprint: Sprint, entries: list[dict],
                 numbers: list[int] | None = None) -> list[Task]:
    """Create several drafts in the sprint. All or nothing: the first invalid entry raises, and the caller's
    transaction rolls back the entries created before it. `numbers` are the entry numbers named in errors
    (default 1, 2, …)."""
    from app.services import tasks

    project = session.get(Project, sprint.project_id)
    require_plan(project, actor)
    if sprint.status == "closed":
        raise Conflict(_("Sprint '%(sprint)s' is closed", sprint=sprint.name), code="sprint_closed")
    if not entries:
        raise Unprocessable(_("No tasks given"))
    created = []
    for i, entry in enumerate(entries):
        n = numbers[i] if numbers else i + 1
        try:
            created.append(tasks.create(session, actor, project=project.project_key, status="draft",
                                        sprint=sprint.id, **entry))
        except AppError as e:
            raise type(e)(_("Entry %(n)d: %(error)s", n=n, error=e.message), code=e.code) from e
    return created


def ordered_tasks(session: Session, sprint: Sprint) -> list[Task]:
    """Tasks of the sprint in display order: editor position (`sprint_rank`) first, then prio and id."""
    return list(session.scalars(select(Task).where(Task.sprint_id == sprint.id)
                                .order_by(Task.sprint_rank.is_(None), Task.sprint_rank, Task.prio, Task.id)))


PLAN_FIELDS = ("title", "description", "acceptance_criteria", "assigned_to", "prio", "due_date", "model_tier", "model",
               "reviewer")
OPTIONAL_PLAN_FIELDS = ("model_tier", "model", "reviewer")  # Entries without them keep the task's values


def _plan_changes(session: Session, task: Task, entry: dict) -> dict:
    """Fields of `entry` that differ from the draft `task` (empty text = None), in `tasks.update` form."""
    changes = {}
    for f in PLAN_FIELDS:
        if f in OPTIONAL_PLAN_FIELDS and f not in entry:
            continue
        value = entry.get(f)
        if isinstance(value, str):
            value = value.strip() or None
        if f == "reviewer":  # Empty keeps the reviewer, `none` removes it (new entries: empty = project default).
            if value is None:
                continue
            current = session.get(Actor, task.reviewer_id).name if task.reviewer_id else "none"
            if value != current:
                changes[f] = None if value == "none" else value
            continue
        if f == "assigned_to":
            current = session.get(Actor, task.assigned_to).name if task.assigned_to else None
        else:
            current = getattr(task, f)
            current = (current.strip() or None) if isinstance(current, str) else current
        if value != current:
            changes[f] = value
    return changes


def save_plan(session: Session, actor: Actor, project: Project, sprint: Sprint | None, *, name: str,
              goal: str | None = None, start_date: date | None = None, end_date: date | None = None,
              entries: list[dict], removed: list[str] | None = None) -> dict:
    """Sprint editor: create (`sprint` None) or edit a planned sprint together with its tasks, in the caller's
    transaction (one commit or nothing).

    `entries` is the editor list in display order; each entry has `title`, `description`, `acceptance_criteria`,
    `assigned_to` (name or None), `prio`, `due_date`, optionally `model_tier`/`model`/`reviewer` (empty = project
    default for new entries and unchanged for existing ones, `none` = owner and admins) and, for an existing task,
    `task_nr` and `row_version`.
    New entries become drafts (`batch_create`); existing drafts are updated with `tasks.update` (version check);
    tasks past draft are read-only here and only keep their position. `removed` names drafts of the sprint to
    cancel. Every listed task gets its position as `sprint_rank`."""
    from app.services import tasks

    if sprint is None:
        sprint = create(session, actor, project, name=name, goal=goal, start_date=start_date, end_date=end_date)
    else:
        if sprint.status != "planned":
            raise Conflict(_("Only planned sprints can be edited in the sprint editor (status: '%(status)s')",
                             status=sprint.status), code="invalid_sprint_status")
        update(session, actor, sprint, {"name": name, "goal": goal, "start_date": start_date,
                                        "end_date": end_date})
    existing = {t.task_nr: t for t in session.scalars(
        select(Task).where(Task.sprint_id == sprint.id).with_for_update().execution_options(populate_existing=True))}
    result = {"sprint": sprint, "created": [], "updated": [], "cancelled": []}

    for nr in dict.fromkeys(removed or []):
        task = existing.get(nr)
        if task is None or task.status == "cancelled":
            continue  # Moved or cancelled elsewhere in the meantime.
        if task.status != "draft":
            raise Conflict(_("%(task_nr)s is no longer a draft and cannot be removed in the sprint editor",
                             task_nr=nr), code="not_a_draft")
        transition(session, task, "cancel", actor, {"body": _("Removed in the sprint editor")})
        task.row_version += 1
        result["cancelled"].append(nr)

    new, numbers = [], []
    for n, entry in enumerate(entries, 1):
        nr = entry.get("task_nr")
        if not nr:
            new.append({f: entry.get(f) or None for f in PLAN_FIELDS} | {"prio": entry.get("prio") or 3})
            numbers.append(n)
            continue
        task = existing.get(nr)
        if task is None or nr in result["cancelled"]:
            raise Conflict(_("Entry %(n)d: %(task_nr)s is no longer part of this sprint – reload the editor",
                             n=n, task_nr=nr), code="not_in_sprint")
        if task.status == "draft":
            changes = _plan_changes(session, task, entry)
            if changes:
                try:
                    tasks.update(session, actor, task, changes, entry.get("row_version"))
                except AppError as e:
                    raise type(e)(_("Entry %(n)d: %(error)s", n=n, error=e.message), code=e.code) from e
                result["updated"].append(nr)
        task.sprint_rank = n
    if new:
        created = batch_create(session, actor, sprint, new, numbers)
        for task, n in zip(created, numbers):
            task.sprint_rank = n
        result["created"] = [t.task_nr for t in created]
    session.flush()
    return result


# --- Release / close -----------------------------------------------------------------------

def release_preview(session: Session, sprint: Sprint) -> dict:
    """What `release` would do now, without changing anything: drafts that would be published and drafts that
    stay drafts with their missing fields (`missing_definition`)."""
    drafts = session.scalars(select(Task).where(Task.sprint_id == sprint.id, Task.status == "draft")
                             .order_by(Task.prio, Task.id)).all()
    released, blocked = [], []
    for task in drafts:
        missing = missing_definition(task)
        if missing:
            blocked.append({"task_nr": task.task_nr, "title": task.title, "missing": missing})
        else:
            released.append({"task_nr": task.task_nr, "title": task.title})
    return {"released": released, "blocked": blocked}


def release(session: Session, actor: Actor, sprint: Sprint) -> dict:
    """Publish every complete draft of the sprint (as `publish`, with the same events) and mark the sprint
    released. Incomplete drafts stay drafts and are returned with their missing fields."""
    project = session.get(Project, sprint.project_id)
    access.require_manage_project(project, actor)
    if sprint.status == "closed":
        raise Conflict(_("Sprint '%(sprint)s' is closed", sprint=sprint.name), code="sprint_closed")
    drafts = session.scalars(select(Task).where(Task.sprint_id == sprint.id, Task.status == "draft")
                             .order_by(Task.prio, Task.id).with_for_update()
                             .execution_options(populate_existing=True)).all()
    released, blocked = [], []
    for task in drafts:
        missing = missing_definition(task)
        if missing:
            blocked.append({"task_nr": task.task_nr, "title": task.title, "missing": missing})
        else:
            transition(session, task, "publish", actor, {})
            released.append({"task_nr": task.task_nr, "title": task.title})
    if sprint.status == "planned":
        sprint.status, sprint.released_at = "released", utcnow()
    session.flush()
    return {"released": released, "blocked": blocked}


def close(session: Session, actor: Actor, sprint: Sprint, move_to: int | None = None,
          detach: bool = False) -> dict:
    """Close a released sprint. Open tasks (any status except done/cancelled) move to `move_to` or are detached;
    only the assignment changes, running tasks keep their status. Finished tasks stay in the sprint."""
    project = session.get(Project, sprint.project_id)
    access.require_manage_project(project, actor)
    if sprint.status != "released":
        raise Conflict(_("Only released sprints can be closed (status: '%(status)s')", status=sprint.status),
                       code="invalid_sprint_status")
    if move_to is not None and detach:
        raise Unprocessable(_("Choose either move_to or detach, not both"))
    target = None
    if move_to is not None:
        target = target_for(session, project, move_to)
        if target.id == sprint.id:
            raise Unprocessable(_("Open tasks cannot be moved to the sprint being closed"))
    open_tasks = session.scalars(select(Task).where(Task.sprint_id == sprint.id,
                                                    Task.status.not_in(END_STATUSES))
                                 .order_by(Task.id).with_for_update()
                                 .execution_options(populate_existing=True)).all()
    if open_tasks and target is None and not detach:
        raise Unprocessable(_("%(count)d open tasks: choose move_to=<sprint> or detach", count=len(open_tasks)),
                            code="open_tasks")
    for task in open_tasks:
        assign(session, task, target, actor)
        task.row_version += 1
    sprint.status, sprint.closed_at = "closed", utcnow()
    session.flush()
    nrs = [t.task_nr for t in open_tasks]
    return {"moved_to": target.id if target else None, "moved": nrs if target else [],
            "detached": [] if target else nrs}


def parse_statuses(status: list[str] | None) -> list[str] | None:
    statuses = [s for v in status or [] for s in v.split(",") if s]
    unknown = set(statuses) - set(SPRINT_STATUSES)
    if unknown:
        raise Unprocessable(_("Unknown sprint status: %(status)s", status=", ".join(sorted(unknown))))
    return statuses or None
