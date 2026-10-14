"""/api/v1/projects/{key}/sprints – sprints per project: list, create, edit, release, close, batch creation."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_actor, SessionDep
from app.logging_setup import audit
from app.models import Actor, Project, Task
from app.schemas import SprintBatch, SprintClose, SprintCreate, SprintPatch
from app.services import projects, sprints
from app.services import tasks as task_svc

router = APIRouter(prefix="/projects/{key}/sprints", tags=["sprints"])


def _sprint(session: Session, key: str, sprint_id: int, for_update: bool = False):
    project = projects.get(session, key)
    return project, sprints.get(session, sprint_id, project, for_update=for_update)


def _tasks(session: Session, sprint, project: Project) -> list[dict]:
    rows = list(session.scalars(select(Task).where(Task.sprint_id == sprint.id).order_by(Task.prio, Task.id)))
    names = task_svc._names(session, {t.assigned_to for t in rows})
    return [task_svc.summary(t, project.project_key, names) for t in rows]


@router.get("")
def list_sprints(key: str, status: list[str] | None = Query(None), session: Session = SessionDep,
                 _: Actor = Depends(current_actor)):
    items = sprints.list_for_project(session, projects.get(session, key), sprints.parse_statuses(status))
    counts = sprints.task_counts(session, [s.id for s in items])
    return [sprints.info(session, s, counts[s.id]) for s in items]


@router.post("", status_code=201)
def create_sprint(key: str, body: SprintCreate, session: Session = SessionDep,
                  me: Actor = Depends(current_actor)):
    sprint = sprints.create(session, me, projects.get(session, key), **body.model_dump())
    audit(me.name, "sprint_created", project=key.upper(), sprint=sprint.id)
    return sprints.info(session, sprint)


@router.get("/{sprint_id}")
def get_sprint(key: str, sprint_id: int, session: Session = SessionDep, _: Actor = Depends(current_actor)):
    project, sprint = _sprint(session, key, sprint_id)
    return {**sprints.info(session, sprint), "tasks": _tasks(session, sprint, project)}


@router.patch("/{sprint_id}")
def patch_sprint(key: str, sprint_id: int, body: SprintPatch, session: Session = SessionDep,
                 me: Actor = Depends(current_actor)):
    _project, sprint = _sprint(session, key, sprint_id, for_update=True)
    sprints.update(session, me, sprint, body.model_dump(exclude_unset=True))
    return sprints.info(session, sprint)


@router.post("/{sprint_id}/release")
def release_sprint(key: str, sprint_id: int, session: Session = SessionDep,
                   me: Actor = Depends(current_actor)):
    _project, sprint = _sprint(session, key, sprint_id, for_update=True)
    result = sprints.release(session, me, sprint)
    audit(me.name, "sprint_released", project=key.upper(), sprint=sprint.id, released=len(result["released"]),
          blocked=len(result["blocked"]))
    return {"sprint": sprints.info(session, sprint), **result}


@router.post("/{sprint_id}/close")
def close_sprint(key: str, sprint_id: int, body: SprintClose, session: Session = SessionDep,
                 me: Actor = Depends(current_actor)):
    _project, sprint = _sprint(session, key, sprint_id, for_update=True)
    result = sprints.close(session, me, sprint, body.move_to, body.detach)
    audit(me.name, "sprint_closed", project=key.upper(), sprint=sprint.id, moved_to=result["moved_to"])
    return {"sprint": sprints.info(session, sprint), **result}


@router.post("/{sprint_id}/tasks", status_code=201)
def batch_create(key: str, sprint_id: int, body: SprintBatch, session: Session = SessionDep,
                 me: Actor = Depends(current_actor)):
    """Create several drafts in the sprint in one transaction (all or nothing)."""
    project, sprint = _sprint(session, key, sprint_id, for_update=True)
    created = sprints.batch_create(session, me, sprint, [e.model_dump() for e in body.tasks])
    names = task_svc._names(session, {t.assigned_to for t in created})
    return {"sprint": sprints.info(session, sprint),
            "created": [task_svc.summary(t, project.project_key, names) for t in created]}
