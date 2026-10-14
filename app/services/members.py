"""Project access (`project_members`) and Gitea collaborator sync (spec section 11a.3).

Gitea errors never block the domain action: they go into `sync_error` and the
collaborator sync job retries them every 15 minutes.
"""
import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.i18n import _
from app.errors import Unprocessable
from app.models import Actor, Project, ProjectMember
from app.services import settings_store
from app.services.gitea import GiteaError

log = logging.getLogger(__name__)


def utcnow():
    from app.services.actors import utcnow as _now

    return _now()


def check_privacy(project: Project, actor: Actor) -> None:
    if project.privacy == "local_only" and actor.runtime == "cloud":
        raise Unprocessable(
            _("Project %(project)s is 'local_only' – cloud actor '%(actor)s' is not allowed (code and context would leave the local environment).", project=project.project_key, actor=actor.name), code="privacy_violation")


def _sync(session: Session, pm: ProjectMember, project: Project, actor: Actor) -> None:
    if not project.gitea_repo:
        pm.gitea_synced_at, pm.sync_error = None, None  # Synced when the repo is linked.
        return
    if not actor.gitea_username:
        pm.gitea_synced_at, pm.sync_error = None, _("Actor has no Gitea user configured")
        return
    try:
        settings_store.gitea_client(session).set_collaborator(
            project.gitea_owner, project.gitea_repo, actor.gitea_username, pm.gitea_permission)
        pm.gitea_synced_at, pm.sync_error = utcnow(), None
    except GiteaError as e:
        pm.gitea_synced_at, pm.sync_error = None, str(e)
        log.warning("collaborator_sync_failed", extra={"project": project.project_key, "actor": actor.name})


def set_member(session: Session, project: Project, actor: Actor, permission: str = "write") -> ProjectMember:
    if permission not in ("read", "write"):
        raise Unprocessable(_( "Permission must be 'read' or 'write'" ))
    check_privacy(project, actor)
    pm = session.get(ProjectMember, (project.id, actor.id))
    if pm is None:
        pm = ProjectMember(project_id=project.id, actor_id=actor.id, gitea_permission=permission)
        session.add(pm)
    pm.gitea_permission = permission
    if actor.is_active:
        _sync(session, pm, project, actor)
    session.flush()
    return pm


def ensure_member(session: Session, project: Project, actor: Actor) -> ProjectMember:
    """Create membership on assignment, preserving existing permission."""
    pm = session.get(ProjectMember, (project.id, actor.id))
    if pm is not None:
        return pm
    return set_member(session, project, actor, "write")


def remove_member(session: Session, project: Project, actor: Actor) -> None:
    pm = session.get(ProjectMember, (project.id, actor.id))
    if pm is None:
        return
    if project.gitea_repo and actor.gitea_username:
        try:
            settings_store.gitea_client(session).remove_collaborator(
                project.gitea_owner, project.gitea_repo, actor.gitea_username)
        except GiteaError as e:
            raise Unprocessable(_("Could not remove Gitea collaborator: %(error)s", error=e),
                                code="gitea_sync_failed") from e
    session.delete(pm)
    session.flush()


def list_for_project(session: Session, project: Project) -> list[tuple[ProjectMember, Actor]]:
    return list(session.execute(select(ProjectMember, Actor).join(Actor, Actor.id == ProjectMember.actor_id)
                                .where(ProjectMember.project_id == project.id).order_by(Actor.name)).all())


def revoke_all(session: Session, actor: Actor) -> None:
    """On deactivation, revoke collaborator rights in all repos but retain membership history."""
    rows = session.execute(select(ProjectMember, Project).join(Project, Project.id == ProjectMember.project_id)
                           .where(ProjectMember.actor_id == actor.id)).all()
    for pm, project in rows:
        pm.gitea_synced_at = None
        if not (project.gitea_repo and actor.gitea_username):
            continue
        try:
            settings_store.gitea_client(session).remove_collaborator(
                project.gitea_owner, project.gitea_repo, actor.gitea_username)
            pm.sync_error = None
        except GiteaError as e:
            pm.sync_error = _("Revocation failed: %(error)s", error=e)
    session.flush()


def mark_for_resync(session: Session, actor: Actor) -> None:
    for pm in session.scalars(select(ProjectMember).where(ProjectMember.actor_id == actor.id)):
        pm.gitea_synced_at, pm.sync_error = None, None
    session.flush()


def sync_project(session: Session, project: Project) -> None:
    for pm, actor in list_for_project(session, project):
        if actor.is_active:
            _sync(session, pm, project, actor)
    session.flush()


def sync_pending(session: Session) -> int:
    """Retry syncing pending or failed memberships of active actors."""
    rows = list(session.execute(
        select(ProjectMember, Project, Actor)
        .join(Project, Project.id == ProjectMember.project_id)
        .join(Actor, Actor.id == ProjectMember.actor_id)
        .where(Actor.is_active.is_(True), Project.gitea_repo.is_not(None),
               or_(ProjectMember.gitea_synced_at.is_(None), ProjectMember.sync_error.is_not(None)))).all())
    for pm, project, actor in rows:
        _sync(session, pm, project, actor)
    session.flush()
    return len(rows)


def sync_actor(session: Session, actor: Actor) -> None:
    """Sync all memberships of one actor now (e.g. right after its Gitea user was linked)."""
    if not actor.is_active:
        return
    for pm, project in session.execute(select(ProjectMember, Project)
                                       .join(Project, Project.id == ProjectMember.project_id)
                                       .where(ProjectMember.actor_id == actor.id)).all():
        _sync(session, pm, project, actor)
    session.flush()
