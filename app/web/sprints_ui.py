"""UI: sprints of a project – sprint page with release preview, close dialog and batch creation.

The project page lists the sprints (`projects_ui`); all rules live in `app/services/sprints.py`.
"""
from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth import UiUser, csrf_protect
from app.errors import AppError
from app.i18n import gettext as _
from app.i18n import ngettext
from app.logging_setup import audit
from app.models import Actor
from app.services import access, projects, sprints
from app.services import tasks as task_svc
from app.services.state_machine import END_STATUSES, missing_definition
from app.web.common import render, ui_actor, web_session
from app.web.tasks_ui import agent_choices, require_user

router = APIRouter(prefix="/projects/{key}/sprints")
POST = [Depends(csrf_protect)]
BATCH_FORMAT = "Title | Agent | Prio | Reviewer"


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError as e:
        raise AppError(_("Date in the format YYYY-MM-DD")) from e


def parse_batch(text: str, agents: set[str], visible: set[str] | None = None) -> list[dict]:
    """One task per non-empty line: `Title` or `Title | Agent | Prio | Reviewer` (agent, prio and reviewer may be
    left empty; an empty reviewer means the project default, `none` nobody). Returns one row per entry with `n`,
    `title`, `assigned_to`, `prio`, `reviewer` and `error` (None when valid). `visible`: the names the user may
    choose (own and public agents, humans); other known agents are refused like the server does."""
    rows = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = [p.strip() for p in raw.split("|")]
        title, agent, prio, reviewer = (parts + ["", "", ""])[:4]
        error = None
        if len(parts) > 4:
            error = _("Too many fields – use %(format)s", format=BATCH_FORMAT)
        elif not title:
            error = _("Title is missing")
        elif len(title) > 200:
            error = _("Title longer than 200 characters")
        elif agent and agent not in agents:
            error = _("Unknown actor '%(name)s'", name=agent)
        elif prio and not (prio.isdigit() and 1 <= int(prio) <= 5):
            error = _("Prio must be between 1 and 5")
        elif reviewer and reviewer.lower() != "none" and reviewer not in agents:
            error = _("Unknown actor '%(name)s'", name=reviewer)
        elif visible is not None and (hidden := next((n for n in (agent, reviewer) if n in agents
                                                      and n not in visible), None)):
            error = _("Agent '%(name)s' is neither public nor yours (nor your owner's)", name=hidden)
        rows.append({"n": len(rows) + 1, "title": title, "assigned_to": agent or None,
                     "prio": int(prio) if prio.isdigit() else 3, "reviewer": reviewer or None, "error": error})
    return rows


def _page(request: Request, user: UiUser, key: str, sprint_id: int, status_code: int = 200, **ctx):
    with web_session() as s:
        me = ui_actor(s, user)
        project = projects.get(s, key)
        sprint = sprints.get(s, sprint_id, project)
        rows = sprints.ordered_tasks(s, sprint)
        names = task_svc._names(s, {t.assigned_to for t in rows})
        late = sprints.late_dependencies(s, sprint, [t.id for t in rows if t.status not in END_STATUSES])
        tasks = [{**task_svc.summary(t, project.project_key, names),
                  "missing": missing_definition(t) if t.status == "draft" else [],
                  "waits_for": late.get(t.id, [])} for t in rows]
        info = sprints.info(s, sprint)
        targets = [x for x in sprints.list_for_project(s, project, ["planned", "released"]) if x.id != sprint.id]
        preview = sprints.release_preview(s, sprint) if sprint.status != "closed" else None
        agents = agent_choices(s, me, [t["assigned_to"] for t in tasks])
        can_plan = sprints.can_plan(project, me)
        can_manage = access.can_manage_project(project, me)
        restorable = len(sprints.moved_out_at_close(s, sprint)[0]) if sprint.status == "closed" else 0
    counts = info["task_counts"]
    total = sum(counts.values())
    return render(request, "sprint.html", status_code, p=project, sprint=info, tasks=tasks, targets=targets,
                  preview=preview, agents=agents, can_plan=can_plan, can_manage=can_manage, total=total,
                  restorable=restorable,
                  done=counts.get("done", 0), cancelled=counts.get("cancelled", 0),
                  open_count=total - counts.get("done", 0) - counts.get("cancelled", 0),
                  batch_format=BATCH_FORMAT, **ctx)


