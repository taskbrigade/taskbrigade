"""/api/v1/runner/jobs/* – endpoints for the data-zone runner (`app/services/runner_jobs.py`). Only runner agents
(`model_location = none`) holding the task get answers; everything else is refused."""
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import SessionDep, current_actor
from app.models import Actor, Task
from app.schemas import RunFailIn, RunResultIn
from app.services import runner_jobs

router = APIRouter(prefix="/runner/jobs", tags=["runner"])


@router.get("/{task_nr}")
def job(task_nr: str, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Job of the runner's in-progress task: dataset location and wrapped key, script reference, limits."""
    return runner_jobs.job(session, me, task_nr)


@router.get("/{task_nr}/script")
def script(task_nr: str, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """The task's script at its merged commit (fetched from Gitea with the service token)."""
    return Response(runner_jobs.script(session, me, task_nr), media_type="text/x-python")


@router.post("/{task_nr}/result")
def result(task_nr: str, body: RunResultIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Outcome of the sandbox job; moves the task to for_review (exit 0) or aborted."""
    row = runner_jobs.record_result(session, me, task_nr, body.model_dump(mode="json"))
    return {"result_id": row.id, "status": session.get(Task, row.task_id).status}


@router.post("/{task_nr}/fail")
def fail(task_nr: str, body: RunFailIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    task = runner_jobs.fail(session, me, task_nr, body.code, body.detail)
    return {"status": task.status}
