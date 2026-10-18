"""One-shot setup for the Docker Compose profile `gitea` (`python -m app.gitea_init`).

Idempotent; every run checks and completes what is missing:
1. wait for Gitea and check the admin login (the admin itself is created by the `gitea-admin` service with the
   Gitea CLI, because the API cannot create the first admin);
2. Gitea service user (TBR_GITEA_SERVICE_USER, else `taskbrigade`; an installation from before 0.8 whose stored token
   belongs to `taskmeister` keeps that user) and its token, stored in TaskBrigade together with the internal
   and the public Gitea URL, plus a token of the Gitea admin (scope `write:admin`) as `gitea_admin_token`, so
   Admin → Neuer Agent creates Gitea users for new agents (the service user stays without admin rights);
3. optionally (TBR_GITEA_DEMO, default on) the agent `demo-agent` with its own Gitea user, the project `DEMO`
   with the repository `<service user>/demo` and one example task assigned to that agent.

The native installation does not use this module.
"""
import logging
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings, getenv
from app.container import StartupError, database_url, wait_for_db
from app.db import make_engine
from app.logging_setup import setup_logging
from app.services.gitea import GiteaClient, GiteaError

log = logging.getLogger("taskbrigade.gitea_init")

TIMEOUT = httpx.Timeout(15.0)
TOKEN_NAME = "taskbrigade"
LEGACY_TOKEN_NAMES = {"taskbrigade": "taskmeister", "taskbrigade-admin": "taskmeister-admin"}  # Before 0.8.
# Scopes for the service user and the demo agent (repositories, collaborators, PRs, branch protection).
TOKEN_SCOPES = ["write:repository", "write:user", "write:organization", "write:issue", "write:misc"]
# Token of the Gitea admin for creating agent users (app/services/onboarding.py).
ADMIN_TOKEN_NAME = "taskbrigade-admin"
ADMIN_TOKEN_SCOPES = ["write:admin", "read:user"]
DEFAULT_SERVICE_USER = "taskbrigade"
LEGACY_SERVICE_USER = "taskmeister"  # Default before TaskBrigade 0.8, kept while the stored token belongs to it.
DEMO_AGENT = "demo-agent"
DEMO_PROJECT = "DEMO"
DEMO_REPO = "demo"
DEMO_TASK_TITLE = "Add a hello script"
DEMO_TASK_DESCRIPTION = (
    "Demo task created by the Docker Compose profile `gitea`.\n\n"
    "Add `hello.py` to the repository root that prints `Hello from TaskBrigade!`.")
DEMO_TASK_CRITERIA = "- `python hello.py` prints `Hello from TaskBrigade!`\n- Submitted as a pull request"


@dataclass
class InitConfig:
    gitea_url: str
    public_url: str
    admin_user: str
    admin_password: str
    service_user: str = ""  # Empty: DEFAULT_SERVICE_USER, or LEGACY_SERVICE_USER (configure_service).
    demo: bool = True
    taskbrigade_url: str = "http://127.0.0.1:8090"
    wait_seconds: int = 120

    @classmethod
    def from_env(cls, settings: Settings, environ: Mapping[str, str]) -> "InitConfig":
        url = settings.gitea_url.rstrip("/")
        return cls(
            gitea_url=url, public_url=(settings.gitea_public_url or url).rstrip("/"),
            admin_user=getenv("GITEA_ADMIN_USER", environ=environ) or "giteaadmin",
            admin_password=getenv("GITEA_ADMIN_PASSWORD", environ=environ) or "",
            service_user=getenv("GITEA_SERVICE_USER", environ=environ) or "",
            demo=(getenv("GITEA_DEMO", environ=environ) or "1").lower() not in ("0", "false", "no", "off"),
            taskbrigade_url=(getenv("PUBLIC_URL", environ=environ) or "http://127.0.0.1:8090").rstrip("/"),
            wait_seconds=settings.tbr_db_wait_seconds)


@dataclass
class InitResult:
    steps: list[str] = field(default_factory=list)
    agent_token: str | None = None  # Only when the demo agent was created in this run.


