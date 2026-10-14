"""/api/v1/projects"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import current_actor, SessionDep
from app.logging_setup import audit
from app.models import Actor
from app.schemas import ProjectCreate, ProjectGitea, ProjectPatch
from app.services import access, members, projects

router = APIRouter(prefix="/projects", tags=["projects"])


def _out(session: Session, project, counts: dict | None = None, setup=None) -> dict:
    out = {**projects.info(session, project), "description": project.description,
           "created_at": project.created_at, "updated_at": project.updated_at}
    if counts is not None:
        out["task_counts"] = counts
    if setup is not None:
        out["gitea_setup"] = setup.as_dict()
    return out


@router.get("")
def list_projects(status: str | None = Query(None), session: Session = SessionDep,
                  _: Actor = Depends(current_actor)):
    items = projects.list_all(session, status)
    counts = projects.task_counts(session, [p.id for p in items])
    return [_out(session, p, counts.get(p.id, {})) for p in items]


@router.post("", status_code=201)
def create_project(body: ProjectCreate, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    project, setup = projects.create(session, me, **body.model_dump())
    audit(me.name, "project_created", project=project.project_key, gitea_mode=body.gitea_mode,
          setup_ok=setup.ok if setup else None)
    return _out(session, project, setup=setup)


@router.get("/{key}")
def get_project(key: str, session: Session = SessionDep, _: Actor = Depends(current_actor)):
    project = projects.get(session, key)
    return _out(session, project, projects.task_counts(session, [project.id]).get(project.id, {}))


@router.patch("/{key}")
def patch_project(key: str, body: ProjectPatch, session: Session = SessionDep,
                  me: Actor = Depends(current_actor)):
    project = projects.get(session, key)
    changed = projects.update(session, me, project, body.model_dump(exclude_unset=True))
    audit(me.name, "project_updated", project=project.project_key, fields=changed)
    return _out(session, project)


@router.post("/{key}/gitea")
def project_gitea(key: str, body: ProjectGitea, session: Session = SessionDep,
                  me: Actor = Depends(current_actor)):
    """Create or link a repo later (promote an idea), or repeat setup."""
    project = projects.get(session, key)
    access.require_manage_project(project, me)
    setup = projects.setup_gitea(session, project, body.mode, body.gitea_owner, body.gitea_repo,
                                 body.add_missing_files, body.protect_branch)
    if body.activate:
        projects.update(session, me, project, {"status": "active"})
    audit(me.name, "project_gitea", project=project.project_key, mode=body.mode, ok=setup.ok)
    return _out(session, project, setup=setup)


@router.get("/{key}/brain", response_class=PlainTextResponse)
def project_brain(key: str, session: Session = SessionDep, _: Actor = Depends(current_actor)):
    return projects.brain(session, projects.get(session, key))


@router.get("/{key}/members")
def list_members(key: str, session: Session = SessionDep, _: Actor = Depends(current_actor)):
    project = projects.get(session, key)
    return [{"actor": a.name, "gitea_permission": pm.gitea_permission, "gitea_synced_at": pm.gitea_synced_at,
             "sync_error": pm.sync_error} for pm, a in members.list_for_project(session, project)]
