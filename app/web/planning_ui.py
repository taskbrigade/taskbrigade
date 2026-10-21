"""UI: planning board – the sprints of one project as a full-width accordion list (`/planning`).

One card per sprint (planned, released, closed) with expand/collapse, progress and its tasks; closed sprints are
hidden by default. The segmented filter (active/planned/closed/all) and the "show closed sprints" toggle narrow the
list. Moving a task row to another sprint calls `sprints.assign`, reordering inside a planned sprint
`sprints.reorder`; release, close, edit and "+ New sprint" post to the sprint routes in `sprints_ui` with
`back=planning`. All rules live in `app/services/sprints.py`; the board loads with a fixed number of queries,
whatever the task count.
"""
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from app.auth import UiUser, csrf_protect
from app.errors import AppError, Conflict, Forbidden
from app.i18n import gettext as _
from app.i18n import ngettext
from app.logging_setup import audit
from app.models import Actor, Project, Sprint, Task, TaskDependency
from app.services import access, projects, sprints
from app.services import tasks as task_svc
from app.services.state_machine import END_STATUSES, RESOLVED_DEPENDENCY_STATUSES
from app.web.common import render, ui_actor, web_session
from app.web.sprints_ui import parse_batch
from app.web.tasks_ui import agent_choices, require_user

router = APIRouter(prefix="/planning")
POST = [Depends(csrf_protect)]
PROJECT_COOKIE = "tm_planning_project"
NO_SPRINT = "none"
STATUS_ORDER = {"released": 0, "planned": 1, "closed": 2}
# Segmented filter: "active" means running (planned + released) sprints; every sprint is also reachable through "all".
FILTERS = (("all", ("planned", "released", "closed")), ("active", ("planned", "released")),
           ("planned", ("planned",)), ("closed", ("closed",)))
# Default filter: "all" shows planned and released sprints with closed ones hidden (the mockup's pressed default),
# so `/planning` without parameters shows what is being planned and what is running.
DEFAULT_FILTER = "all"


def may_plan_somewhere(session, me: Actor | None) -> bool:
    """Menu item "Planning": admins and orchestrators, or the owner of a project that is not archived."""
    if me is None:
        return False
    if me.role in ("admin", "orchestrator"):
        return True
    return me.actor_type == "human" and session.scalar(
        select(Project.id).where(Project.owner_id == me.id, Project.status != "archived").limit(1)) is not None


def _choices(session, me: Actor, wanted: str) -> list[Project]:
    """Projects `me` may plan in; with "Show all projects" off only own ones (plus an explicitly requested one)."""
    rows = [p for p in session.scalars(select(Project).where(Project.status != "archived")
                                       .order_by(Project.project_key)) if sprints.can_plan(p, me)]
    if access.own_projects_only(me):
        rows = [p for p in rows if p.owner_id == me.id or p.project_key == wanted]
    return rows


def _url(key: str, msg: str | None = None, err: str | None = None) -> str:
    url = f"/planning?project={quote_plus(key)}"
    return f"{url}&msg={quote_plus(msg)}" if msg else f"{url}&err={quote_plus(err)}" if err else url


def waiting_for(session, task_ids: list[int]) -> dict[int, list[str]]:
    """Unfinished dependencies per task (task id → task numbers it waits for), in one query."""
    out: dict[int, list[str]] = {}
    if task_ids:
        rows = session.execute(select(TaskDependency.task_id, Task.task_nr)
                               .join(Task, Task.id == TaskDependency.depends_on_task_id)
                               .where(TaskDependency.task_id.in_(task_ids), Task.status.not_in(tuple(RESOLVED_DEPENDENCY_STATUSES)))
                               .order_by(Task.id))
        for task_id, nr in rows:
            out.setdefault(task_id, []).append(nr)
    return out


def _card(task: Task, names: dict[int, str], waits: dict[int, list[str]]) -> dict:
    return {"task_nr": task.task_nr, "title": task.title, "status": task.status, "prio": task.prio,
            "assigned_to": names.get(task.assigned_to), "waits": waits.get(task.id, [])}


def _order(sprint: Sprint) -> tuple:
    """Released before planned (closed last), each by start date (none last), then id."""
    return STATUS_ORDER.get(sprint.status, 3), sprint.start_date is None, sprint.start_date or 0, sprint.id


