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
import json
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.i18n import _
from app.errors import AppError, Conflict, Forbidden, NotFound, Unprocessable
from app.models import SPRINT_STATUSES, Actor, Project, Sprint, Task, TaskDependency, TaskEvent
from app.services import access
from app.services.state_machine import END_STATUSES, RESOLVED_DEPENDENCY_STATUSES, add_event, missing_definition, transition, utcnow

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
    if sprint is not None and task.workflow_run_step_id is not None:
        raise Unprocessable(_("Workflow tasks cannot belong to a sprint"), code="workflow_task_no_sprint")
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


def batch_create(session: Session, actor: Actor, sprint: Sprint | None, entries: list[dict],
                 numbers: list[int] | None = None, project: Project | None = None) -> list[Task]:
    """Create several drafts in the sprint (`sprint` None: drafts without a sprint in `project`). All or nothing:
    the first invalid entry raises, and the caller's transaction rolls back the entries created before it.
    `numbers` are the entry numbers named in errors (default 1, 2, …)."""
    from app.services import tasks

    if sprint is not None:
        project = session.get(Project, sprint.project_id)
    require_plan(project, actor)
    if sprint is not None and sprint.status == "closed":
        raise Conflict(_("Sprint '%(sprint)s' is closed", sprint=sprint.name), code="sprint_closed")
    if not entries:
        raise Unprocessable(_("No tasks given"))
    created = []
    for i, entry in enumerate(entries):
        n = numbers[i] if numbers else i + 1
        try:
            created.append(tasks.create(session, actor, project=project.project_key, status="draft",
                                        sprint=sprint.id if sprint is not None else None, **entry))
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


def _update_draft(session: Session, actor: Actor, task: Task, changes: dict, row_version: int | None,
                  n: int) -> bool:
    """Apply plan `changes` to the draft `task` with `tasks.update` (errors name entry `n`); False if none."""
    from app.services import tasks

    if not changes:
        return False
    try:
        tasks.update(session, actor, task, changes, row_version)
    except AppError as e:
        raise type(e)(_("Entry %(n)d: %(error)s", n=n, error=e.message), code=e.code) from e
    return True


def prio_from_order(i: int, n: int) -> int:
    """Prio of the `i`-th (0-based) of `n` drafts when the order sets the prio: 1–5 spread over the list (the
    sprint editor's "Prio from order" in `static/sprint_editor.js` uses the same formula)."""
    return i + 1 if n <= 5 else 1 + (i * 5) // n


def reorder(session: Session, actor: Actor, sprint: Sprint, task_nrs: list[str]) -> list[str]:
    """Planning board: new order of a planned sprint's tasks. Every task gets its position as `sprint_rank` (as
    in `save_plan`); drafts also get their prio from the order (`prio_from_order`, via `tasks.update` like the
    sprint editor). Tasks of the sprint missing from `task_nrs` keep their relative order after the listed ones.
    Returns the drafts whose prio changed."""
    project = session.get(Project, sprint.project_id)
    require_plan(project, actor)
    if sprint.status != "planned":
        raise Conflict(_("Only tasks of a planned sprint can be reordered (status: '%(status)s')",
                         status=sprint.status), code="invalid_sprint_status")
    rows = list(session.scalars(select(Task).where(Task.sprint_id == sprint.id, Task.status != "cancelled")
                                .order_by(Task.sprint_rank.is_(None), Task.sprint_rank, Task.prio, Task.id)
                                .with_for_update().execution_options(populate_existing=True)))
    position = {nr.upper(): i for i, nr in enumerate(dict.fromkeys(task_nrs))}
    rows.sort(key=lambda t: position.get(t.task_nr, len(position)))  # Stable: unlisted ones keep their order.
    drafts = [t for t in rows if t.status == "draft"]
    changed = []
    for i, task in enumerate(drafts):
        prio = prio_from_order(i, len(drafts))
        if prio != task.prio and _update_draft(session, actor, task, {"prio": prio}, None, i + 1):
            changed.append(task.task_nr)
    for n, task in enumerate(rows, 1):
        task.sprint_rank = n
    session.flush()
    return changed


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
        if task.status == "draft" and _update_draft(session, actor, task, _plan_changes(session, task, entry),
                                                    entry.get("row_version"), n):
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

def _is_later(start: date | None, sprint_id: int, than: Sprint) -> bool:
    """Sprint (`start`, `sprint_id`) comes after `than`: by start date when both have one, otherwise by id
    (creation order)."""
    if start and than.start_date and start != than.start_date:
        return start > than.start_date
    return sprint_id > than.id


