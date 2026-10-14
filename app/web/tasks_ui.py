"""UI: Inbox, Backlog, Kanban, Task-Detail, Quick-Capture (Spec 11)."""
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote, quote_plus, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import and_, or_, select

from app.auth import UiUser, csrf_protect, current_user, LoginRequired
from app.config import get_settings
from app.errors import AppError
from app.i18n import N_, ngettext
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import TASK_STATUSES, Actor, Project, Sprint, Task
from app.services import access, model_choice, reviews, sprints, state_machine, telegram_actions
from app.services import tasks as svc
from app.services.state_machine import InvalidTransition
from app.web.common import is_htmx, render, render_markdown, ui_actor, ui_actor_or_none, web_session

router = APIRouter()
POST = [Depends(csrf_protect)]
KANBAN_STATUSES = ("draft", "defined", "in_progress", "questions", "aborted", "for_review", "changes_requested", "done")
# Kanban columns: (key, statuses, drop target). Questions and aborted tasks both wait for a human decision and share
# one column; dropping a card there always means "abort" (reason prompt or override as before), never "ask".
KANBAN_COLUMNS = (("draft", ("draft",), "draft"), ("defined", ("defined",), "defined"),
                  ("in_progress", ("in_progress",), "in_progress"),
                  ("waiting", ("aborted", "questions"), "aborted"), ("for_review", ("for_review",), "for_review"),
                  ("changes_requested", ("changes_requested",), "changes_requested"), ("done", ("done",), "done"))
DEFAULT_BACKLOG_STATUS = [s for s in TASK_STATUSES if s not in ("done", "cancelled")]


def require_user(request: Request) -> UiUser:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def _filter_options(session, me: Actor | None = None, keep: str | None = None, keep_agents=()) -> dict:
    """Choices for filters and forms. With `me` and the profile switch "Show all projects" off, projects and
    sprints are limited to the projects `me` owns (plus `keep`, a project the page was explicitly opened for).
    Assignee and reviewer choices show humans and the agents `me` may choose – own and public ones, all for an admin
    with "Show all agents" (`access.agent_choice_filter`) – plus `keep_agents` (e.g. the current assignee)."""
    owners = session.scalars(select(Actor.name).join(Project, Project.owner_id == Actor.id).distinct()
                             .order_by(Actor.name))
    visible = []
    if access.own_projects_only(me):
        visible.append(Project.owner_id == me.id if not keep else
                       or_(Project.owner_id == me.id, Project.project_key == keep.strip().upper()))
    sprint_rows = [{"id": i, "name": n, "status": st, "project": key} for i, n, st, key in session.execute(
        select(Sprint.id, Sprint.name, Sprint.status, Project.project_key)
        .join(Project, Project.id == Sprint.project_id).where(*visible).order_by(Project.project_key, Sprint.id))]
    return {
        "projects": list(session.execute(select(Project.project_key, Project.name).where(*visible)
                                         .order_by(Project.project_key)).all()),
        "agents": agent_choices(session, me, keep_agents),
        "owners": list(owners),
        "own_projects_only": bool(visible),
        "sprints": sprint_rows,
        "sprint_names": {x["id"]: x["name"] for x in sprint_rows},
        "agent_models": {a.name: {"allowed": model_choice.allowed(a), "applies": model_choice.applies(a)}
                         for a in session.scalars(select(Actor).where(Actor.is_active.is_(True),
                                                                      Actor.actor_type == "agent"))},
        "reviewers": reviews.candidates(session, me),
    }


def agent_choices(session, me: Actor | None, keep=()) -> list[str]:
    """Names for assignee fields and agent filters: active humans and the agents `me` may choose, plus `keep`
    (already assigned or selected names stay visible instead of being emptied silently)."""
    keep = [k for k in keep if k]
    visible = access.agent_choice_filter(me)
    return list(session.scalars(select(Actor.name).where(
        or_(and_(Actor.is_active.is_(True), visible), Actor.name.in_(keep)) if keep else
        and_(Actor.is_active.is_(True), visible)).order_by(Actor.name)))


def _sprint_param(request: Request) -> str | None:
    """Sprint filter: empty = all, `none` = tasks without a sprint, otherwise one sprint id."""
    value = request.query_params.get("sprint") or ""
    return value if value == "none" or value.isdigit() else None


OWNER_COOKIE = "tm_owner"


def _owner_choice(request: Request) -> tuple[str, bool]:
    """Owner filter: `owner` parameter (me | all | name), else the last cookie choice; default `all` (only shown
    with the profile switch "Show all projects" on). The second value is True when the choice changed and a cookie
    should be written."""
    if "owner" in request.query_params:
        return (request.query_params.get("owner") or "all"), True
    return request.cookies.get(OWNER_COOKIE) or "all", False