def _sprint_rows(session, project: Project) -> list[dict]:
    """Every sprint of the project with its task rows and counts – a constant number of queries.

    Each entry carries the sprint, its rows (`cards`) and the numbers the header shows:
    `done`, `total` (without cancelled), `open` (total − done), `drafts` and `empty`.
    """
    rows = sorted(sprints.list_for_project(session, project), key=_order)
    ids = [x.id for x in rows]
    counts = sprints.task_counts(session, ids)
    tasks = list(session.scalars(select(Task).where(Task.sprint_id.in_(ids), Task.status != "cancelled")
                 .order_by(Task.sprint_rank.is_(None), Task.sprint_rank, Task.prio, Task.id))) if ids else []
    names = task_svc._names(session, {t.assigned_to for t in tasks})
    waits = waiting_for(session, [t.id for t in tasks])
    by_sprint: dict[int, list[dict]] = {}
    for t in tasks:
        by_sprint.setdefault(t.sprint_id, []).append(_card(t, names, waits))
    out = []
    for x in rows:
        c = counts[x.id]
        total = sum(c.values()) - c.get("cancelled", 0)
        cards = by_sprint.get(x.id, [])
        out.append({"sprint": x, "cards": cards, "total": total, "done": c.get("done", 0),
                    "open": total - c.get("done", 0), "drafts": c.get("draft", 0), "empty": not cards,
                    "moved_out": len(sprints.moved_out_at_close(session, x)[0]) if x.status == "closed" else 0})
    return out


def filter_counts(rows: list[dict]) -> dict[str, int]:
    """Counts of the segmented filter: active = running (planned + released); all = every sprint; plus closed."""
    out = {"all": len(rows)}
    for key, statuses in FILTERS:
        if key != "all":
            out[key] = sum(1 for r in rows if r["sprint"].status in statuses)
    return out


def visible(rows: list[dict], active: str, show_closed: bool) -> list[dict]:
    """Sprints the filter shows; closed ones stay hidden even under "All" while the toggle is off."""
    statuses = dict(FILTERS).get(active, FILTERS[0][1])
    return [r for r in rows if r["sprint"].status in statuses and
            (show_closed or r["sprint"].status != "closed")]


@router.get("")
def planning(request: Request, user: UiUser = Depends(require_user)):
    qp = request.query_params
    wanted = (qp.get("project") or "").strip().upper()
    with web_session() as s:
        me = ui_actor(s, user)
        if not may_plan_somewhere(s, me):
            raise Forbidden(_("Only admins, orchestrators and project owners can plan sprints"))
        choices = _choices(s, me, wanted)
        keys = [p.project_key for p in choices]
        remembered = (request.cookies.get(PROJECT_COOKIE) or "").upper()
        key = wanted if wanted in keys else remembered if remembered in keys else (keys[0] if keys else None)
        project = next((p for p in choices if p.project_key == key), None)
        show_closed = qp.get("closed") == "1"
        active = qp.get("filter") if qp.get("filter") in dict(FILTERS) else DEFAULT_FILTER
        all_rows = _sprint_rows(s, project) if project else []
        rows = visible(all_rows, active, show_closed)
        # Counts always cover every sprint of the project (closed ones too), so the segmented filter agrees with the
        # subtitle's total – the "show closed" toggle only narrows the displayed list, never the counts.
        counts = filter_counts(all_rows)
        can_manage = access.can_manage_project(project, me) if project else False
        project_ctx = {"key": project.project_key, "name": project.name} if project else None
        sprints_n = len(all_rows)
    targets = [r["sprint"] for r in all_rows if r["sprint"].status != "closed"]
    response = render(request, "planning.html", projects=[(p, n) for p, n in
                                                          ((c.project_key, c.name) for c in choices)],
                      project=project_ctx, rows=rows, counts=counts, active=active, show_closed=show_closed,
                      targets=targets, can_manage=can_manage, sprint_count=sprints_n, owner_hint=not choices)
    if project_ctx and key != remembered:
        response.set_cookie(PROJECT_COOKIE, key, max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


@router.get("/{key}/sprints/{sprint_id}/release")
def release_preview(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user)):
    """Content of the "Release…" dialog: the existing release preview with the release button."""
    with web_session() as s:
        me = ui_actor(s, user)
        project = projects.get(s, key)
        sprint = sprints.get(s, sprint_id, project)
        access.require_manage_project(project, me)
        if sprint.status == "closed":
            raise Conflict(_("Sprint '%(sprint)s' is closed", sprint=sprint.name), code="sprint_closed")
        preview = sprints.release_preview(s, sprint)
        info = {"id": sprint.id, "status": sprint.status}
    return render(request, "_release_preview.html", p={"project_key": project.project_key}, sprint=info,
                  preview=preview, back="planning")


