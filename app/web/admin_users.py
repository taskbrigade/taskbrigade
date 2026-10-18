"""Admin → Users & Agents (spec sections 11a.2 / 11a.3, variant A)."""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import UiUser, csrf_protect, current_user, require_admin
from app.errors import AppError
from app.i18n import _
from app.logging_setup import audit
from app.models import AGENT_KINDS, ROLES, Actor
from app.services import actors as svc
from app.services import model_choice, settings_store, zones
from app.web.common import render, web_session

router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_admin)])
POST = [Depends(csrf_protect)]


def _me(session, user: UiUser) -> Actor | None:
    return session.get(Actor, user.actor_id) if user.actor_id else None


def _opt_int(value: str, default: int) -> int:
    try:
        return int(value) if value.strip() else default
    except ValueError as e:
        raise AppError(_("Number expected")) from e


def _token_page(request: Request, actor: Actor, token: str, created: bool):
    if request.query_params.get("panel") == "1":
        response = _users_page(request, selected=actor.name, new_token=token)
        response.headers["Cache-Control"] = "no-store"
        return response
    from app.services import settings_store

    with web_session() as s:
        base = settings_store.get(s, "app_url") or str(request.base_url)
    base = base.rstrip("/")
    return render(request, "admin/user_token.html", actor=actor, token=token, created=created, base_url=base)


def _list_filters(request: Request) -> dict:
    """Filters of the list (query parameters, combinable and shareable): `owner` (name or `none`), `role`, `kind`
    (agent_kind). Unknown values are ignored."""
    qp = request.query_params
    return {"owner": (qp.get("owner") or "").strip(), "role": qp.get("role") if qp.get("role") in ROLES else "",
            "kind": qp.get("kind") if qp.get("kind") in AGENT_KINDS else "",
            "q": (qp.get("q") or "").strip(),
            "type": qp.get("type") if qp.get("type") in ("human", "agent") else ""}


def _matches(actor: Actor, owner: str | None, f: dict) -> bool:
    if f.get("type") and actor.actor_type != f["type"]:
        return False
    if f.get("q") and f["q"].casefold() not in " ".join(
            str(v or "") for v in (actor.name, actor.display_name, actor.agent_kind, actor.gitea_username)).casefold():
        return False
    if f["role"] and actor.role != f["role"]:
        return False
    if f["kind"] and actor.agent_kind != f["kind"]:
        return False
    if f["owner"]:  # Owner filter: agents only (humans have no owner).
        return actor.actor_type == "agent" and (owner is None if f["owner"] == "none" else owner == f["owner"])
    return True


