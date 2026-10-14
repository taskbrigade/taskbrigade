"""Projects including Gitea setup (spec section 9)."""
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.i18n import _
from app.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.models import PROJECT_STATUSES, TASK_STATUSES, Actor, Project, Task
from app.services import access, members, settings_store
from app.services.gitea import GiteaError

KEY_RE = re.compile(r"^[A-Z0-9]{2,8}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
REPO_FILES = ("brain.md", "AGENTS.md", "CLAUDE.md", "brain-changes/README.md")
OPEN_STATUSES = ("draft", "defined", "in_progress", "questions", "aborted", "for_review", "changes_requested")

_repo_env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "templates" / "repo"),
                        keep_trailing_newline=True)


def render_repo_file(name: str, **ctx) -> str:
    return _repo_env.get_template(name).render(**ctx)


@dataclass
class SetupResult:
    ok: bool = True
    steps: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "steps": self.steps, "error": self.error}


def get(session: Session, key: str, for_update: bool = False) -> Project:
    stmt = select(Project).where(Project.project_key == key.upper())
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    project = session.scalar(stmt)
    if project is None:
        raise NotFound(_("Project '%(key)s' not found", key=key))
    return project


def list_all(session: Session, status: str | None = None) -> list[Project]:
    stmt = select(Project).order_by(Project.project_key)
    if status:
        stmt = stmt.where(Project.status.in_(status.split(",")))
    return list(session.scalars(stmt))


def task_counts(session: Session, project_ids: list[int] | None = None) -> dict[int, dict[str, int]]:
    stmt = select(Task.project_id, Task.status, func.count()).group_by(Task.project_id, Task.status)
    if project_ids is not None:
        stmt = stmt.where(Task.project_id.in_(project_ids))
    out: dict[int, dict[str, int]] = {}
    for pid, status, n in session.execute(stmt):
        out.setdefault(pid, dict.fromkeys(TASK_STATUSES, 0))[status] = n
    return out


def _require_admin(actor: Actor) -> None:
    if actor.role != "admin":
        raise Forbidden(_( 'Only admins can create or edit projects' ))


def _require_creator(actor: Actor) -> None:
    if actor.role != "admin" and not (actor.actor_type == "human" and actor.role == "member"):
        raise Forbidden(_( 'Only admins and members can create projects' ))


def resolve_owner(session: Session, actor: Actor, owner: str | None) -> Actor:
    """A project's owner is always an active human; default to its creator."""
    if owner:
        person = session.scalar(select(Actor).where(Actor.name == owner.strip()))
        if person is None or person.actor_type != "human" or not person.is_active:
            raise Unprocessable(_("Owner '%(owner)s' is not an active human in TaskBrigade", owner=owner))
        return person
    if actor.actor_type == "human":
        return actor
    raise Unprocessable(_( 'Specify an owner – projects must belong to a human' ))


def owner_collaborator(session: Session, project: Project) -> None:
    """Add an owner with a Gitea user as a collaborator so they can review pull requests."""
    owner = session.get(Actor, project.owner_id)
    if owner is not None and owner.gitea_username and project.gitea_repo:
        members.ensure_member(session, project, owner)


def owner_name(session: Session, project: Project) -> str | None:
    owner = session.get(Actor, project.owner_id) if project.owner_id else None
    return owner.name if owner else None


def _clone_url(session: Session, project: Project) -> str | None:
    if not project.gitea_repo:
        return None
    base = settings_store.gitea_public_url(session)
    return f"{base}/{project.gitea_owner}/{project.gitea_repo}.git"


def info(session: Session, project: Project) -> dict:
    """Project details for task payloads and the API."""
    return {"key": project.project_key, "name": project.name, "status": project.status,
            "privacy": project.privacy, "gitea_owner": project.gitea_owner, "gitea_repo": project.gitea_repo,
            "repo_url": project.gitea_url, "clone_url": _clone_url(session, project),
            "default_branch": project.default_branch, "brain_path": project.brain_path,
            "owner": owner_name(session, project), "serialize_reviews": bool(project.serialize_reviews),
            "default_reviewer": default_reviewer_name(session, project)}


def default_reviewer_name(session: Session, project: Project) -> str | None:
    reviewer = session.get(Actor, project.default_reviewer_id) if project.default_reviewer_id else None
    return reviewer.name if reviewer else None