def _owner_ctx(session, request: Request, me) -> dict:
    if access.own_projects_only(me):  # Profile switch off: fixed to own projects, filter hidden, cookie untouched.
        return {"owner": "me", "owner_id": me.id, "owner_changed": False, "owner_locked": True,
                "owner_hint": not access.owned_project_keys(session, me)}
    owner, changed = _owner_choice(request)
    try:
        owner_id = svc.owner_filter_id(session, me, owner)
    except AppError:  # For example, a deleted name in the cookie.
        owner, owner_id, changed = "all", None, True
    hint = owner == "me" and me is not None and not access.owned_project_keys(session, me)
    return {"owner": owner, "owner_id": owner_id, "owner_changed": changed, "owner_locked": False,
            "owner_hint": hint}


def _with_owner_cookie(response, ctx: dict):
    if ctx.get("owner_changed"):
        response.set_cookie(OWNER_COOKIE, ctx["owner"], max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


# --- Inbox --------------------------------------------------------------------------------

def _last_question(session, task_nr: str) -> str | None:
    from app.models import TaskComment

    return session.scalar(select(TaskComment.body).join(Task, Task.id == TaskComment.task_id)
                          .where(Task.task_nr == task_nr, TaskComment.comment_type == "question")
                          .order_by(TaskComment.id.desc()).limit(1))


@router.get("/")
def inbox(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor_or_none(s, user)
        octx = _owner_ctx(s, request, me) if me else {"owner": "all", "owner_id": None, "owner_changed": False,
                                                       "owner_locked": False, "owner_hint": False}
        sprint = _sprint_param(request)
        data = svc.inbox(s, octx["owner_id"], sprint)
        for t in data["questions"]:
            t["last_question"] = _last_question(s, t["task_nr"])
        telegram_actions.mark_questions_read(s, me, [t["task_nr"] for t in data["questions"]])
        for t in data["for_review"]:
            t["result_summary"] = s.scalar(select(Task.result_summary).where(Task.task_nr == t["task_nr"]))
        managed = access.owned_project_keys(s, me)
        opts = _filter_options(s, me)
    return _with_owner_cookie(render(request, "inbox.html", inbox=data, managed=managed, sprint_filter=sprint,
                                     **octx, **opts), octx)


# --- Backlog ------------------------------------------------------------------------------

def _backlog_params(request: Request) -> dict:
    qp = request.query_params
    statuses = [x for v in qp.getlist("status") for x in v.split(",") if x in TASK_STATUSES]
    prios = [int(p) for p in qp.getlist("prio") if p.isdigit() and 1 <= int(p) <= 5]
    return {"project": qp.get("project") or None, "assigned_to": qp.get("agent") or None,
            "status": statuses or ([] if qp.get("all") else DEFAULT_BACKLOG_STATUS), "prio": prios,
            "q": qp.get("q") or None, "sort": qp.get("sort") or "prio", "desc": qp.get("desc") == "1",
            "sprint": _sprint_param(request)}


@router.get("/backlog")
def backlog(request: Request, user: UiUser = Depends(require_user)):
    params = _backlog_params(request)
    with web_session() as s:
        me = ui_actor(s, user)
        octx = _owner_ctx(s, request, me)
        items, total = svc.search(s, me, project=params["project"], assigned_to=params["assigned_to"],
                                  status=params["status"] or None, prio=params["prio"] or None, q=params["q"],
                                  sort=params["sort"], desc=params["desc"], limit=500,
                                  owner=None if octx["owner_id"] is None else octx["owner"], sprint=params["sprint"])
        opts = _filter_options(s, me, keep_agents=[params["assigned_to"]])
        can_plan = me.role in ("admin", "orchestrator") or bool(access.owned_project_keys(s, me))

    def sort_url(col: str) -> str:
        qp = [(k, v) for k, v in request.query_params.multi_items() if k not in ("sort", "desc")]
        qp += [("sort", col), ("desc", "0" if params["sort"] == col and params["desc"] else "1"
                                if params["sort"] == col else "0")]
        return "/backlog?" + urlencode(qp)

    active = [params["assigned_to"], params["project"], params["sprint"], params["prio"],
              sorted(params["status"]) != sorted(DEFAULT_BACKLOG_STATUS)]
    ctx = dict(items=items, total=total, params=params, sort_url=sort_url, statuses=TASK_STATUSES,
               sprint_filter=params["sprint"], can_plan_sprints=can_plan, active_filters=sum(map(bool, active)),
               **opts, **octx)
    if is_htmx(request) and request.headers.get("HX-Target") == "backlog-table":
        return _with_owner_cookie(render(request, "_backlog_table.html", **ctx), octx)
    return _with_owner_cookie(render(request, "backlog.html", **ctx), octx)


# --- Kanban -------------------------------------------------------------------------------

@router.get("/kanban")
def kanban(request: Request, user: UiUser = Depends(require_user)):
    qp = request.query_params
    project, agent, sprint = qp.get("project") or None, qp.get("agent") or None, _sprint_param(request)
    with web_session() as s:
        me = ui_actor(s, user)
        octx = _owner_ctx(s, request, me)
        owner = None if octx["owner_id"] is None else octx["owner"]
        items, _total = svc.search(s, me, project=project, assigned_to=agent,
                                   status=[c for c in KANBAN_STATUSES if c != "done"], limit=500, owner=owner,
                                   sprint=sprint)
        done_range, done_changed = _done_range_choice(request)
        since = done_since(done_range, svc.utcnow())
        done, done_total = svc.search(s, me, project=project, assigned_to=agent, status=["done"],
                                      limit=KANBAN_DONE_LIMIT, sort="done_at", desc=True, owner=owner, sprint=sprint,
                                      done_since=since)
        done_all = done_total if since is None else svc.search(  # For "(n of N)" in the column head.
            s, me, project=project, assigned_to=agent, status=["done"], limit=1, owner=owner, sprint=sprint)[1]
        can_drag = me is not None and (me.role in ("admin", "orchestrator") or bool(access.owned_project_keys(s, me)))
        opts = _filter_options(s, me, keep_agents=[agent])
    columns = _kanban_columns(items, done)
    # Column shown on phones (portrait): `col` parameter, kept in the URL by static/kanban.js.
    active_col = qp.get("col") if qp.get("col") in {c["key"] for c in columns} else KANBAN_DEFAULT_COLUMN
    response = render(request, "kanban.html", columns=columns, project=project, agent=agent, sprint_filter=sprint,
                      active_col=active_col, active_filters=sum(map(bool, (project, agent, sprint))),
                      can_drag=can_drag, done_range=done_range, done_ranges=DONE_RANGES, done_total=done_total,
                      done_all=done_all, done_backlog_url=_done_backlog_url(project, agent, sprint), **opts, **octx)
    if done_changed:
        response.set_cookie(DONE_RANGE_COOKIE, done_range, max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return _with_owner_cookie(response, octx)


KANBAN_DEFAULT_COLUMN = "in_progress"


# Kanban filter for the done column: key → label. Days count back from now; "today" starts at local midnight
# (TZ_DISPLAY). "all" shows the newest KANBAN_DONE_LIMIT cards with a hint to the backlog.
DONE_RANGES = {"today": N_("Today"), "3d": N_("Last 3 days"), "7d": N_("Last week"), "30d": N_("Last month"),
               "all": N_("All")}
DONE_RANGE_DAYS = {"3d": 3, "7d": 7, "30d": 30}
DONE_RANGE_DEFAULT = "3d"
DONE_RANGE_COOKIE = "tm_done_range"
KANBAN_DONE_LIMIT = 100


def _done_backlog_url(project: str | None, agent: str | None, sprint: str | None) -> str:
    """Backlog with status Done, newest first, keeping the kanban filters (for done tasks beyond the limit)."""
    qp = [(k, v) for k, v in (("project", project), ("agent", agent), ("sprint", sprint)) if v]
    return "/backlog?" + urlencode([*qp, ("status", "done"), ("sort", "done_at"), ("desc", "1")])


def _done_range_choice(request: Request) -> tuple[str, bool]:
    """Done filter: `done` parameter, else the last cookie choice, else the last 3 days. The second value is True
    when a cookie should be written (also to replace an unknown value)."""
    if "done" in request.query_params:
        value = request.query_params.get("done") or ""
        return (value if value in DONE_RANGES else DONE_RANGE_DEFAULT), True
    value = request.cookies.get(DONE_RANGE_COOKIE)
    if value is None:
        return DONE_RANGE_DEFAULT, False
    return (value, False) if value in DONE_RANGES else (DONE_RANGE_DEFAULT, True)


def done_since(done_range: str, now: datetime) -> datetime | None:
    """Start of the done range as naive UTC (`now` is naive UTC); None for "all"."""
    if done_range in DONE_RANGE_DAYS:
        return now - timedelta(days=DONE_RANGE_DAYS[done_range])
    if done_range == "today":
        tz = ZoneInfo(get_settings().tz_display)
        local = now.replace(tzinfo=UTC).astimezone(tz)
        midnight = datetime.combine(local.date(), time(0), tzinfo=tz)
        return midnight.astimezone(UTC).replace(tzinfo=None)
    return None


def _kanban_columns(items: list[dict], done: list[dict]) -> list[dict]:
    """Group the cards into the kanban columns. The shared column lists aborted tasks first, then questions,
    each longest waiting first (last change, i.e. the status change, oldest first)."""
    columns = []
    for key, statuses, drop in KANBAN_COLUMNS:
        if key == "done":
            cards = done
        elif len(statuses) == 1:
            cards = [t for t in items if t["status"] == statuses[0]]
        else:
            cards = sorted((t for t in items if t["status"] in statuses),
                           key=lambda t: (statuses.index(t["status"]), t["updated_at"]))
        label = N_("Questions / Aborted") if key == "waiting" else None
        columns.append({"key": key, "statuses": statuses, "drop": drop, "label": label, "cards": cards})
    return columns


def allowed_moves(session, task: Task, me: Actor) -> list[str]:
    """Drop targets of the kanban columns `me` may move the card to by the normal rules (a generic state machine
    rule from the task's status that `check_allowed` accepts) – never an override. The card's own column is left
    out. Conditions checked only while running (e.g. a description before releasing) are reported by the move."""
    owner_id = session.get(Project, task.project_id).owner_id
    targets = []
    for _key, statuses, drop in KANBAN_COLUMNS:
        rule = None if task.status in statuses else state_machine.find_generic_rule(task.status, drop)
        if rule is None:
            continue
        try:
            state_machine.check_allowed(rule, task, me, owner_id,
                                        rule.who == "reviewer" and reviews.may_review(session, task, me))
        except AppError:
            continue
        targets.append(drop)
    return targets


@router.get("/kanban/{nr}/moves")
def kanban_moves(request: Request, nr: str, user: UiUser = Depends(require_user)):
    """Choices of the "Move" menu on a kanban card (touch and keyboard alternative to drag and drop)."""
    with web_session() as s:
        task = svc.get(s, nr)
        targets = allowed_moves(s, task, ui_actor(s, user))
        nr, status = task.task_nr, task.status
    return render(request, "_card_moves.html", nr=nr, status=status, targets=targets)


@router.post("/kanban/move", dependencies=POST)
def kanban_move(request: Request, user: UiUser = Depends(require_user), task_nr: str = Form(...),
                to: str = Form(...), comment: str = Form(""), override: bool = Form(False)):
    """Drag and drop. 409 moves the card back (admin can override after confirmation);
    422 requires a comment (e.g. an answer or review reason)."""
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            task = svc.get(s, task_nr, for_update=True)
            state_machine.transition_to(s, task, to, me, comment or None, override)
            if override:
                audit(me.name, "status_override", task=task.task_nr, to=to, via="kanban")
            names = {task.assigned_to: s.scalar(select(Actor.name).where(Actor.id == task.assigned_to))}
            card = svc.summary(task, s.get(Project, task.project_id).project_key, names)
            sprint_names = {task.sprint_id: s.get(Sprint, task.sprint_id).name} if task.sprint_id else {}
    except InvalidTransition as e:
        with web_session() as s:
            can_override = state_machine.can_override(s, svc.get(s, task_nr), ui_actor(s, user))
        return JSONResponse({"message": e.message, "can_override": can_override}, status_code=409)
    except AppError as e:
        return JSONResponse({"message": e.message, "needs_comment": e.code == "missing_field"},
                            status_code=e.status)
    return render(request, "_card.html", t=card, sprint_names=sprint_names)


# --- Create task -------------------------------------------------------------------------

@router.get("/tasks/new")
def new_task(request: Request, user: UiUser = Depends(require_user)):
    qp = request.query_params
    with web_session() as s:
        opts = _filter_options(s, ui_actor_or_none(s, user), keep=qp.get("project"))
    return render(request, "task_new.html", form={"project": qp.get("project"), "parent_task_nr": qp.get("parent"),
                                                  "prio": 3, "status": "draft", "sprint": qp.get("sprint")},
                  back=_back_url(request.headers.get("referer"), qp), **opts)


def _back_url(referer: str | None, qp=None) -> str:
    """Target of "Cancel" in the new-task form: the page it was opened from (same site only), else the parent
    task, the project or the backlog."""
    qp = qp or {}
    if referer:
        parts = urlsplit(referer)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if path.startswith("/") and not path.startswith("//") and not parts.path.startswith("/tasks/new"):
            return path
    if qp.get("parent"):
        return f"/tasks/{quote(qp['parent'])}"
    if qp.get("project"):
        return f"/projects/{quote(qp['project'])}"
    return "/backlog"


MODEL_ERRORS = ("invalid_model_tier", "invalid_model", "model_not_allowed")


def _error_section(e: AppError, parent_task_nr: str, depends_on: str) -> str | None:
    """Folded form section that holds the field of a validation error, so the form opens it."""
    if e.code in MODEL_ERRORS:
        return "model"
    nrs = [x.upper() for x in (parent_task_nr + " " + depends_on.replace(",", " ")).split()]
    if e.message == _("Parent task must be in the same project") or any(nr in e.message.upper() for nr in nrs):
        return "links"
    return None


def _sprint_id(value: str) -> int | None:
    """Sprint form field: empty or `none` = no sprint, otherwise the sprint id."""
    if value.strip() in ("", "none"):
        return None
    if not value.strip().isdigit():
        raise AppError(_("Unknown sprint"))
    return int(value)


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError as e:
        raise AppError(_("Date in the format YYYY-MM-DD")) from e


@router.post("/tasks/new", dependencies=POST)
def create_task(request: Request, user: UiUser = Depends(require_user), project: str = Form(...),
                title: str = Form(...), description: str = Form(""), acceptance_criteria: str = Form(""),
                prio: int = Form(3), assigned_to: str = Form(""), due_date: str = Form(""),
                parent_task_nr: str = Form(""), depends_on: str = Form(""), create_spec_file: bool = Form(False),
                status: str = Form("draft"), sprint: str = Form(""), model_tier: str = Form(""),
                model: str = Form(""), reviewer: str = Form(""), back: str = Form(""),
                attachments: list[UploadFile] = File(default=[])):
    form = dict(project=project, title=title, description=description, acceptance_criteria=acceptance_criteria,
                prio=prio, assigned_to=assigned_to, due_date=due_date, parent_task_nr=parent_task_nr,
                depends_on=depends_on, status=status, sprint=sprint, model_tier=model_tier, model=model,
                reviewer=reviewer, create_spec_file=create_spec_file)
    try:
        with web_session() as s:
            task = svc.create(s, ui_actor(s, user), project=project, title=title, description=description,
                              acceptance_criteria=acceptance_criteria, prio=prio,
                              assigned_to=assigned_to or None, due_date=_parse_date(due_date),
                              parent_task_nr=parent_task_nr.strip() or None,
                              depends_on=[x.strip() for x in depends_on.replace(",", " ").split() if x.strip()],
                              create_spec_file=create_spec_file, status=status, sprint=_sprint_id(sprint),
                              model_tier=model_tier or None, model=model or None,  # `none` = no choice
                              reviewer=reviewer or None)  # empty = project default, `none` = nobody
            nr = task.task_nr
            notice = sprints.planned_notice(s.get(Sprint, task.sprint_id)) \
                if status == "defined" and task.status == "draft" and task.sprint_id else None
    except AppError as e:
        with web_session() as s:
            opts = _filter_options(s, ui_actor_or_none(s, user), keep=project)
        return render(request, "task_new.html", 422, form=form, error=e.message, back=_back_url(back),
                      error_section=_error_section(e, parent_task_nr, depends_on), **opts)
    # Upload attachments after saving so the task survives an upload failure.
    error = None
    uploads = [a for a in attachments if a.filename]
    if uploads:
        error = _attach(user, nr, uploads)
    msg = " · ".join(m for m in (notice, _stored(len(uploads)) if uploads else None) if m)
    target = f"/tasks/{nr}" + (f"?err={quote_plus(error)}" if error else (f"?msg={quote_plus(msg)}" if msg else ""))
    return RedirectResponse(target, 303)


def _attach(user: UiUser, nr: str, files: list[UploadFile]) -> str | None:
    """Attach files; return an error message or None."""
    from app.services import attachments

    uploads = [attachments.Upload(f.filename or "file", f.file.read(attachments.MAX_BYTES + 1)) for f in files]
    try:
        with web_session() as s:
            attachments.add(s, ui_actor(s, user), svc.get(s, nr), uploads)
    except AppError as e:
        return _("Attachments not saved: %(error)s", error=e.message)
    return None


def _stored(n: int) -> str:
    return ngettext("%(num)d attachment stored in the repo", "%(num)d attachments stored in the repo", n)


@router.post("/tasks/{nr}/attachments", dependencies=POST)
def upload_attachments(request: Request, nr: str, user: UiUser = Depends(require_user),
                       attachments: list[UploadFile] = File(default=[])):
    files = [a for a in attachments if a.filename]
    if not files:
        return RedirectResponse(f"/tasks/{nr}?err={quote_plus(_('No file selected'))}", 303)
    error = _attach(user, nr, files)
    return RedirectResponse(f"/tasks/{nr}?" + (f"err={quote_plus(error)}" if error else
                                              f"msg={quote_plus(_stored(len(files)))}"), 303)


@router.post("/tasks/quick", dependencies=POST)
def quick_capture(request: Request, user: UiUser = Depends(require_user), title: str = Form(...),
                  project: str = Form(...)):
    with web_session() as s:
        task = svc.create(s, ui_actor(s, user), project=project, title=title, status="draft")  # Develop the idea first.
        nr = task.task_nr
    return RedirectResponse(f"/tasks/{nr}", 303)


@router.post("/markdown-preview", dependencies=POST)
async def markdown_preview(request: Request, user: UiUser = Depends(require_user)):
    """Render the form's first text field (sent via hx-include)."""
    from fastapi.responses import HTMLResponse

    form = await request.form()
    text = next((str(v) for k, v in form.multi_items() if k != "csrf_token"), "")
    return HTMLResponse(render_markdown(text) if text.strip() else f"<em>{escape(_('empty'))}</em>")


# --- Task-Detail --------------------------------------------------------------------------

def _detail(request: Request, user: UiUser, nr: str, status_code: int = 200, **ctx):
    with web_session() as s:
        me = ui_actor(s, user)
        task_obj = svc.get(s, nr)
        task = svc.payload(s, task_obj)
        if task_obj.status == "questions":  # Seen here: may be answered from Telegram (local_only)
            telegram_actions.mark_questions_read(s, me, [task_obj.task_nr])
        opts = _filter_options(s, me, keep=task["project"], keep_agents=[task["assigned_to"]])
        # Sprint choice: open sprints of the task's project, plus the current one even when it is closed.
        project_sprints = [x for x in opts["sprints"] if x["project"] == task["project"]
                           and (x["status"] != "closed" or x["id"] == task_obj.sprint_id)]
        is_owner = access.owns(s.get(Project, task_obj.project_id), me)
        agent_reviewer = reviews.agent_reviewer(s, task_obj)
        rights = {
            "is_owner": is_owner,
            # Edit, assign, cancel: admins, orchestrators, owners.
            "can_manage": me is not None and (me.role in ("admin", "orchestrator") or is_owner),
            # Answer questions: admins and owners.
            "can_answer": me is not None and (me.role == "admin" or is_owner),
            # Approve, reject: admins, owners and the task's entered reviewer.
            "can_review": me is not None and (me.role == "admin" or is_owner or reviews.may_review(s, task_obj, me)),
            "agent_reviewer": agent_reviewer.name if agent_reviewer else None,
            # Override, reassign active tasks, release aborted tasks: human admins and owners.
            "human_admin": state_machine.can_override(s, task_obj, me),
            # Duplicate into a new draft: admins, orchestrators, owners.
            "can_duplicate": svc.can_duplicate(s.get(Project, task_obj.project_id), me),
        }
    return render(request, "task_detail.html", status_code, t=task, me=me, statuses=TASK_STATUSES,
                  project_sprints=project_sprints, **rights, **opts, **ctx)


@router.get("/tasks/{nr}")
def task_detail(request: Request, nr: str, user: UiUser = Depends(require_user)):
    return _detail(request, user, nr)


def _run(request: Request, user: UiUser, nr: str, fn, message: str, **error_ctx):
    """Run an action; redirect on success or show the detail page with an error (plus `error_ctx`, e.g. the
    entered values)."""
    try:
        with web_session() as s:
            fn(s, ui_actor(s, user))
    except AppError as e:
        return _detail(request, user, nr, 422 if e.status < 500 else e.status, error=e.message,
                       merge_conflict=e.code in ("merge_conflict", "brain_conflict", "changelog_invalid"), **error_ctx)
    return RedirectResponse(f"/tasks/{nr}?msg={quote_plus(message)}", 303)


@router.get("/tasks/{nr}/follow-up")
def follow_up_form(request: Request, nr: str, user: UiUser = Depends(require_user)):
    with web_session() as s:
        origin = svc.get(s, nr)
        form = {**svc.follow_up_draft(origin), "prio": origin.prio, "status": "draft", "assigned_to": "",
                "points_into": "description"}
        form["selected"] = list(range(len(form["points"])))  # All open points preselected.
        return render(request, "task_follow_up.html", origin=svc.summary(origin, s.get(Project, origin.project_id)
                                                                         .project_key, {}),
                      form=form, **_filter_options(s, ui_actor_or_none(s, user),
                                                   keep=s.get(Project, origin.project_id).project_key))


@router.post("/tasks/{nr}/follow-up", dependencies=POST)
def create_follow_up(request: Request, nr: str, user: UiUser = Depends(require_user), title: str = Form(...),
                     description: str = Form(""), acceptance_criteria: str = Form(""), prio: int = Form(3),
                     assigned_to: str = Form(""), status: str = Form("draft"), points: list[str] = Form(default=[]),
                     points_into: str = Form("description")):
    selected = sorted({int(p) for p in points if p.isdigit()})
    form = dict(title=title, description=description, acceptance_criteria=acceptance_criteria, prio=prio,
                assigned_to=assigned_to, status=status, points_into=points_into, selected=selected)
    try:
        with web_session() as s:
            origin = svc.get(s, nr)
            available = svc.open_points(origin)
            task = svc.create_follow_up(s, ui_actor(s, user), origin, title=title, description=description,
                                        acceptance_criteria=acceptance_criteria,
                                        points=[available[i] for i in selected if i < len(available)],
                                        points_into=points_into, prio=prio, assigned_to=assigned_to or None,
                                        status=status)
            new_nr = task.task_nr
    except AppError as e:
        with web_session() as s:
            origin = svc.get(s, nr)
            form["points"] = svc.open_points(origin)
            return render(request, "task_follow_up.html", 422, origin=svc.summary(
                origin, s.get(Project, origin.project_id).project_key, {}), form=form, error=e.message,
                **_filter_options(s, ui_actor_or_none(s, user), keep=s.get(Project, origin.project_id).project_key,
                                  keep_agents=[assigned_to]))
    return RedirectResponse(f"/tasks/{new_nr}?msg={quote_plus(_('Follow-up task created'))}", 303)


@router.post("/tasks/{nr}/duplicate", dependencies=POST)
def duplicate_task(request: Request, nr: str, user: UiUser = Depends(require_user), assign: str = Form("")):
    """Copy the task into a new draft and open it for editing."""
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            origin = svc.get(s, nr)
            task = svc.duplicate(s, me, origin, assign=assign.strip() or None)
            new_nr = task.task_nr
            audit(me.name, "task_duplicated", task=origin.task_nr, duplicate=new_nr, via="ui")
    except AppError as e:
        return _detail(request, user, nr, 422 if e.status < 500 else e.status, error=e.message)
    msg = _("Duplicated from %(task_nr)s – a draft; adjust it and release it.", task_nr=nr.upper())
    return RedirectResponse(f"/tasks/{new_nr}?msg={quote_plus(msg)}#edit", 303)


@router.post("/tasks/{nr}/edit", dependencies=POST)
def edit_task(request: Request, nr: str, user: UiUser = Depends(require_user), row_version: int = Form(...),
              title: str = Form(...), description: str = Form(""), acceptance_criteria: str = Form(""),
              prio: int = Form(...), assigned_to: str = Form(""), due_date: str = Form(""),
              sprint: str = Form(""), then_publish: bool = Form(False), model_tier: str = Form(""),
              model: str = Form(""), structure: bool = Form(False), parent_task_nr: str = Form(""),
              depends_on: str = Form(""), reviewer: str = Form("")):
    def fn(s, me):
        task = svc.get(s, nr, for_update=True)
        changes = {"title": title, "description": description or None,
                   "acceptance_criteria": acceptance_criteria or None, "prio": prio,
                   "due_date": _parse_date(due_date)}
        current = s.scalar(select(Actor.name).where(Actor.id == task.assigned_to)) if task.assigned_to else ""
        if (assigned_to or "") != (current or ""):
            changes["assigned_to"] = assigned_to or None
        # FastAPI turns an empty form value into the default, so the form sends `none` to detach; empty = keep.
        if sprint and _sprint_id(sprint) != task.sprint_id:
            changes["sprint"] = _sprint_id(sprint)
        # Only changed values: an unchanged model must not be re-checked against a new assignee (it is dropped).
        # The form sends `none` to clear; empty/missing keeps the value (FastAPI cannot tell them apart).
        for key, value in (("model_tier", model_tier), ("model", model)):
            if value and (None if value == "none" else value) != getattr(task, key):
                changes[key] = None if value == "none" else value
        current_reviewer = s.scalar(select(Actor.name).where(Actor.id == task.reviewer_id)) if task.reviewer_id else None
        if reviewer and (None if reviewer == "none" else reviewer) != current_reviewer:  # `none` clears, empty keeps
            changes["reviewer"] = None if reviewer == "none" else reviewer
        if structure:  # Parent/dependency fields are on the form (drafts); without the marker they stay unchanged.
            changes["parent_task_nr"] = parent_task_nr.strip() or None
            changes["depends_on"] = [x.strip() for x in depends_on.replace(",", " ").split() if x.strip()]
        svc.update(s, me, task, changes, row_version)
    # On an error the form opens again with the submitted values (no lost input) and with the folded section
    # that holds the faulty field open.
    entered = dict(row_version=row_version, title=title, description=description,
                   acceptance_criteria=acceptance_criteria, prio=prio, assigned_to=assigned_to, due_date=due_date,
                   sprint=sprint, model_tier=model_tier, model=model, reviewer=reviewer)
    if structure:
        entered.update(parent_task_nr=parent_task_nr, depends_on=depends_on)
    # Save and publish: save in a separate commit before publishing. If publishing fails
    # (e.g. missing acceptance criteria), the changes remain saved.
    try:
        with web_session() as s:
            fn(s, ui_actor(s, user))
    except AppError as e:
        return _detail(request, user, nr, 422 if e.status < 500 else e.status, error=e.message, edit_form=entered,
                       error_section=_error_section(e, parent_task_nr if structure else "",
                                                    depends_on if structure else ""))
    if not then_publish:
        return RedirectResponse(f"/tasks/{nr}?msg={quote_plus(_('Saved'))}", 303)
    try:
        with web_session() as s:
            state_machine.transition(s, svc.get(s, nr, for_update=True), "publish", ui_actor(s, user), {})
    except AppError as e:
        err = _("Saved, but not released: %(error)s", error=e.message)
        return RedirectResponse(f"/tasks/{nr}?err={quote_plus(err)}", 303)
    msg = _("Saved and released – agents can pick it up now")
    return RedirectResponse(f"/tasks/{nr}?msg={quote_plus(msg)}", 303)


@router.post("/tasks/{nr}/comment", dependencies=POST)
def comment_task(request: Request, nr: str, user: UiUser = Depends(require_user), body: str = Form(...)):
    return _run(request, user, nr, lambda s, me: svc.comment(s, me, svc.get(s, nr), body), _("Commented"),
                comment_body=body)  # On an error the thread opens with the unsent text.


@router.post("/tasks/{nr}/action", dependencies=POST)
def task_action(request: Request, nr: str, user: UiUser = Depends(require_user), action: str = Form(...),
                body: str = Form(""), to: str = Form("")):
    def fn(s, me):
        task = svc.get(s, nr, for_update=True)
        if action == "override":
            state_machine.transition_to(s, task, to, me, body or None, override=True)
            audit(me.name, "status_override", task=nr, to=to, via="ui")
        elif action in ("answer", "approve", "reject", "reject_conflict", "cancel", "abort", "reopen", "publish",
                        "to_draft"):
            state_machine.transition(s, task, action, me, {"body": body})
            if action in ("approve", "reject", "reject_conflict"):
                audit(me.name, f"review_{action}", task=nr, via="ui")
        else:
            raise AppError(_("Unknown action '%(action)s'", action=action))
    labels = {"answer": N_("Answered"), "approve": N_("Merged"), "reject": N_("Rejected"),
              "reject_conflict": N_("Returned to the agent with conflict instructions"),
              "cancel": N_("Cancelled"), "abort": N_("Aborted – waiting for reassignment"),
              "reopen": N_("Released again"), "publish": N_("Released – agents can pick it up now"),
              "to_draft": N_("Back to draft"), "override": N_("Status set")}
    return _run(request, user, nr, fn, _(labels.get(action, N_("OK"))))


@router.post("/inbox/{nr}/{action}", dependencies=POST)
def inbox_action(request: Request, nr: str, action: str, user: UiUser = Depends(require_user),
                 body: str = Form("")):
    """Inbox inline actions (HTMX): answer, approve, reject; replace the item with the result."""
    if action not in ("answer", "approve", "reject", "reject_conflict"):
        raise AppError(_("Unknown action"))
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            state_machine.transition(s, svc.get(s, nr, for_update=True), action, me, {"body": body})
            if action != "answer":
                audit(me.name, f"review_{action}", task=nr, via="inbox")
    except AppError as e:
        if e.code in ("merge_conflict", "brain_conflict", "changelog_invalid"):  # Offer a direct "return to agent" button on conflict.
            return render(request, "_merge_conflict.html", 422, nr=nr, message=e.message)
        return render(request, "_flash.html", 422, message=f"{nr}: {e.message}", kind="error")
    done = {"answer": N_("%(nr)s answered – can be picked up again."), "approve": N_("%(nr)s merged and done."),
            "reject": N_("%(nr)s rejected."),
            "reject_conflict": N_("%(nr)s returned to the agent with conflict instructions.")}[action]
    return render(request, "_flash.html", message=_(done, nr=nr), kind="ok")