@router.post("/move", dependencies=POST)
def move(request: Request, user: UiUser = Depends(require_user), task_nr: str = Form(...),
         sprint: str = Form(...), order: str = Form("")):
    """Drag and drop (and the row menu): put the task into `sprint` (an id or `none`) via `sprints.assign`; inside a
    planned sprint `order` (task numbers of the target list, top to bottom) sets rank and draft prio via
    `sprints.reorder`. Answers with the new row; errors as JSON `{message}` – the row goes back."""
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            task = task_svc.get(s, task_nr, for_update=True)
            project = s.get(Project, task.project_id)
            sprints.require_plan(project, me)
            target = None if sprint == NO_SPRINT else sprints.target_for(
                s, project, int(sprint) if sprint.isdigit() else -1)
            if (target.id if target else None) != task.sprint_id:
                if task.status in END_STATUSES:
                    raise Conflict(_("%(task_nr)s is finished and stays in its sprint", task_nr=task.task_nr),
                                   code="task_finished")
                sprints.assign(s, task, target, me)
                task.row_version += 1
                audit(me.name, "task_sprint_moved", task=task.task_nr, sprint=target.id if target else None,
                      via="planning")
            if target is not None and target.status == "planned" and order.strip():
                sprints.reorder(s, me, target, order.replace(",", " ").split())
            s.flush()
            names = task_svc._names(s, {task.assigned_to})
            card = _card(task, names, waiting_for(s, [task.id]))
            key, sid = project.project_key, task.sprint_id
            may_manage = access.can_manage_project(project, me)
            targets = [x for x in sprints.list_for_project(s, project) if x.status != "closed"]
    except AppError as e:
        return JSONResponse({"message": e.message}, status_code=e.status)
    return render(request, "_plan_row.html", t=card, project={"key": key}, sprint={"id": sid},
                  can_manage=may_manage, targets=targets, no_sprint=NO_SPRINT)


@router.post("/{key}/quick-add", dependencies=POST)
def quick_add(key: str, user: UiUser = Depends(require_user), sprint: str = Form(NO_SPRINT),
              lines: str = Form("")):
    """"+ Task" of a sprint card: one draft per line (`Title | Agent | Prio | Reviewer`, as the sprint batch).
    Redirects back to the sprint card (`#sprint-<id>`) so the new drafts are in view."""
    key = key.upper()
    anchor = "#sprint-" + (sprint if sprint.isdigit() else NO_SPRINT)
    with web_session() as s:
        agents = set(s.scalars(select(Actor.name).where(Actor.is_active.is_(True))))
        me = ui_actor(s, user)
        visible = None if me.role == "admin" else set(agent_choices(s, me))
    rows = parse_batch(lines, agents, visible)
    errors = [f"{r['n']}: {r['error']}" for r in rows if r["error"]]
    if not rows or errors:
        return RedirectResponse(_url(key, err="; ".join(errors) or _("No tasks given")) + anchor, 303)
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            project = projects.get(s, key)
            target = None if sprint == NO_SPRINT else sprints.get(
                s, int(sprint) if sprint.isdigit() else -1, project, for_update=True)
            created = sprints.batch_create(s, me, target, [{"title": r["title"], "assigned_to": r["assigned_to"],
                                                            "prio": r["prio"], "reviewer": r["reviewer"]}
                                                           for r in rows], project=project)
            audit(me.name, "sprint_batch", project=key, sprint=target.id if target else None, count=len(created),
                  via="planning")
    except AppError as e:
        return RedirectResponse(_url(key, err=e.message) + anchor, 303)
    msg = ngettext("%(num)d draft created", "%(num)d drafts created", len(created))
    return RedirectResponse(_url(key, msg=msg) + anchor, 303)