def late_dependencies(session: Session, sprint: Sprint, task_ids: list[int]) -> dict[int, list[dict]]:
    """Dependencies of `task_ids` that will not be done in time for `sprint` (task id → list): the dependency is not
    `done`/`cancelled` and has no sprint, sits in a planned sprint or in a sprint later than this one. Dependencies in the same
    sprint or in an earlier released/closed one are not listed. One query."""
    out: dict[int, list[dict]] = {}
    if not task_ids:
        return out
    rows = session.execute(
        select(TaskDependency.task_id, Task.task_nr, Task.title, Task.status, Sprint.id, Sprint.name, Sprint.status,
               Sprint.start_date)
        .join(Task, Task.id == TaskDependency.depends_on_task_id)
        .outerjoin(Sprint, Sprint.id == Task.sprint_id)
        .where(TaskDependency.task_id.in_(task_ids), Task.status.not_in(tuple(RESOLVED_DEPENDENCY_STATUSES))).order_by(Task.id))
    for task_id, nr, title, status, sid, sname, sstatus, sstart in rows:
        if sid == sprint.id:
            continue
        if sid is not None and sstatus != "planned" and not _is_later(sstart, sid, sprint):
            continue
        out.setdefault(task_id, []).append({"task_nr": nr, "title": title, "status": status, "sprint": sid,
                                            "sprint_name": sname, "sprint_status": sstatus})
    return out


def _release_lists(session: Session, sprint: Sprint, drafts: list[Task], publish=None) -> dict:
    released, blocked, ready = [], [], []
    for task in drafts:
        missing = missing_definition(task)
        if missing:
            blocked.append({"task_nr": task.task_nr, "title": task.title, "missing": missing})
        else:
            ready.append(task)
            released.append({"task_nr": task.task_nr, "title": task.title})
    late = late_dependencies(session, sprint, [t.id for t in ready])
    waiting = [{"task_nr": t.task_nr, "title": t.title, "waits_for": late[t.id]} for t in ready if t.id in late]
    if publish:
        for task in ready:
            publish(task)
    return {"released": released, "blocked": blocked, "waiting": waiting}


def release_preview(session: Session, sprint: Sprint) -> dict:
    """What `release` would do now, without changing anything: drafts that would be published, drafts that stay
    drafts with their missing fields (`missing_definition`) and, as a warning, the published ones that wait for a
    dependency that will not be done in time (`waiting`, see `late_dependencies`)."""
    drafts = session.scalars(select(Task).where(Task.sprint_id == sprint.id, Task.status == "draft")
                             .order_by(Task.prio, Task.id)).all()
    return _release_lists(session, sprint, drafts)


def release(session: Session, actor: Actor, sprint: Sprint) -> dict:
    """Publish every complete draft of the sprint (as `publish`, with the same events) and mark the sprint
    released. Incomplete drafts stay drafts and are returned with their missing fields; `waiting` lists published
    tasks with a late dependency (a warning, releasing is never refused for it)."""
    project = session.get(Project, sprint.project_id)
    access.require_manage_project(project, actor)
    if sprint.status == "closed":
        raise Conflict(_("Sprint '%(sprint)s' is closed", sprint=sprint.name), code="sprint_closed")
    drafts = session.scalars(select(Task).where(Task.sprint_id == sprint.id, Task.status == "draft")
                             .order_by(Task.prio, Task.id).with_for_update()
                             .execution_options(populate_existing=True)).all()
    result = _release_lists(session, sprint, drafts,
                            publish=lambda task: transition(session, task, "publish", actor, {}))
    if sprint.status == "planned":
        sprint.status, sprint.released_at = "released", utcnow()
    session.flush()
    return result


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
    # Remember which tasks the close moved out and where to, so `reopen` can tell a task moved again afterwards
    # from one still where the close left it.
    sprint.moved_task_ids = _pack_moved(open_tasks, target) if open_tasks else None
    session.flush()
    nrs = [t.task_nr for t in open_tasks]
    return {"moved_to": target.id if target else None, "moved": nrs if target else [],
            "detached": [] if target else nrs}


def moved_out_at_close(session: Session, sprint: Sprint) -> tuple[list[Task], list[str]]:
    """Tasks that `close` moved out of `sprint` (to another sprint or detached) and the skipped ones.

    The reliable source is `sprint.moved_task_ids`, written by `close` (`_pack_moved`): the ids of the moved tasks
    and the sprint the close moved them to (null = detached). It is immune to sprint renames and to tasks moved out
    by hand while the sprint was still released (a sprint name is only unique per project, so matching by name
    alone is not enough).

    For sprints closed before the `moved_task_ids` column existed (values are NULL) the lookup falls back to the
    task events of type `sprint` written at/after `closed_at` by the close call, limited to this sprint's project.

    Skipped either way: tasks that are done/cancelled, that are already back in this sprint, or that were moved
    again after the close call – `restore=True` must not overwrite that. Returns the tasks to restore and the task
    numbers to report."""
    stored = _stored_moved(sprint)
    if stored is not None:
        ids, target = stored
        moved_again = _moved_again_since_close(session, ids, target)
    else:
        fallback = _moved_ids_from_events(session, sprint)
        if fallback is None:
            return [], []
        ids, last = fallback
        moved_again = set(session.scalars(select(TaskEvent.task_id)
                                          .where(TaskEvent.event_type == "sprint", TaskEvent.task_id.in_(ids),
                                                 TaskEvent.id > last))) if ids else set()
    tasks = {t.id: t for t in session.scalars(select(Task).where(Task.id.in_(ids)).with_for_update()
                                             .execution_options(populate_existing=True))}
    restore, skipped = [], []
    for task_id in ids:
        task = tasks.get(task_id)
        if task is None:
            continue  # Task deleted in the meantime.
        if (task.status in END_STATUSES or task_id in moved_again or task.sprint_id == sprint.id):
            skipped.append(task.task_nr)
        else:
            restore.append(task)
    return restore, skipped