def create(session: Session, actor: Actor, *, key: str, name: str, description: str | None = None,
           status: str = "idea", privacy: str = "local_only", gitea_mode: str = "none",
           gitea_owner: str | None = None, gitea_repo: str | None = None, add_missing_files: bool = True,
           protect_branch: bool | None = None, owner: str | None = None) -> tuple[Project, SetupResult | None]:
    _require_creator(actor)
    owner_actor = resolve_owner(session, actor, owner)
    if actor.role != "admin" and owner_actor.id != actor.id:
        raise Forbidden(_( 'Members can only create projects for themselves' ))
    key = key.strip().upper()
    if not KEY_RE.match(key):
        raise Unprocessable(_("Project key: 2–8 characters A-Z/0-9"))
    if status not in PROJECT_STATUSES or privacy not in ("local_only", "cloud_ok"):
        raise Unprocessable(_( 'Invalid status or privacy value' ))
    if gitea_mode not in ("create", "link", "none"):
        raise Unprocessable(_( "gitea_mode must be 'create', 'link', or 'none'" ))
    if gitea_mode == "none" and status == "active":
        raise Unprocessable(_("Active projects require a Gitea repository (ideas without one: status 'idea')"))
    if not name.strip():
        raise Unprocessable(_( 'Name is required' ))
    if session.scalar(select(Project.id).where(Project.project_key == key)):
        raise Conflict(_("Project key '%(key)s' is already in use", key=key))

    project = Project(project_key=key, name=name.strip(), description=description or None, status=status,
                      privacy=privacy, created_by=actor.id, owner_id=owner_actor.id, next_task_seq=1,
                      default_branch="main",
                      brain_path="brain.md")
    session.add(project)
    session.flush()
    result = None
    if gitea_mode != "none":
        result = setup_gitea(session, project, gitea_mode, gitea_owner, gitea_repo, add_missing_files,
                             protect_branch)
    return project, result


def setup_gitea(session: Session, project: Project, mode: str, owner: str | None, repo: str | None,
                add_missing_files: bool = True, protect_branch: bool | None = None) -> SetupResult:
    """Create or link a repo, add missing files, and set branch protection.

    Errors creating or finding the repo raise an exception (nothing is saved). Later errors
    (files or protection) leave the project saved and appear in the result (repeat setup)."""
    if mode not in ("create", "link"):
        raise Unprocessable(_( "mode must be 'create' or 'link'" ))
    client = settings_store.gitea_client(session)
    owner = (owner or settings_store.get(session, "gitea_default_owner") or "").strip()
    repo = (repo or (project.gitea_repo if mode == "link" else None) or project.project_key.lower()).strip()
    if not owner:
        raise Unprocessable(_("Gitea owner missing (set the default owner under Admin → Settings)"))
    if not REPO_RE.match(repo):
        raise Unprocessable(_( 'Invalid repository name' ))
    other = session.scalar(select(Project).where(Project.gitea_owner == owner, Project.gitea_repo == repo,
                                                 Project.id != project.id))
    if other:
        raise Conflict(_("Repository %(owner)s/%(repo)s is already linked to project %(project)s", owner=owner, repo=repo, project=other.project_key))

    result = SetupResult()
    try:
        existing = client.get_repo(owner, repo)
        if mode == "link" and existing is None:
            raise Unprocessable(_("Repository %(owner)s/%(repo)s does not exist in Gitea", owner=owner, repo=repo), code="repo_not_found")
        if existing is None:
            existing = client.create_repo(owner, repo, project.description or project.name)
            result.steps.append(_("Repository %(owner)s/%(repo)s created", owner=owner, repo=repo))
        else:
            result.steps.append(_("Repository %(owner)s/%(repo)s linked", owner=owner, repo=repo))
    except GiteaError as e:
        raise Unprocessable(f"Gitea: {e}", code="gitea_error") from e

    project.gitea_owner, project.gitea_repo = existing.owner, existing.name
    project.gitea_url = settings_store.public_gitea_link(session, existing.html_url)
    project.default_branch = existing.default_branch or "main"
    session.flush()

    try:
        if mode == "create" or add_missing_files:
            missing = [f for f in REPO_FILES if not client.file_exists(owner, repo, f, project.default_branch)]
            files = {f: render_repo_file(f, project=project) for f in missing}
            if mode == "create" and not client.file_exists(owner, repo, "tasks/.gitkeep", project.default_branch):
                files["tasks/.gitkeep"] = ""
            if files:
                client.create_files(owner, repo, files, _("TaskBrigade: initial files"), project.default_branch)
                result.steps.append(_("Files added: %(files)s", files=", ".join(files)))
        protect = protect_branch if protect_branch is not None else (
            mode == "create" and settings_store.get_bool(session, "branch_protection"))
        if protect:
            client.protect_branch(owner, repo, project.default_branch, client.current_user().login)
            result.steps.append(_("Branch protection enabled on %(branch)s", branch=project.default_branch))
    except GiteaError as e:
        result.ok, result.error = False, str(e)
    owner_collaborator(session, project)
    members.sync_project(session, project)
    return result