def _planning_url(key: str, status: str | None, msg: str | None = None, err: str | None = None) -> str:
    """Back to the planning board showing the acted sprint: the default filter "all" (closed hidden by default),
    the toggle on for a closed sprint so it stays in view."""
    url = f"/planning?project={quote_plus(key.upper())}&filter=all&closed={1 if status == 'closed' else 0}"
    if msg:
        return f"{url}&msg={quote_plus(msg)}"
    return f"{url}&err={quote_plus(err)}" if err else url


def _url(key: str, sprint_id: int, msg: str | None = None, err: str | None = None, back: str = "",
         status: str | None = None) -> str:
    """Where an action redirects to: the sprint page, or the planning board when the form sent `back=planning`."""
    if back == "planning":
        return _planning_url(key, status, msg, err)
    url = f"/projects/{key}/sprints/{sprint_id}"
    if msg:
        return f"{url}?msg={quote_plus(msg)}"
    return f"{url}?err={quote_plus(err)}" if err else url


@router.post("/new", dependencies=POST)
def create_sprint(key: str, user: UiUser = Depends(require_user), name: str = Form(...),
                  goal: str = Form(""), start_date: str = Form(""), end_date: str = Form(""), back: str = Form("")):
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            sprint = sprints.create(s, me, projects.get(s, key), name=name, goal=goal,
                                    start_date=_date(start_date), end_date=_date(end_date))
            sprint_id = sprint.id
            audit(me.name, "sprint_created", project=key.upper(), sprint=sprint_id, via="ui")
    except AppError as e:
        if back == "planning":
            return RedirectResponse(_url(key, 0, err=e.message, back=back), 303)
        return RedirectResponse(f"/projects/{key}?err={quote_plus(e.message)}", 303)
    if back == "planning":  # A new sprint is planned: the board shows it right away under the default filter.
        return RedirectResponse(_planning_url(key, "planned", msg=_("Sprint created")), 303)
    return RedirectResponse(_url(key, sprint_id, msg=_("Sprint created"), back=back), 303)


@router.get("/{sprint_id}")
def sprint_page(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user)):
    return _page(request, user, key, sprint_id)


def _run(request: Request, user: UiUser, key: str, sprint_id: int, fn, back: str = ""):
    """Run an action on the locked sprint; redirect with its message or show the page with the error (from the
    planning board: back there with the error)."""
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            sprint = sprints.get(s, sprint_id, projects.get(s, key), for_update=True)
            message = fn(s, me, sprint)
            status = sprint.status
    except AppError as e:
        if back == "planning":
            return RedirectResponse(_url(key, sprint_id, err=e.message, back=back), 303)
        return _page(request, user, key, sprint_id, 422 if e.status < 500 else e.status, error=e.message)
    return RedirectResponse(_url(key, sprint_id, msg=message, back=back, status=status), 303)


@router.post("/{sprint_id}/edit", dependencies=POST)
def edit_sprint(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user),
                name: str = Form(...), goal: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
                back: str = Form("")):
    def fn(s, me, sprint):
        sprints.update(s, me, sprint, {"name": name, "goal": goal, "start_date": _date(start_date),
                                       "end_date": _date(end_date)})
        return _("Saved")
    return _run(request, user, key, sprint_id, fn, back)


@router.post("/{sprint_id}/release", dependencies=POST)
def release_sprint(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user),
                   back: str = Form("")):
    def fn(s, me, sprint):
        result = sprints.release(s, me, sprint)
        audit(me.name, "sprint_released", project=key.upper(), sprint=sprint.id,
              released=len(result["released"]), blocked=len(result["blocked"]), via="ui")
        msg = ngettext("Sprint released: %(num)d task released", "Sprint released: %(num)d tasks released",
                       len(result["released"]))
        if result["blocked"]:
            msg += " · " + ngettext("%(num)d stays a draft (see below)", "%(num)d stay drafts (see below)",
                                    len(result["blocked"]))
        return msg
    return _run(request, user, key, sprint_id, fn, back)