def presence(actor: Actor, now: datetime) -> dict:
    seconds = None
    if actor.last_seen_at:
        seen = actor.last_seen_at.replace(tzinfo=UTC) if actor.last_seen_at.tzinfo is None else actor.last_seen_at
        seconds = max(0, (now - seen).total_seconds())
    state = "offline" if not actor.is_active or seconds is None or seconds >= 86400 else (
        "online" if seconds < 900 else "idle")
    if seconds is None:
        label = _("Never")
    elif seconds < 60:
        label = _("Just now")
    elif seconds < 3600:
        label = _("%(count)s min ago", count=int(seconds // 60))
    elif seconds < 86400:
        label = _("%(count)s h ago", count=int(seconds // 3600))
    else:
        label = _("%(count)s d ago", count=int(seconds // 86400))
    return {"state": state, "label": label, "status": {
        "online": _("Online"), "idle": _("Idle"), "offline": _("Offline")}[state]}


@router.get("")
def list_users(request: Request):
    return _users_page(request)


def _users_page(request: Request, selected: str | None = None, status: int = 200, **ctx):
    """With the profile switch "Show all agents" off the list shows humans and only the admin's own agents
    (`access.own_agents_only`); `?all_agents=1` shows every agent for this view. A hidden agent stays reachable
    through its detail link."""
    from app.services import access

    f = _list_filters(request)
    all_agents = request.query_params.get("all_agents") == "1"
    with web_session() as s:
        user = current_user(request)
        me = _me(s, user) if user else None
        every = svc.list_all(s)
        everyone = every
        if not all_agents and access.own_agents_only(me):  # Humans are always listed.
            everyone = [a for a in every if a.actor_type == "human" or a.owner_id == me.id]
        owners = svc.owner_names(s)
        counts = svc.task_counts(s)
        enforce = zones.enforced(s)
        names = humans(s)
        selected = selected or request.query_params.get("detail")
        actor = svc.get(s, selected) if selected else None
    actors = [a for a in everyone if _matches(a, owners.get(a.id), f)]
    # Zone badge "may work up to": the strictest data class the actor may work on.
    zone_max = {a.id: max(zones.allowed_classes(a, enforce), key=zones.CLASS_RANK.get) for a in actors}
    now = datetime.now(UTC)
    seen = {a.id: presence(a, now) for a in every}  # Also for a hidden agent opened via its detail link.
    groups = []
    for owner in sorted({owners.get(a.id) or "" for a in actors if a.actor_type == "agent"}):
        rows = [a for a in actors if a.actor_type == "agent" and (owners.get(a.id) or "") == owner]
        groups.append({"owner": owner, "agents": True, "rows": rows,
                       "online": sum(seen[a.id]["state"] == "online" for a in rows)})
    people = [a for a in actors if a.actor_type == "human"]
    if people:
        groups.append({"agents": False, "rows": people})
    if actor and actor.id not in zone_max:
        zone_max[actor.id] = max(zones.allowed_classes(actor, enforce), key=zones.CLASS_RANK.get)
    return render(request, "admin/users.html", status, actors=actors, total=len(everyone), counts=counts, owners=owners,
                  zone_max=zone_max, presence=seen, groups=groups, actor=actor, humans=names,
                  list_url=request.url_for("list_users").include_query_params(
                      **f, **({"all_agents": "1"} if all_agents else {})),
                  type_counts={t: sum(a.actor_type == t for a in everyone) for t in ("agent", "human")},
                  hidden_agents=len(every) - len(everyone), all_agents=all_agents,
                  all_agents_url=request.url_for("list_users").include_query_params(**f, all_agents="1"),
                  filters=f, filtered=any(f.values()), roles=ROLES, kinds=AGENT_KINDS,
                  section="users", **ctx)


def humans(session) -> list[str]:
    """Owner choices for agents: active humans."""
    from sqlalchemy import select

    return list(session.scalars(select(Actor.name).where(Actor.actor_type == "human", Actor.is_active.is_(True))
                                .order_by(Actor.name)))


def owner_from_form(session, value: str, default: Actor | None) -> Actor | None:
    """Owner select: `none` = no owner, empty/missing = `default`, otherwise a human's name."""
    if not value.strip():
        return default
    return svc.resolve_owner(session, value)


def _new_form(request: Request, status: int = 200, **ctx):
    with web_session() as s:
        names = humans(s)
    return render(request, "admin/user_form.html", status, actor=None, roles=ROLES, kinds=AGENT_KINDS,
                  section="users", humans=names, **{"form": {}, **ctx})


@router.get("/new")
def new_form(request: Request, user: UiUser = Depends(require_admin)):
    return _new_form(request, form={"owner": user.name if user.actor_id else "",
                                    "actor_type": "human" if request.query_params.get("type") == "human" else "agent",
                                    "role": "member" if request.query_params.get("type") == "human" else "worker"})


@router.post("/new", dependencies=POST)
def create(request: Request, user: UiUser = Depends(require_admin), name: str = Form(...),
           display_name: str = Form(""), actor_type: str = Form(...), agent_kind: str = Form(""),
           role: str = Form("worker"), runtime: str = Form("local"), lease_minutes: str = Form("120"),
           max_parallel_tasks: str = Form("2"), notes: str = Form(""), email: str = Form(""),
           password: str = Form(""), password2: str = Form(""), tailscale_login: str = Form(""),
           owner: str = Form(""), is_public: bool = Form(False), model_location: str = Form("cloud"),
           confirm_local_model: bool = Form(False)):
    form = {"name": name, "display_name": display_name, "actor_type": actor_type, "agent_kind": agent_kind,
            "role": role, "runtime": runtime, "model_location": model_location, "lease_minutes": lease_minutes,
            "max_parallel_tasks": max_parallel_tasks, "notes": notes, "email": email,
            "tailscale_login": tailscale_login, "owner": owner, "is_public": is_public}
    try:
        if password != password2:
            raise AppError(_("The passwords do not match"))
        if actor_type == "agent":
            zones.require_confirmation(model_location, None, confirm_local_model)
        with web_session() as s:
            created = svc.create(
                s, name=name, display_name=display_name or name, actor_type=actor_type,
                agent_kind=agent_kind if actor_type == "agent" else None, role=role, runtime=runtime,
                model_location=model_location if actor_type == "agent" else "cloud",
                lease_minutes=_opt_int(lease_minutes, 120), max_parallel_tasks=_opt_int(max_parallel_tasks, 2),
                notes=notes, email=email if actor_type == "human" else None,
                password=password if actor_type == "human" else None,
                tailscale_login=tailscale_login if actor_type == "human" else None,
                owner=owner_from_form(s, owner, _me(s, user)) if actor_type == "agent" else None,
                is_public=is_public)
            audit(user.name, "actor_created", target=created.actor.name, model_location=created.actor.model_location,
                  via="ui")
            return _token_page(request, created.actor, created.token, created=True)
    except AppError as e:
        return _new_form(request, 422, form=form, error=e.message)


def _access_rows(s, actor: Actor) -> list[dict]:
    from sqlalchemy import select

    from app.models import Project, ProjectMember

    rows = s.execute(select(Project, ProjectMember).outerjoin(
        ProjectMember, (ProjectMember.project_id == Project.id) & (ProjectMember.actor_id == actor.id))
        .order_by(Project.project_key)).all()
    return [{"project": p, "member": pm,
             "blocked": not zones.may_handle(p, actor, session=s)} for p, pm in rows]


def _detail(request: Request, name: str, status: int = 200, **ctx):
    if request.query_params.get("panel") == "1":
        return _users_page(request, selected=name, status=status, **ctx)
    with web_session() as s:
        actor = svc.get(s, name)
        counts = svc.task_counts(s).get(actor.id, {"open": 0, "running": 0, "current": None})
        access = _access_rows(s, actor)
        models = model_choice.settings(actor)
        sftp_enabled = settings_store.get_bool(s, "sftp_enabled")
        names = humans(s)
        owner = s.get(Actor, actor.owner_id).name if actor.owner_id else None
    return render(request, "admin/user_form.html", status, actor=actor, roles=ROLES, kinds=AGENT_KINDS,
                  section="users", counts=counts, form={}, access=access, models=models, humans=names, owner=owner,
                  model_tiers=model_choice.TIER_LABELS, sftp_enabled=sftp_enabled,
                  **ctx)


@router.get("/{name}")
def detail(request: Request, name: str):
    return _detail(request, name)


def _action(request: Request, name: str, fn, ok_message: str):
    """Run the action, then show the detail page with a message instead of crashing on errors."""
    try:
        with web_session() as s:
            result = fn(s, svc.get(s, name))
    except AppError as e:
        return _detail(request, name, 422, error=e.message)
    if isinstance(result, RedirectResponse) or hasattr(result, "body"):
        return result
    return _detail(request, name, message=ok_message)


@router.post("/{name}", dependencies=POST)
def update(request: Request, name: str, user: UiUser = Depends(require_admin), display_name: str = Form(...),
           role: str = Form(...), runtime: str = Form(...), agent_kind: str = Form(""),
           lease_minutes: str = Form(...), max_parallel_tasks: str = Form(...), notes: str = Form(""),
           email: str = Form(""), tailscale_login: str = Form(""), model_default: str = Form(""),
           model_simple: str = Form(""), model_standard: str = Form(""), model_complex: str = Form(""),
           models_allowed: str = Form(""), owner: str = Form(""), is_public: bool = Form(False),
           model_location: str = Form(""), confirm_local_model: bool = Form(False)):
    def fn(s, actor):
        changes = {"display_name": display_name, "role": role, "runtime": runtime, "notes": notes,
                   "lease_minutes": _opt_int(lease_minutes, actor.lease_minutes),
                   "max_parallel_tasks": _opt_int(max_parallel_tasks, actor.max_parallel_tasks)}
        if actor.actor_type == "agent":
            changes["agent_kind"] = agent_kind
            changes["is_public"] = is_public
            if model_location.strip():
                zones.require_confirmation(model_location, actor.model_location, confirm_local_model)
                changes["model_location"] = model_location
            if owner.strip():  # `none` removes the owner; empty/missing keeps it (FastAPI empty-form rule).
                changes["owner"] = owner
            changes.update(model_default=model_default, models_allowed=model_choice.parse_list(models_allowed),
                           model_tiers={"simple": model_simple, "standard": model_standard,
                                        "complex": model_complex})
        else:
            changes.update(email=email, tailscale_login=tailscale_login)
        before = actor.model_location
        changed = svc.update(s, actor, changes)
        audit(user.name, "actor_updated", target=name, fields=changed, via="ui")
        if "model_location" in changed:
            audit(user.name, "model_location_changed", target=name, old=before, new=actor.model_location, via="ui")
    return _action(request, name, fn, _("Saved."))


@router.post("/{name}/summary", dependencies=POST)
def update_summary(request: Request, name: str, user: UiUser = Depends(require_admin),
                   role: str = Form(...), runtime: str = Form(...), owner: str = Form(""),
                   max_parallel_tasks: str = Form(...)):
    def fn(s, actor):
        changes = {"role": role, "runtime": runtime,
                   "max_parallel_tasks": _opt_int(max_parallel_tasks, actor.max_parallel_tasks)}
        if actor.actor_type == "agent" and owner.strip():
            changes["owner"] = owner
        changed = svc.update(s, actor, changes)
        audit(user.name, "actor_updated", target=name, fields=changed, via="ui")
    return _action(request, name, fn, _("Saved."))


@router.post("/{name}/revoke-token", dependencies=POST)
def revoke(request: Request, name: str, user: UiUser = Depends(require_admin)):
    def fn(s, actor):
        actor.api_key_hash = actor.api_key_prefix = actor.api_key_created_at = None
        s.flush()
        audit(user.name, "token_revoked", target=name, via="ui")
    return _action(request, name, fn, _("API token revoked."))


@router.post("/{name}/data-access", dependencies=POST)
def set_data_access(request: Request, name: str, user: UiUser = Depends(require_admin),
                    data_access: bool = Form(False)):
    def fn(s, actor):
        from app.services import data_audit

        if actor.actor_type != "human":
            raise AppError(_("The data right is only for humans"))
        if bool(actor.data_access) == data_access:
            return
        actor.data_access = data_access
        data_audit.append(s, "data_access_changed", "actor", actor.id, actor=_me(s, user), via="web",
                          detail={"actor": actor.name, "data_access": data_access})
        audit(user.name, "data_access_changed", target=name, data_access=data_access, via="ui")
    return _action(request, name, fn, _("Saved."))


@router.post("/{name}/password", dependencies=POST)
def set_password(request: Request, name: str, user: UiUser = Depends(require_admin),
                 password: str = Form(...), password2: str = Form(...)):
    def fn(s, actor):
        if password != password2:
            raise AppError(_("The passwords do not match"))
        svc.set_password(s, actor, password)
        audit(user.name, "password_reset", target=name, via="ui")
    return _action(request, name, fn, _("Password set."))


@router.post("/{name}/rotate-token", dependencies=POST)
def rotate(request: Request, name: str, user: UiUser = Depends(require_admin)):
    try:
        with web_session() as s:
            actor = svc.get(s, name)
            token = svc.rotate_token(s, actor)
            audit(user.name, "token_rotated", target=name, via="ui")
    except AppError as e:
        return _detail(request, name, 422, error=e.message)
    return _token_page(request, actor, token, created=False)


@router.post("/{name}/active", dependencies=POST)
def set_active(request: Request, name: str, user: UiUser = Depends(require_admin), active: bool = Form(...)):
    def fn(s, actor):
        svc.set_active(s, actor, active, by=_me(s, user))
        audit(user.name, "actor_activated" if active else "actor_deactivated", target=name, via="ui")
    msg = _("Reactivated – now use “Rotate token”, the old token stays invalid.") if active else \
        _("Deactivated, the API token is invalid.")
    return _action(request, name, fn, msg)


@router.post("/{name}/gitea", dependencies=POST)
def set_gitea(request: Request, name: str, user: UiUser = Depends(require_admin),
              gitea_username: str = Form(...), gitea_token: str = Form(...), gitea_email: str = Form("")):
    def fn(s, actor):
        svc.set_gitea_credentials(s, actor, gitea_username, gitea_token, gitea_email)
        audit(user.name, "gitea_credentials_set", target=name, gitea_user=actor.gitea_username, via="ui")
    return _action(request, name, fn, _("Gitea token checked and stored encrypted."))


@router.post("/{name}/gitea/check", dependencies=POST)
def check_gitea(request: Request, name: str):
    return _action(request, name, lambda s, a: svc.check_gitea_credentials(s, a), _("Gitea token is valid."))


@router.post("/{name}/gitea/delete", dependencies=POST)
def delete_gitea(request: Request, name: str, user: UiUser = Depends(require_admin)):
    def fn(s, actor):
        svc.clear_gitea_credentials(s, actor)
        audit(user.name, "gitea_credentials_removed", target=name, via="ui")
    return _action(request, name, fn, _("Gitea credentials removed."))


@router.post("/{name}/sftp", dependencies=POST)
def set_sftp(request: Request, name: str, user: UiUser = Depends(require_admin),
             sftp_username: str = Form(...), sftp_password: str = Form(...)):
    from app.services import sftp

    def fn(s, actor):
        sftp.set_credentials(s, actor, sftp_username, sftp_password)
        audit(user.name, "sftp_credentials_set", target=name, sftp_user=actor.sftp_username, via="ui")
    return _action(request, name, fn, _("SFTP login stored encrypted – check it with “Test access”."))


@router.post("/{name}/sftp/check", dependencies=POST)
def check_sftp(request: Request, name: str, user: UiUser = Depends(require_admin)):
    from app.services import sftp

    try:
        with web_session() as s:
            detail = sftp.test_access(s, svc.get(s, name))
    except AppError as e:
        return _detail(request, name, 422, error=e.message)
    audit(user.name, "sftp_access_checked", target=name, via="ui")
    return _detail(request, name, message=_("SFTP access works: %(detail)s", detail=detail))


@router.post("/{name}/sftp/delete", dependencies=POST)
def delete_sftp(request: Request, name: str, user: UiUser = Depends(require_admin)):
    from app.services import sftp

    def fn(s, actor):
        sftp.clear_credentials(s, actor)
        audit(user.name, "sftp_credentials_removed", target=name, via="ui")
    return _action(request, name, fn, _("SFTP login removed."))


@router.post("/{name}/projects", dependencies=POST)
async def set_project_access(request: Request, name: str, user: UiUser = Depends(require_admin)):
    """Save project access and add or remove the Gitea collaborator per project."""
    from app.services import members

    form = await request.form()

    def fn(s, actor):
        rows = _access_rows(s, actor)
        # Validate everything before touching Gitea to avoid a partial update on error.
        for row in rows:
            if form.get(f"member_{row['project'].project_key}") == "1":
                members.check_privacy(row["project"], actor)
            if str(form.get(f"perm_{row['project'].project_key}") or "write") not in ("read", "write"):
                raise AppError(_( "Permission must be 'read' or 'write'" ))
        changes = []
        for row in rows:
            key = row["project"].project_key
            wanted = form.get(f"member_{key}") == "1"
            perm = str(form.get(f"perm_{key}") or "write")
            if wanted and (row["member"] is None or row["member"].gitea_permission != perm
                           or row["member"].sync_error):
                members.set_member(s, row["project"], actor, perm)
                changes.append(f"+{key}:{perm}")
            elif not wanted and row["member"] is not None:
                members.remove_member(s, row["project"], actor)
                changes.append(f"-{key}")
        audit(user.name, "project_access_updated", target=name, changes=changes, via="ui")
    return _action(request, name, fn, _("Project access saved."))