def _pack_moved(tasks: list[Task], target: Sprint | None) -> str:
    """JSON for `sprints.moved_task_ids`: the moved task ids and the sprint the close moved them to (null =
    detached). The target lets `reopen` recognise a task that was moved again afterwards."""
    return json.dumps({"ids": [t.id for t in tasks], "target": target.id if target else None})


def _stored_moved(sprint: Sprint) -> tuple[list[int], int | None] | None:
    """(task ids, close target) that `close` stored, or None when the sprint was closed before the column
    existed or the value cannot be read."""
    if sprint.moved_task_ids is None:
        return None
    try:
        data = json.loads(sprint.moved_task_ids)
    except (TypeError, ValueError):
        return None
    if isinstance(data, list):  # Defensive: an older/foreign shape with only the ids.
        return [int(i) for i in data], None
    return [int(i) for i in data.get("ids") or []], data.get("target")


def _moved_again_since_close(session: Session, ids: list[int], target: int | None) -> set[int]:
    """Stored-path counterpart of the event check: a task was moved again after the close when it is no longer in
    the sprint the close moved it to (null = detached)."""
    if not ids:
        return set()
    current = {task_id: sprint_id for task_id, sprint_id in
               session.execute(select(Task.id, Task.sprint_id).where(Task.id.in_(ids)))}
    return {task_id for task_id in ids if current.get(task_id, object()) != target}


def _moved_ids_from_events(session: Session, sprint: Sprint) -> tuple[list[int], int] | None:
    """Fallback for sprints closed before `moved_task_ids`: the `sprint` events written at/after `closed_at` with
    this sprint's name as old value, limited to this sprint's project. Returns the task ids and the id of the last
    such event (the close call's last move), or None when there is nothing to look at."""
    if sprint.closed_at is None:
        return None
    # MariaDB timestamps have second granularity and `closed_at` is set right after the events of the close call,
    # so the close's own events may lie up to one second before `closed_at`.
    since = sprint.closed_at - timedelta(seconds=1)
    events = list(session.scalars(
        select(TaskEvent).join(Task, Task.id == TaskEvent.task_id)
        .where(TaskEvent.event_type == "sprint", TaskEvent.from_value == sprint.name,
               Task.project_id == sprint.project_id, TaskEvent.created_at >= since)
        .order_by(TaskEvent.id)))
    if not events:
        return None
    return list(dict.fromkeys(e.task_id for e in events)), events[-1].id


def reopen(session: Session, actor: Actor, sprint: Sprint, restore_tasks: bool = True) -> dict:
    """Reopen a closed sprint: status back to `released`, `closed_at` cleared. Same permission as `close`.

    With `restore_tasks` the tasks that `close` moved out of the sprint (to another sprint or detached) are moved
    back with `assign` (so events are written); their status is untouched. Tasks that someone moved again after
    the close, or that are done/cancelled, are skipped and reported in `skipped`."""
    project = session.get(Project, sprint.project_id)
    access.require_manage_project(project, actor)
    if sprint.status != "closed":
        raise Conflict(_("Only closed sprints can be reopened (status: '%(status)s')", status=sprint.status),
                       code="invalid_sprint_status")
    restore, skipped = moved_out_at_close(session, sprint) if restore_tasks else ([], [])
    sprint.status, sprint.closed_at, sprint.moved_task_ids = "released", None, None
    for task in restore:
        assign(session, task, sprint, actor)
        task.row_version += 1
    session.flush()
    return {"restored": [t.task_nr for t in restore], "skipped": skipped}


def parse_statuses(status: list[str] | None) -> list[str] | None:
    statuses = [s for v in status or [] for s in v.split(",") if s]
    unknown = set(statuses) - set(SPRINT_STATUSES)
    if unknown:
        raise Unprocessable(_("Unknown sprint status: %(status)s", status=", ".join(sorted(unknown))))
    return statuses or None