@router.post("/{sprint_id}/close", dependencies=POST)
def close_sprint(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user),
                 open_tasks: str = Form("detach"), move_to: str = Form(""), back: str = Form("")):
    def fn(s, me, sprint):
        move = open_tasks == "move"
        if move and not move_to.isdigit():
            raise AppError(_("Choose the sprint the open tasks move to"))
        result = sprints.close(s, me, sprint, int(move_to) if move else None, detach=not move)
        audit(me.name, "sprint_closed", project=key.upper(), sprint=sprint.id, moved_to=result["moved_to"],
              via="ui")
        if result["moved"]:
            target = sprints.get(s, result["moved_to"])
            return ngettext("Sprint closed: %(num)d open task moved to '%(sprint)s'",
                            "Sprint closed: %(num)d open tasks moved to '%(sprint)s'", len(result["moved"]),
                            sprint=target.name)
        if result["detached"]:
            return ngettext("Sprint closed: %(num)d open task detached from the sprint",
                            "Sprint closed: %(num)d open tasks detached from the sprint", len(result["detached"]))
        return _("Sprint closed")
    return _run(request, user, key, sprint_id, fn, back)


@router.post("/{sprint_id}/reopen", dependencies=POST)
def reopen_sprint(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user),
                  restore_tasks: str = Form("0"), back: str = Form("")):
    def fn(s, me, sprint):
        result = sprints.reopen(s, me, sprint, restore_tasks == "1")
        audit(me.name, "sprint_reopened", project=key.upper(), sprint=sprint.id, restored=len(result["restored"]),
              skipped=len(result["skipped"]), via="ui")
        msg = ngettext("Sprint reopened: %(num)d task moved back", "Sprint reopened: %(num)d tasks moved back",
                       len(result["restored"]))
        if result["skipped"]:
            msg += " · " + ngettext("%(num)d skipped (moved elsewhere later or finished)",
                                    "%(num)d skipped (moved elsewhere later or finished)",
                                    len(result["skipped"]))
        return msg
    return _run(request, user, key, sprint_id, fn, back)


@router.post("/{sprint_id}/batch", dependencies=POST)
def batch_tasks(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user),
                lines: str = Form(""), step: str = Form("preview")):
    """Batch creation in two steps: `preview` shows the parsed lines, `create` creates them as drafts."""
    with web_session() as s:
        agents = set(s.scalars(select(Actor.name).where(Actor.is_active.is_(True))))
        me = ui_actor(s, user)  # Admins may assign any agent (the choices only hide them by default).
        visible = None if me is None or me.role == "admin" else set(agent_choices(s, me))
    rows = parse_batch(lines, agents, visible)
    if not rows:
        return _page(request, user, key, sprint_id, 422, batch_lines=lines, batch_rows=[],
                     batch_error=_("No tasks given"))
    if step != "create" or any(r["error"] for r in rows):
        return _page(request, user, key, sprint_id, 422 if step == "create" else 200, batch_lines=lines,
                     batch_rows=rows, batch_error=_("Fix the marked lines first") if step == "create" else None)

    try:
        with web_session() as s:
            me = ui_actor(s, user)
            sprint = sprints.get(s, sprint_id, projects.get(s, key), for_update=True)
            created = sprints.batch_create(s, me, sprint, [{"title": r["title"], "assigned_to": r["assigned_to"],
                                                            "prio": r["prio"], "reviewer": r["reviewer"]}
                                                           for r in rows])
            audit(me.name, "sprint_batch", project=key.upper(), sprint=sprint.id, count=len(created), via="ui")
    except AppError as e:
        return _page(request, user, key, sprint_id, 422 if e.status < 500 else e.status, batch_lines=lines,
                     batch_rows=rows, batch_error=e.message)
    msg = ngettext("%(num)d draft created – complete each one on its task page",
                   "%(num)d drafts created – complete each one on its task page", len(created))
    return RedirectResponse(_url(key, sprint_id, msg=msg), 303)
