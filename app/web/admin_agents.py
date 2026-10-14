"""Admin → Neuer Agent (new agent): wizard that creates the agent, its Gitea user (with the optional Gitea admin
token, otherwise a guided manual step) and a one-time join code for `tbr join`."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request

from app.auth import UiUser, csrf_protect, require_admin
from app.errors import AppError
from app.i18n import _
from app.logging_setup import audit
from app.models import AGENT_KINDS, Actor, Project
from app.services import actors as svc
from app.services import members, onboarding, settings_store
from app.web.common import render, web_session
from app.web.downloads import server_url

router = APIRouter(prefix="/admin/agents", dependencies=[Depends(require_admin)])
POST = [Depends(csrf_protect)]
AGENT_ROLES = ("worker", "reviewer", "orchestrator", "admin")


def _me(session, user: UiUser) -> Actor | None:
    return session.get(Actor, user.actor_id) if user.actor_id else None


def _projects(session) -> list[Project]:
    from sqlalchemy import select

    return list(session.scalars(select(Project).where(Project.status != "archived").order_by(Project.project_key)))


# Agent kinds `tbr join --setup` sets up completely (cli/setup.py, cli/setup_apps.py); desktop apps have no kind.
SETUP_KINDS = {"claude_code": "claude-code", "codex": "codex", "opencode": "opencode", "hermes": "hermes",
               "openclaw": "openclaw"}


def join_commands(base_url: str, name: str, code: str, agent_kind: str | None = None) -> dict[str, str]:
    """Commands for the agent machine: install tbr (if missing) and join – including tbr setup where the kind is known."""
    setup = f" --setup {SETUP_KINDS[agent_kind]}" if agent_kind in SETUP_KINDS else ""
    return {"install": f"curl -fsSL {base_url}/install.sh | sh",
            "join": f"tbr join {base_url} {code} --dir ~/taskbrigade-work/{name}{setup}"}


def _setup(request: Request, s, actor: Actor, status: int = 200, code: str | None = None,
           expires: datetime | None = None, **ctx):
    base = server_url(request)
    pending = onboarding.pending_code(s, actor)
    return render(request, "admin/agent_setup.html", status, actor=actor, section="users", code=code,
                  expires=expires, commands=join_commands(base, actor.name, code, actor.agent_kind) if code else None,
                  pending=pending, base_url=base, gitea_auto=onboarding.gitea_admin_client(s) is not None,
                  gitea_configured=bool(settings_store.get(s, "gitea_url")),
                  join_minutes=onboarding.join_minutes(s), **ctx)


def _form(request: Request, status: int = 200, **ctx):
    from app.web.admin_users import humans

    with web_session() as s:
        projects = _projects(s)
        gitea_auto = onboarding.gitea_admin_client(s) is not None
        names = humans(s)
    return render(request, "admin/agent_new.html", status, kinds=AGENT_KINDS, roles=AGENT_ROLES, projects=projects,
                  gitea_auto=gitea_auto, humans=names, section="users", **{"form": {}, **ctx})


@router.get("/new")
def new_form(request: Request, user: UiUser = Depends(require_admin)):
    return _form(request, form={"owner": user.name if user.actor_id else ""})  # Owner: the creating human.


@router.post("/new", dependencies=POST)
async def create(request: Request, user: UiUser = Depends(require_admin), name: str = Form(...),
                 display_name: str = Form(""), agent_kind: str = Form(...), runtime: str = Form("local"),
                 role: str = Form("worker"), owner: str = Form(""), is_public: bool = Form(False)):
    from app.web.admin_users import owner_from_form

    form_data = await request.form()
    selected = [str(k) for k in form_data.getlist("projects")]
    form = {"name": name, "display_name": display_name, "agent_kind": agent_kind, "runtime": runtime, "role": role,
            "projects": selected, "owner": owner, "is_public": is_public}
    try:
        if role not in AGENT_ROLES:
            raise AppError(_("Role must be one of %(roles)s", roles=", ".join(AGENT_ROLES)))
        with web_session() as s:
            projects = [p for p in _projects(s) if p.project_key in selected]
            created = svc.create(s, name=name, display_name=display_name or name, actor_type="agent",
                                 agent_kind=agent_kind, role=role, runtime=runtime, issue_token=False,
                                 owner=owner_from_form(s, owner, _me(s, user)), is_public=is_public)
            actor = created.actor
            for project in projects:  # Before Gitea: a privacy violation rolls everything back.
                members.check_privacy(project, actor)
            audit(user.name, "actor_created", target=actor.name, via="ui-wizard")
            gitea_error = None
            if onboarding.gitea_admin_client(s) is not None:
                try:
                    onboarding.provision_gitea(s, actor)
                    audit(user.name, "gitea_user_provisioned", target=actor.name, gitea_user=actor.gitea_username,
                          via="ui")
                except AppError as e:
                    gitea_error = e.message
            for project in projects:
                members.set_member(s, project, actor, "write")
            if projects:
                audit(user.name, "project_access_updated", target=actor.name,
                      changes=[f"+{p.project_key}:write" for p in projects], via="ui-wizard")
            code, expires = onboarding.create_code(s, actor, _me(s, user))
            audit(user.name, "join_code_created", target=actor.name, expires_at=expires.isoformat(), via="ui")
            return _setup(request, s, actor, code=code, expires=expires, created=True, gitea_error=gitea_error)
    except AppError as e:
        return _form(request, 422, form=form, error=e.message)


@router.get("/{name}/setup")
def setup(request: Request, name: str):
    with web_session() as s:
        return _setup(request, s, svc.get(s, name))


def _action(request: Request, name: str, fn, ok_message: str):
    """Run `fn(session, actor)`, then show the setup page with its result or error."""
    try:
        with web_session() as s:
            actor = svc.get(s, name)
            extra = fn(s, actor) or {}
    except AppError as e:
        with web_session() as s:
            return _setup(request, s, svc.get(s, name), 422, error=e.message)
    with web_session() as s:
        return _setup(request, s, svc.get(s, name), message=ok_message, **extra)


@router.post("/{name}/join-code", dependencies=POST)
def new_code(request: Request, name: str, user: UiUser = Depends(require_admin)):
    def fn(s, actor):
        code, expires = onboarding.create_code(s, actor, _me(s, user))
        audit(user.name, "join_code_created", target=name, expires_at=expires.isoformat(), via="ui")
        return {"code": code, "expires": expires}
    return _action(request, name, fn, _("New join code created; earlier unused codes are invalid."))


@router.post("/{name}/gitea/provision", dependencies=POST)
def provision(request: Request, name: str, user: UiUser = Depends(require_admin)):
    def fn(s, actor):
        onboarding.provision_gitea(s, actor)
        audit(user.name, "gitea_user_provisioned", target=name, gitea_user=actor.gitea_username, via="ui")
    return _action(request, name, fn, _("Gitea user and token created."))


@router.post("/{name}/gitea", dependencies=POST)
def link_gitea(request: Request, name: str, user: UiUser = Depends(require_admin),
               gitea_username: str = Form(...), gitea_token: str = Form(...), gitea_email: str = Form("")):
    def fn(s, actor):
        svc.set_gitea_credentials(s, actor, gitea_username, gitea_token, gitea_email)
        members.sync_actor(s, actor)
        audit(user.name, "gitea_credentials_set", target=name, gitea_user=actor.gitea_username, via="ui")
    return _action(request, name, fn, _("Gitea token checked and stored encrypted."))


@router.post("/{name}/gitea/check", dependencies=POST)
def check_gitea(request: Request, name: str):
    return _action(request, name, lambda s, a: svc.check_gitea_credentials(s, a) and None, _("Gitea token is valid."))
