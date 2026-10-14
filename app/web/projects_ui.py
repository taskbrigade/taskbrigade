"""UI: projects (spec section 11.5)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from urllib.parse import quote_plus

from app.auth import UiUser, csrf_protect
from app.errors import AppError
from app.i18n import gettext as _
from app.logging_setup import audit
from sqlalchemy import select

from app.models import Actor
from app.services import access, members, projects, reviews, settings_store, sprints
from app.services.gitea import GiteaError
from app.web.common import render, ui_actor, ui_actor_or_none, web_session
from app.web.tasks_ui import require_user

router = APIRouter(prefix="/projects")
POST = [Depends(csrf_protect)]


@router.get("")
def list_projects(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor_or_none(s, user)
        items = projects.list_all(s)
        own_only = access.own_projects_only(me)
        if own_only:  # Profile switch "Show all projects" off.
            items = [p for p in items if p.owner_id == me.id]
        counts = projects.task_counts(s, [p.id for p in items])
        owners = {a.id: a for a in s.scalars(select(Actor).where(Actor.actor_type == "human"))}
    return render(request, "projects.html", items=items, counts=counts, owners=owners, own_only=own_only,
                  can_create=user.is_admin or user.role == "member")


def _humans(s) -> list[str]:
    return list(s.scalars(select(Actor.name).where(Actor.actor_type == "human", Actor.is_active.is_(True))
                          .order_by(Actor.name)))


@router.get("/new")
def new_project(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        default_owner = settings_store.get(s, "gitea_default_owner") or ""
        humans = _humans(s)
        me = ui_actor(s, user)
    return render(request, "project_new.html", form={"gitea_mode": "create", "gitea_owner": default_owner,
                                                     "privacy": "local_only", "status": "active",
                                                     "owner": me.name if me else ""}, humans=humans)


@router.get("/repos")
def repo_options(request: Request, user: UiUser = Depends(require_user), q: str = ""):
    """HTMX: <option> list of Gitea repos for linking an existing repository."""
    with web_session() as s:
        try:
            repos = settings_store.gitea_client(s).search_repos(q)
        except GiteaError as e:
            return HTMLResponse(f'<option value="">{escape(_("Gitea: %(error)s", error=str(e)))}</option>')
    opts = "".join(f'<option value="{escape(r.owner)}/{escape(r.name)}">{escape(r.owner)}/{escape(r.name)}</option>'
                   for r in repos)
    return HTMLResponse(f'<option value="">{escape(_("– choose a repo –"))}</option>' + opts)


@router.post("/new", dependencies=POST)
def create_project(request: Request, user: UiUser = Depends(require_user), key: str = Form(...),
                   name: str = Form(...), description: str = Form(""), status: str = Form("idea"),
                   privacy: str = Form("local_only"), gitea_mode: str = Form("none"), gitea_owner: str = Form(""),
                   gitea_repo: str = Form(""), linked_repo: str = Form(""), add_missing_files: bool = Form(False),
                   protect_branch: bool = Form(False), owner: str = Form("")):
    form = dict(key=key, name=name, description=description, status=status, privacy=privacy,
                gitea_mode=gitea_mode, gitea_owner=gitea_owner, gitea_repo=gitea_repo, owner=owner)
    if gitea_mode == "link" and linked_repo:
        gitea_owner, _sep, gitea_repo = linked_repo.partition("/")
    if gitea_mode == "none":
        status = "idea" if status == "active" else status
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            project, setup = projects.create(
                s, me, key=key, name=name, description=description, status=status, privacy=privacy,
                gitea_mode=gitea_mode, gitea_owner=gitea_owner or None, gitea_repo=gitea_repo or None,
                add_missing_files=add_missing_files if gitea_mode == "link" else True,
                protect_branch=protect_branch if gitea_mode == "link" else None, owner=owner or None)
            pkey = project.project_key
            audit(me.name, "project_created", project=pkey, gitea_mode=gitea_mode, via="ui")
    except AppError as e:
        with web_session() as s:
            humans = _humans(s)
        return render(request, "project_new.html", 422, form=form, error=e.message, humans=humans)
    msg = _("Project created") if setup is None or setup.ok else \
        _("Project created, Gitea setup incomplete: %(error)s", error=setup.error)
    return RedirectResponse(f"/projects/{pkey}?msg={quote_plus(msg)}", 303)


def _detail(request: Request, user: UiUser, key: str, status_code: int = 200, **ctx):
    with web_session() as s:
        project = projects.get(s, key)
        me = ui_actor(s, user)
        can_manage = access.can_manage_project(project, me)
        can_plan = sprints.can_plan(project, me)
        sprint_rows = sprints.list_for_project(s, project)
        sprint_counts = sprints.task_counts(s, [x.id for x in sprint_rows])
        humans = _humans(s)
        owner = s.get(Actor, project.owner_id)
        counts = projects.task_counts(s, [project.id]).get(project.id, {})
        member_rows = members.list_for_project(s, project)
        try:
            missing = projects.missing_files(s, project)
            gitea_error = None
        except GiteaError as e:
            missing, gitea_error = [], str(e)
        info = projects.info(s, project)
        reviewer_choices = reviews.candidates(s, me)  # The current default stays visible (template).
    return render(request, "project_detail.html", status_code, p=project, info=info, counts=counts,
                  reviewers=reviewer_choices,
                  members=member_rows, missing=missing, gitea_error=gitea_error, can_manage=can_manage,
                  humans=humans, owner=owner, sprints=sprint_rows, sprint_counts=sprint_counts, can_plan=can_plan,
                  **ctx)


@router.get("/{key}")
def project_detail(request: Request, key: str, user: UiUser = Depends(require_user)):
    return _detail(request, user, key)


def _run(request: Request, user: UiUser, key: str, fn, ok: str):
    try:
        with web_session() as s:
            result = fn(s, ui_actor(s, user))
    except AppError as e:
        return _detail(request, user, key, 422, error=e.message)
    if result is not None and not result.ok:
        err = _("Gitea setup incomplete: %(error)s", error=result.error)
        return RedirectResponse(f"/projects/{key}?err={quote_plus(err)}", 303)
    return RedirectResponse(f"/projects/{key}?msg={quote_plus(ok)}", 303)


@router.post("/{key}/edit", dependencies=POST)
def edit_project(request: Request, key: str, user: UiUser = Depends(require_user), name: str = Form(...),
                 description: str = Form(""), status: str = Form(...), privacy: str = Form(...),
                 owner: str = Form(""), serialize_reviews: bool = Form(False), default_reviewer: str = Form("")):
    def fn(s, me):
        changes = {"name": name, "description": description or None, "status": status, "privacy": privacy,
                   "serialize_reviews": serialize_reviews}
        if owner:
            changes["owner"] = owner
        if default_reviewer:  # `none` removes it; empty/missing keeps it (FastAPI cannot tell them apart).
            changes["default_reviewer"] = default_reviewer
        changed = projects.update(s, me, projects.get(s, key), changes)
        audit(me.name, "project_updated", project=key, fields=changed, via="ui")
    return _run(request, user, key, fn, _("Saved"))


@router.post("/{key}/gitea", dependencies=POST)
def project_gitea(request: Request, key: str, user: UiUser = Depends(require_user), mode: str = Form(...),
                  gitea_owner: str = Form(""), gitea_repo: str = Form(""), activate: bool = Form(False),
                  protect_branch: bool = Form(False)):
    """Activate an idea (create repo and set active) or repeat setup (missing files and protection)."""
    def fn(s, me):
        project = projects.get(s, key)
        access.require_manage_project(project, me)
        result = projects.setup_gitea(s, project, mode, gitea_owner or project.gitea_owner,
                                      gitea_repo or project.gitea_repo, True,
                                      protect_branch if mode == "link" else None)
        if activate:
            projects.update(s, me, project, {"status": "active"})
        audit(me.name, "project_gitea", project=key, mode=mode, ok=result.ok, via="ui")
        return result
    return _run(request, user, key, fn, _("Gitea setup done"))