class GiteaAdmin:
    """The few admin calls the setup needs, with basic auth of the Gitea admin."""

    def __init__(self, base_url: str, user: str, password: str):
        self.base_url, self.auth = base_url.rstrip("/"), (user, password)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return httpx.request(method, f"{self.base_url}/api/v1{path}", auth=self.auth, timeout=TIMEOUT,
                                 headers={"Accept": "application/json"}, **kw)
        except httpx.HTTPError as e:
            raise GiteaError(f"Gitea unreachable: {type(e).__name__}") from e

    def version(self) -> str | None:
        """None while Gitea does not answer yet."""
        try:
            r = httpx.get(f"{self.base_url}/api/v1/version", timeout=TIMEOUT)
        except httpx.HTTPError:
            return None
        return (r.json() or {}).get("version") if r.status_code == 200 else None

    def check_admin(self) -> None:
        r = self._request("GET", "/user")
        if r.status_code == 401:
            raise GiteaError(f"Gitea rejects the admin login '{self.auth[0]}' (TBR_GITEA_ADMIN_PASSWORD)", 401)
        if r.status_code != 200 or not r.json().get("is_admin"):
            raise GiteaError(f"'{self.auth[0]}' is not a Gitea admin (HTTP {r.status_code})", r.status_code)

    def ensure_user(self, username: str) -> bool:
        """Create a local user with a random password (nobody signs in with it). Return True if created."""
        r = self._request("GET", f"/users/{quote(username, safe='')}")
        if r.status_code == 200:
            return False
        if r.status_code != 404:
            raise GiteaError(f"Gitea user {username}: HTTP {r.status_code}", r.status_code)
        r = self._request("POST", "/admin/users", json={
            "username": username, "email": f"{username}@noreply.localhost", "password": secrets.token_urlsafe(24),
            "must_change_password": False})
        if r.status_code != 201:
            raise GiteaError(f"Create Gitea user {username}: HTTP {r.status_code} – {r.text[:200]}", r.status_code)
        return True

    def new_token(self, username: str, name: str = TOKEN_NAME, scopes: list[str] = TOKEN_SCOPES) -> str:
        """Replace the token `name` of `username` (an admin may manage other users' tokens)."""
        base = f"/users/{quote(username, safe='')}/tokens"
        for old in (name, LEGACY_TOKEN_NAMES.get(name)):
            if not old:
                continue
            r = self._request("DELETE", f"{base}/{old}")
            if r.status_code not in (204, 404):
                raise GiteaError(f"Delete token of {username}: HTTP {r.status_code}", r.status_code)
        r = self._request("POST", base, json={"name": name, "scopes": scopes})
        if r.status_code != 201:
            raise GiteaError(f"Create token for {username}: HTTP {r.status_code} – {r.text[:200]}", r.status_code)
        return r.json()["sha1"]