def missing_files(session: Session, project: Project) -> list[str]:
    if not project.gitea_repo:
        return []
    client = settings_store.gitea_client(session)
    return [f for f in REPO_FILES
            if not client.file_exists(project.gitea_owner, project.gitea_repo, f, project.default_branch)]


def update(session: Session, actor: Actor, project: Project, changes: dict) -> list[str]:
    access.require_manage_project(project, actor)
    changes = dict(changes)
    new_owner = None
    if "owner" in changes:  # An admin or current owner may transfer ownership to an active human.
        new_owner = resolve_owner(session, actor, changes.pop("owner") or None)
    reviewer_changed = "default_reviewer" in changes
    if reviewer_changed:
        from app.services import reviews

        reviewer = reviews.find(session, changes.pop("default_reviewer"))
        if reviewer is not None and reviewer.id != project.default_reviewer_id:
            access.require_may_use_agent(actor, reviewer)
        if reviewer is not None:
            reviews.check(project if "privacy" not in changes else Project(privacy=changes["privacy"],
                                                                           project_key=project.project_key),
                          reviewer, None)
    allowed = {"name", "description", "status", "privacy", "serialize_reviews"}
    if set(changes) - allowed:
        raise Unprocessable(_("Cannot change: %(fields)s", fields=", ".join(sorted(set(changes) - allowed))))
    if "status" in changes:
        if changes["status"] not in PROJECT_STATUSES:
            raise Unprocessable(_( 'Invalid status' ))
        if changes["status"] == "active" and not project.gitea_repo:
            raise Unprocessable(_( 'Create or link a Gitea repository before activating' ))
    if changes.get("privacy") == "local_only" and project.privacy != "local_only":
        cloud = session.scalar(select(func.count()).select_from(Task).join(Actor, Actor.id == Task.assigned_to)
                               .where(Task.project_id == project.id, Task.status.in_(OPEN_STATUSES),
                                      Actor.runtime == "cloud"))
        if cloud:
            raise Unprocessable(_("%(count)d open task(s) are assigned to cloud agents – reassign them first", count=cloud),
                                code="privacy_violation")
    elif changes.get("privacy") not in (None, "local_only", "cloud_ok"):
        raise Unprocessable(_( "Privacy must be 'local_only' or 'cloud_ok'" ))
    if "name" in changes and not (changes["name"] or "").strip():
        raise Unprocessable(_( 'Name must not be empty' ))
    changed = []
    for k, v in changes.items():
        if getattr(project, k) != v:
            setattr(project, k, v)
            changed.append(k)
    if reviewer_changed and (reviewer.id if reviewer else None) != project.default_reviewer_id:
        project.default_reviewer_id = reviewer.id if reviewer else None
        changed.append("default_reviewer")
    if new_owner is not None and new_owner.id != project.owner_id:
        project.owner_id = new_owner.id
        changed.append("owner")
        owner_collaborator(session, project)
    session.flush()
    return changed


def brain(session: Session, project: Project) -> str:
    if not project.gitea_repo:
        raise NotFound(_( 'Project has no Gitea repository' ))
    try:
        text = settings_store.gitea_client(session).raw_file(project.gitea_owner, project.gitea_repo,
                                                             project.brain_path, project.default_branch)
    except GiteaError as e:
        raise Unprocessable(f"Gitea: {e}", code="gitea_error") from e
    if text is None:
        raise NotFound(_("%(path)s does not exist in the repository", path=project.brain_path))
    return text
