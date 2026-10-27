"""UI: sprint editor – create or edit a planned sprint together with its task headings and details on one page.

The page keeps the task list as JSON in the hidden field `entries` (maintained by `static/sprint_editor.js`);
saving hands it to `sprints.save_plan` in one transaction, "save and release" adds `sprints.release`.
"""
import json
from datetime import date
from types import SimpleNamespace
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth import UiUser, csrf_protect
from app.errors import AppError, Forbidden
from app.i18n import gettext as _
from app.i18n import ngettext
from app.logging_setup import audit
from app.models import TASK_STATUSES, Actor, Project
from app.services import access, model_choice, projects, reviews, sprints
from app.services.state_machine import missing_definition
from app.web.common import field_labels, render, status_label, ui_actor, web_session
from app.web.tasks_ui import agent_choices, require_user

router = APIRouter()
POST = [Depends(csrf_protect)]
MAX_ENTRIES = 200
TEXT_FIELDS = ("title", "description", "acceptance_criteria", "assigned_to", "due_date")
MODEL_FIELDS = ("model_tier", "model", "reviewer")  # Only when sent: a missing key keeps the task's value


def _date(value: str, n: int | None = None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError as e:
        msg = _("Date in the format YYYY-MM-DD")
        raise AppError(_("Entry %(n)d: %(error)s", n=n, error=msg) if n else msg) from e


def parse_entries(raw: str) -> list[dict]:
    """The editor's `entries` field: a JSON list of task entries in display order (see `sprints.save_plan`).
    Returns normalised entries (texts as strings, `due_date` still as text); invalid data raises AppError."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        data = None
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        raise AppError(_("The task list could not be read – reload the sprint editor"))
    if len(data) > MAX_ENTRIES:
        raise AppError(_("At most %(max)d tasks per sprint in the editor", max=MAX_ENTRIES))
    out = []
    for e in data:
        prio = e.get("prio")
        entry = {f: str(e.get(f) or "") for f in TEXT_FIELDS}
        entry.update(task_nr=str(e.get("task_nr") or "") or None, status=str(e.get("status") or "draft"),
                     row_version=e["row_version"] if isinstance(e.get("row_version"), int) else None,
                     prio=int(prio) if str(prio).isdigit() else 3)
        entry.update({f: str(e.get(f) or "") for f in MODEL_FIELDS if f in e})
        out.append(entry)
    return out


def parse_removed(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise AppError(_("The task list could not be read – reload the sprint editor"))
    return [str(x) for x in data if x]


def _entry(task, names: dict[int, str]) -> dict:
    return {"task_nr": task.task_nr, "row_version": task.row_version, "status": task.status, "title": task.title,
            "description": task.description or "", "acceptance_criteria": task.acceptance_criteria or "",
            "assigned_to": names.get(task.assigned_to, "") if task.assigned_to else "", "prio": task.prio,
            "due_date": task.due_date.isoformat() if task.due_date else "", "model_tier": task.model_tier or "",
            "model": task.model or "", "reviewer": names.get(task.reviewer_id, "none") if task.reviewer_id else "none"}


def _missing(entry: dict) -> list[str]:
    return missing_definition(SimpleNamespace(description=entry["description"],
                                              acceptance_criteria=entry["acceptance_criteria"]))


def _editor(request: Request, user: UiUser, key: str | None, sprint_id: int | None, status_code: int = 200, *,
            form: dict | None = None, entries: list[dict] | None = None, removed: list[str] | None = None,
            error: str | None = None):
    """Render the editor: from the database, or (after a failed save) with the submitted values."""
    with web_session() as s:
        me = ui_actor(s, user)
        sprint = info = None
        no_own_projects = False
        if sprint_id is not None:
            project = projects.get(s, key)
            sprint = sprints.get(s, sprint_id, project)
            sprints.require_plan(project, me)
            if sprint.status != "planned" and form is None:
                msg = _("Only planned sprints can be edited in the sprint editor (status: '%(status)s')",
                        status=sprint.status)
                return RedirectResponse(f"/projects/{project.project_key}/sprints/{sprint.id}?err={quote_plus(msg)}",
                                        303)
            info = sprints.info(s, sprint)
            choices = [project]
        else:
            choices = [p for p in s.scalars(select(Project).where(Project.status != "archived")
                                            .order_by(Project.project_key)) if sprints.can_plan(p, me)]
            if not choices:
                raise Forbidden(_("Only admins, orchestrators and project owners can plan sprints"))
            if access.own_projects_only(me):  # Profile switch "Show all projects" off: own projects only.
                wanted = request.query_params.get("project", "").upper() if form is None else form.get("project")
                choices = [p for p in choices if me.id in p.owner_ids or p.project_key == wanted]
                no_own_projects = not choices
        if form is None:
            form = ({"name": info["name"], "goal": info["goal"] or "", "start_date": info["start_date"] or "",
                     "end_date": info["end_date"] or ""} if info else
                    {"project": request.query_params.get("project", "").upper(), "name": "", "goal": "",
                     "start_date": "", "end_date": ""})
        if entries is None:
            entries = []
            if sprint is not None:
                rows = [t for t in sprints.ordered_tasks(s, sprint) if t.status != "cancelled"]
                names = dict(s.execute(select(Actor.id, Actor.name).where(Actor.id.in_(
                    {t.assigned_to for t in rows if t.assigned_to} | {t.reviewer_id for t in rows if t.reviewer_id}))
                ).all())
                entries = [_entry(t, names) for t in rows]
        projects_ = [(p.project_key, p.name) for p in choices]
        manage_keys = [p.project_key for p in choices if access.can_manage_project(p, me)]
        agents = agent_choices(s, me, [e.get("assigned_to") for e in entries])  # Assigned names stay choosable.
        reviewer_choices = reviews.candidates(s, me)
        shown = {r["name"] for r in reviewer_choices}
        reviewer_choices += [{"name": n, "kind": "agent"} for n in dict.fromkeys(e.get("reviewer") for e in entries)
                             if n and n != "none" and n not in shown]
        agent_models = {a.name: {"allowed": model_choice.allowed(a), "applies": model_choice.applies(a)}
                        for a in s.scalars(select(Actor).where(Actor.is_active.is_(True),
                                                               Actor.actor_type == "agent"))}
    items = [{**e, "missing": field_labels(_missing(e)) if e["status"] == "draft" else ""} for e in entries]
    return render(request, "sprint_editor.html", status_code, sprint=info, key=key, form=form, items=items,
                  entries_json=json.dumps(entries, ensure_ascii=False), removed_json=json.dumps(removed or []), projects=projects_,
                  manage_keys=manage_keys, agents=agents, agent_models=agent_models, error=error,
                  reviewers=reviewer_choices,
                  owner_hint=no_own_projects,
                  status_labels={st: status_label(st) for st in TASK_STATUSES})


def _save(request: Request, user: UiUser, key: str | None, sprint_id: int | None, form: dict, entries_raw: str,
          removed_raw: str, action: str):
    entries, removed = [], []
    try:
        entries, removed = parse_entries(entries_raw), parse_removed(removed_raw)
        plan = [{**e, "due_date": _date(e["due_date"], n), "assigned_to": e["assigned_to"] or None}
                for n, e in enumerate(entries, 1)]
        with web_session() as s:
            me = ui_actor(s, user)
            project = projects.get(s, key or form.get("project") or "")
            sprint = sprints.get(s, sprint_id, project, for_update=True) if sprint_id is not None else None
            result = sprints.save_plan(s, me, project, sprint, name=form["name"], goal=form["goal"],
                                       start_date=_date(form["start_date"]), end_date=_date(form["end_date"]),
                                       entries=plan, removed=removed)
            saved_key, saved_id = project.project_key, result["sprint"].id
            audit(me.name, "sprint_planned", project=saved_key, sprint=saved_id, drafts_created=len(result["created"]),
                  drafts_updated=len(result["updated"]), drafts_cancelled=len(result["cancelled"]), via="editor")
            released = sprints.release(s, me, result["sprint"]) if action == "release" else None
            if released is not None:
                audit(me.name, "sprint_released", project=saved_key, sprint=saved_id,
                      released=len(released["released"]), blocked=len(released["blocked"]), via="editor")
    except AppError as e:
        return _editor(request, user, key, sprint_id,
                       422 if e.status < 500 else e.status, form=form, entries=entries, removed=removed,
                       error=e.message)
    if released is not None:
        msg = ngettext("Sprint released: %(num)d task released", "Sprint released: %(num)d tasks released",
                       len(released["released"]))
        if released["blocked"]:
            msg += " · " + ngettext("%(num)d stays a draft (see below)", "%(num)d stay drafts (see below)",
                                    len(released["blocked"]))
        return RedirectResponse(f"/projects/{saved_key}/sprints/{saved_id}?msg={quote_plus(msg)}", 303)
    parts = [_("Sprint saved")]
    if result["created"]:
        parts.append(ngettext("%(num)d draft created", "%(num)d drafts created", len(result["created"])))
    if result["updated"]:
        parts.append(ngettext("%(num)d draft updated", "%(num)d drafts updated", len(result["updated"])))
    if result["cancelled"]:
        parts.append(ngettext("%(num)d draft cancelled", "%(num)d drafts cancelled", len(result["cancelled"])))
    msg = " · ".join(parts)
    return RedirectResponse(f"/projects/{saved_key}/sprints/{saved_id}/editor?msg={quote_plus(msg)}", 303)


@router.get("/sprints/new")
def new_sprint(request: Request, user: UiUser = Depends(require_user)):
    return _editor(request, user, None, None)


@router.post("/sprints/new", dependencies=POST)
def create_sprint(request: Request, user: UiUser = Depends(require_user), project: str = Form(""),
                  name: str = Form(""), goal: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
                  entries: str = Form("[]"), removed: str = Form("[]"), action: str = Form("save")):
    form = {"project": project, "name": name, "goal": goal, "start_date": start_date, "end_date": end_date}
    return _save(request, user, None, None, form, entries, removed, action)


@router.get("/projects/{key}/sprints/{sprint_id}/editor")
def edit_sprint(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user)):
    return _editor(request, user, key, sprint_id)


@router.post("/projects/{key}/sprints/{sprint_id}/editor", dependencies=POST)
def save_sprint(request: Request, key: str, sprint_id: int, user: UiUser = Depends(require_user),
                name: str = Form(""), goal: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
                entries: str = Form("[]"), removed: str = Form("[]"), action: str = Form("save")):
    form = {"name": name, "goal": goal, "start_date": start_date, "end_date": end_date}
    return _save(request, user, key, sprint_id, form, entries, removed, action)