def wait_for_gitea(admin: GiteaAdmin, timeout: float, interval: float = 2.0, *,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> str:
    deadline = clock() + timeout
    while True:
        version = admin.version()
        if version:
            return version
        if clock() >= deadline:
            raise StartupError(f"Gitea at {admin.base_url} not reachable after {timeout:.0f} s")
        sleep(interval)


def _token_user(base_url: str, token: str | None):
    if not token:
        return None
    try:
        return GiteaClient(base_url, token).current_user()
    except GiteaError:
        return None


def _token_belongs_to(base_url: str, token: str | None, login: str, admin: bool = False) -> bool:
    user = _token_user(base_url, token)
    return user is not None and user.login.lower() == login.lower() and (user.is_admin or not admin)


def configure_service(session: Session, admin: GiteaAdmin, cfg: InitConfig, result: InitResult) -> None:
    """Service user, token, internal/public URL and default owner in TaskBrigade's settings."""
    from app.models import AppSetting
    from app.services import settings_store

    if not cfg.service_user:  # An installation from before 0.8 keeps its service user; never a second one next to it.
        current = _token_user(cfg.gitea_url, settings_store.get(session, "gitea_token"))
        legacy = current is not None and current.login.lower() == LEGACY_SERVICE_USER
        cfg.service_user = LEGACY_SERVICE_USER if legacy else DEFAULT_SERVICE_USER
    if admin.ensure_user(cfg.service_user):
        result.steps.append(f"Gitea user {cfg.service_user} created")
    values: dict[str, str | None] = {}
    # Stored in the database, not compared with get(): the app container does not see this container's env.
    for key, value in (("gitea_url", cfg.gitea_url), ("gitea_public_url", cfg.public_url)):
        row = session.get(AppSetting, key)
        if row is None or row.setting_value != value:
            values[key] = value
    if not settings_store.get(session, "gitea_default_owner"):
        values["gitea_default_owner"] = cfg.service_user
    if not _token_belongs_to(cfg.gitea_url, settings_store.get(session, "gitea_token"), cfg.service_user):
        values["gitea_token"] = admin.new_token(cfg.service_user)
        result.steps.append(f"Service token for {cfg.service_user} stored")
    if not _token_belongs_to(cfg.gitea_url, settings_store.get(session, "gitea_admin_token"), cfg.admin_user,
                             admin=True):
        values["gitea_admin_token"] = admin.new_token(cfg.admin_user, ADMIN_TOKEN_NAME, ADMIN_TOKEN_SCOPES)
        result.steps.append(f"Gitea admin token for new agents stored ({cfg.admin_user})")
    if values:
        settings_store.update(session, values, None)
        result.steps.append("Settings updated: " + ", ".join(sorted(values)))


def ensure_demo(session: Session, admin: GiteaAdmin, cfg: InitConfig, settings: Settings,
                result: InitResult) -> None:
    """Demo agent with Gitea credentials, project DEMO with repository and one example task."""
    from app import crypto
    from app.models import Actor, Project, Task
    from app.services import actors, members, projects, tasks

    owner = session.scalar(select(Actor).where(Actor.name == settings.bootstrap_admin_actor))
    if owner is None:
        raise StartupError(f"Bootstrap admin '{settings.bootstrap_admin_actor}' missing – demo skipped")
    agent = session.scalar(select(Actor).where(Actor.name == DEMO_AGENT))
    if agent is None:
        created = actors.create(session, name=DEMO_AGENT, display_name="Demo agent", actor_type="agent",
                                agent_kind="other", runtime="local", model_location="local",  # DEMO is local_only
                                notes="Created by the Docker Compose profile gitea")
        agent, result.agent_token = created.actor, created.token
        result.steps.append(f"Agent {DEMO_AGENT} created")
    if admin.ensure_user(DEMO_AGENT):
        result.steps.append(f"Gitea user {DEMO_AGENT} created")
    token = crypto.decrypt(actors._fernet(), agent.gitea_token_enc) if agent.gitea_token_enc else None
    if agent.gitea_username != DEMO_AGENT or not _token_belongs_to(cfg.gitea_url, token, DEMO_AGENT):
        actors.set_gitea_credentials(session, agent, DEMO_AGENT, admin.new_token(DEMO_AGENT), None)
        result.steps.append(f"Gitea credentials of {DEMO_AGENT} stored")

    project = session.scalar(select(Project).where(Project.project_key == DEMO_PROJECT))
    if project is None:
        project, setup = projects.create(
            session, owner, key=DEMO_PROJECT, name="Demo", status="active", privacy="local_only",
            description="Demo project of the Docker Compose profile gitea", gitea_mode="create",
            gitea_owner=cfg.service_user, gitea_repo=DEMO_REPO)
        result.steps.append(f"Project {DEMO_PROJECT} created")
        if setup is not None and not setup.ok:
            log.warning("demo_repo_incomplete", extra={"error": setup.error})
    members.ensure_member(session, project, agent)
    if not session.scalar(select(func.count()).select_from(Task).where(Task.project_id == project.id)):
        task = tasks.create(session, owner, project=DEMO_PROJECT, title=DEMO_TASK_TITLE,
                            description=DEMO_TASK_DESCRIPTION, acceptance_criteria=DEMO_TASK_CRITERIA,
                            assigned_to=DEMO_AGENT)
        result.steps.append(f"Task {task.task_nr} created")


def run(settings: Settings, cfg: InitConfig, *, sleep: Callable[[float], None] = time.sleep) -> InitResult:
    if not cfg.admin_password:
        raise StartupError("TBR_GITEA_ADMIN_PASSWORD is not set")
    admin = GiteaAdmin(cfg.gitea_url, cfg.admin_user, cfg.admin_password)
    version = wait_for_gitea(admin, cfg.wait_seconds, sleep=sleep)
    log.info("gitea_reachable", extra={"version": version, "url": cfg.gitea_url})
    result = InitResult()
    engine = make_engine(database_url(settings))
    try:
        before = wait_for_db(engine, cfg.wait_seconds, sleep=sleep)
        if not before.schema_current:
            raise StartupError("Database schema is not current – start the taskbrigade service first")
        admin.check_admin()
        with Session(engine) as session:
            configure_service(session, admin, cfg, result)
            if cfg.demo:
                ensure_demo(session, admin, cfg, settings, result)
            session.commit()
    except GiteaError as e:
        raise StartupError(f"Gitea: {e}") from e
    finally:
        engine.dispose()
    return result


def summary(cfg: InitConfig, result: InitResult) -> str:
    lines = ["TaskBrigade + Gitea are ready.",
             f"  TaskBrigade: {cfg.taskbrigade_url}",
             f"  Gitea:       {cfg.public_url} (admin: {cfg.admin_user}, internal: {cfg.gitea_url})"]
    lines += [f"  - {step}" for step in result.steps] or ["  - Nothing to do, everything was already set up."]
    if result.agent_token:
        lines += ["", f"API token of {DEMO_AGENT} (shown once; rotate it under Admin → Benutzer & Agenten):",
                  f"  {result.agent_token}",
                  "Agent machine, e.g. in an empty working directory:",
                  f'  printf \'url = "{cfg.taskbrigade_url}"\\ntoken = "{result.agent_token}"\\n\' > .taskbrigade.toml',
                  f"  tbr prepare --task {DEMO_PROJECT}-0001"]
    return "\n".join(lines)


def main() -> None:
    setup_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    cfg = InitConfig.from_env(settings, os.environ)
    try:
        result = run(settings, cfg)
    except StartupError as e:
        log.error("gitea_init_failed", extra={"error": str(e)})
        raise SystemExit(1) from e
    print(summary(cfg, result), flush=True)


if __name__ == "__main__":
    main()
