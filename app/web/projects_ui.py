"""UI: projects (spec section 11.5)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from urllib.parse import quote_plus

from app.auth import UiUser, csrf_protect
from app.errors import AppError
from app.i18n import gettext as _, ngettext
from app.logging_setup import audit
from sqlalchemy import select

from app.models import PROJECT_STATUSES, Actor
from app.services import access, members, projects, result_mail, reviews, settings_store, sprints, zones
from app.services.gitea import GiteaError
from app.web.common import quick_capture_projects, render, ui_actor, ui_actor_or_none, web_session
from app.web.tasks_ui import require_user

router = APIRouter(prefix="/projects")
POST = [Depends(csrf_protect)]

# Projects overview: view switch, status and sort choices. Sorting happens in Python (the list is small,
# counts and last activity come from the aggregated overview) so every sort key matches how the view is rendered.
# The list is always the default; the card view applies only while `?view=cards` is in the URL. The old
# `tm_projects_view` cookie is no longer written and is deleted when it still arrives, so a stale
# value cannot force the card view.
VIEW_COOKIE = "tm_projects_view"
VIEWS = ("list", "cards")
STATUS_CHOICES = (("active", ("active",)), ("paused", ("paused",)), ("archived", ("archived",)),
                  ("all", PROJECT_STATUSES))
SORT_CHOICES = ("activity", "key", "open")


def _choice(value: str | None, allowed, default):
    return value if value in allowed else default


def _overview(request: Request, me: Actor | None):
    """Rows, numbers and filter state of the projects overview (one query per fact, no per-project queries)."""
    qp = request.query_params
    own_only = access.own_projects_only(me)
    # With the profile switch "Show all projects" off the view stays on own projects, whatever the URL says.
    mine = "mine" if own_only else _choice(qp.get("mine"), ("mine", "all"), "all")
    status = _choice(qp.get("status"), [key for key, _statuses in STATUS_CHOICES], "active")
    sort = _choice(qp.get("sort"), SORT_CHOICES, "activity")
    q = (qp.get("q") or "").strip()
    view = _choice(qp.get("view"), VIEWS, "list")
    with web_session() as s:
        every = projects.list_all(s)
        owner_hint = own_only and not any(p.owner_id == me.id for p in every)
        # Scope first (mine/all), then the status, so the header number describes exactly the chosen scope.
        scoped = [p for p in every if me is not None and p.owner_id == me.id] if mine == "mine" else every
        items = [p for p in scoped if p.status in dict(STATUS_CHOICES)[status]]
        scope_total = sum(1 for p in scoped if p.status == "active")
        needle = q.casefold()
        if needle:
            items = [p for p in items if needle in _search_text(p)]
        numbers = projects.overview(s, [p.id for p in items])
        owners = {a.id: a for a in s.scalars(select(Actor).where(Actor.actor_type == "human"))}
        mixed = zones.mixed_projects(s, [p.id for p in items])
    # "Current" project of the quick capture form: the first project of its (alphabetical) choice list. Read
    # outside the block above: `quick_capture_projects` opens its own session, which must not nest.
    capture = [key for key, _name in quick_capture_projects(getattr(me, "id", None))]
    current = capture[0] if capture else None

    # Order for the rendered list: the sort field of the toolbar. `activity` puts the newest first and projects
    # without tasks last, `key` is the project key, `open` the number of open tasks (tie-break: key).
    def order(p):
        if sort == "key":
            return (p.project_key,)
        if sort == "open":
            return (open_count(numbers[p.id]["counts"]), p.project_key)
        last = numbers[p.id]["last_activity"]
        return (last is None, -(last.timestamp() if last else 0), p.project_key)

    items.sort(key=order)
    current_row = next((p for p in items if p.project_key == current), None)
    # The cards view leads with the "Current" project (template renders it before the rest); the list keeps the
    # sort above. Reorder only for the cards view – reordering here would override the chosen list sort.
    if current_row is not None and view == "cards":
        items = [current_row, *[p for p in items if p is not current_row]]
    return {"items": items, "current_ids": {current_row.id} if current_row else set(),
            "numbers": numbers, "owners": owners, "mixed": mixed, "q": q, "status": status, "sort": sort,
            "view": view, "mine": mine, "own_only": own_only, "owner_hint": owner_hint,
            "status_choices": STATUS_CHOICES,
            "sort_choices": SORT_CHOICES, "open_count": open_count, "active_total": scope_total,
            "subtitle": _subtitle(me, scope_total, mine)}


def open_count(counts: dict) -> int:
    """Open tasks of a project: everything that is neither finished nor cancelled (draft counts as open)."""
    return sum(n for status, n in counts.items() if status not in ("done", "cancelled"))


def _subtitle(me: Actor | None, total: int, mine: str) -> str:
    """Header subtitle: "<n> active projects · owner <user>" (the owner is only known in the mine scope)."""
    text = ngettext("%(num)d active project", "%(num)d active projects", total, num=total)
    if mine == "mine" and me is not None:
        text = _("%(projects)s · owner %(owner)s", projects=text, owner=me.name)
    return text


def _search_text(project) -> str:
    """Searchable text of a project: key, name and repo (owner/repo) – the three fields of the search box."""
    return " ".join((project.project_key, project.name,
                     f"{project.gitea_owner or ''}/{project.gitea_repo or ''}")).casefold()


@router.get("")
def list_projects(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor_or_none(s, user)
    ctx = _overview(request, me)
    ctx["can_create"] = user.is_admin or user.role == "member"
    response = render(request, "projects.html", **ctx)
    if VIEW_COOKIE in request.cookies:
        response.delete_cookie(VIEW_COOKIE, httponly=True, samesite="lax")
    return response


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
        mixed = bool(zones.mixed_projects(s, [project.id]))
        member_rows = members.list_for_project(s, project)
        try:
            missing = projects.missing_files(s, project)
            gitea_error = None
        except GiteaError as e:
            missing, gitea_error = [], str(e)
        info = projects.info(s, project)
        reviewer_choices = reviews.candidates(s, me)  # The current default stays visible (template).
        mail_recipients = result_mail.recipients(s, project)
        mail_limit = settings_store.get(s, "mail_daily_limit_per_project")
        four_eyes_default = settings_store.get_bool(s, "data_four_eyes")
    return render(request, "project_detail.html", status_code, p=project, info=info, counts=counts, mixed=mixed,
                  reviewers=reviewer_choices, mail_recipients=mail_recipients, mail_limit=mail_limit,
                  four_eyes_default=four_eyes_default,
                  mail_modes=result_mail.MODES,
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


FOUR_EYES = {"default": None, "on": True, "off": False}


@router.post("/{key}/edit", dependencies=POST)
def edit_project(request: Request, key: str, user: UiUser = Depends(require_user), name: str = Form(...),
                 description: str = Form(""), status: str = Form(...), privacy: str = Form(...),
                 owner: str = Form(""), serialize_reviews: bool = Form(False), default_reviewer: str = Form(""),
                 data_four_eyes: str = Form("")):
    def fn(s, me):
        changes = {"name": name, "description": description or None, "status": status, "privacy": privacy,
                   "serialize_reviews": serialize_reviews}
        if data_four_eyes in FOUR_EYES:  # Empty/missing keeps the stored value (FastAPI empty-form rule)
            changes["data_four_eyes"] = FOUR_EYES[data_four_eyes]
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


@router.post("/{key}/mail-recipients", dependencies=POST)
def add_mail_recipient(request: Request, key: str, user: UiUser = Depends(require_user), address: str = Form(...),
                       label: str = Form(""), mode: str = Form("freigabe")):
    """Allow an address for result mails (admins and the owner); an existing address gets the new label/mode."""
    def fn(s, me):
        result_mail.add_recipient(s, me, projects.get(s, key), address, label, mode)
    return _run(request, user, key, fn, _("Recipient saved"))


@router.post("/{key}/mail-recipients/{recipient_id}/delete", dependencies=POST)
def remove_mail_recipient(request: Request, key: str, recipient_id: int, user: UiUser = Depends(require_user)):
    return _run(request, user, key, lambda s, me: result_mail.remove_recipient(s, me, projects.get(s, key),
                                                                               recipient_id),
                _("Recipient removed"))
