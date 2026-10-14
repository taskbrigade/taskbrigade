"""/api/v1/tasks – backlog, creation, editing, and workflow shortcuts."""
from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from sqlalchemy.orm import Session

from app.i18n import _
from app.api.deps import current_actor, SessionDep
from app.errors import Unprocessable
from app.logging_setup import audit
from app.models import Actor, Sprint
from app.schemas import (BodyIn, CommentIn, DuplicateIn, HeartbeatIn, ModelUsedIn, ReleaseIn, ReviewIn, SftpUploadIn, SubmitIn,
                         TaskCreate, TaskPatch, TransitionIn)
from app.services import notifications, reviews, sprints, state_machine
from app.services import tasks as svc

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
def list_tasks(project: str | None = None, assigned_to: str | None = None,
               status: list[str] | None = Query(None), prio: list[int] | None = Query(None),
               q: str | None = None, parent: str | None = None, limit: int = 50, offset: int = 0,
               sort: str = "prio", desc: bool = False, owner: str | None = None, sprint: str | None = None,
               reviewer: str | None = None, session: Session = SessionDep,
               me: Actor = Depends(current_actor)):
    """`reviewer`: me | none | agents | <name>."""
    statuses = [s for v in status or [] for s in v.split(",") if s]
    items, total = svc.search(session, me, project=project, assigned_to=assigned_to, status=statuses or None,
                              prio=prio, q=q, parent=parent, limit=limit, offset=offset, sort=sort, desc=desc,
                              owner=owner, sprint=sprint, reviewer=reviewer)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create_task(body: TaskCreate, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    task = svc.create(session, me, **body.model_dump())
    out = svc.payload(session, task)
    if body.status == "defined" and task.status == "draft":  # Planned sprint: always a draft.
        out["notice"] = sprints.planned_notice(session.get(Sprint, task.sprint_id))
    return out


@router.get("/{task_nr}")
def get_task(task_nr: str, session: Session = SessionDep, _: Actor = Depends(current_actor)):
    return svc.payload(session, svc.get(session, task_nr))


@router.patch("/{task_nr}")
def patch_task(task_nr: str, body: TaskPatch, if_match: str | None = Header(None),
               session: Session = SessionDep, me: Actor = Depends(current_actor)):
    version = None
    if if_match:
        try:
            version = int(if_match.strip().strip('"').removeprefix("W/").strip('"'))
        except ValueError as e:
            raise Unprocessable(_( 'If-Match must contain the numeric row_version' )) from e
    task = svc.update(session, me, svc.get(session, task_nr, for_update=True),
                      body.model_dump(exclude_unset=True), version)
    return svc.payload(session, task)


@router.post("/{task_nr}/duplicate", status_code=201)
def duplicate_task(task_nr: str, body: DuplicateIn | None = None, session: Session = SessionDep,
                   me: Actor = Depends(current_actor)):
    """Copy the task into a new draft of the same project (admins, orchestrators, the project owner)."""
    body = body or DuplicateIn()
    origin = svc.get(session, task_nr)
    task = svc.duplicate(session, me, origin, assign=body.assign, title=body.title)
    audit(me.name, "task_duplicated", task=origin.task_nr, duplicate=task.task_nr, via="api")
    return svc.payload(session, task)


@router.post("/{task_nr}/attachments", status_code=201)
async def upload_attachments(task_nr: str, files: list[UploadFile] = File(...),
                             session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Attach files: commit them under tasks/<task_nr>/ on the project's default branch."""
    from app.services import attachments

    uploads = [attachments.Upload(f.filename or "datei", await f.read(attachments.MAX_BYTES + 1)) for f in files]
    task = svc.get(session, task_nr)
    attachments.add(session, me, task, uploads)
    return svc.payload(session, task)["attachments"]


@router.post("/{task_nr}/comments", status_code=201)
def add_comment(task_nr: str, body: CommentIn, session: Session = SessionDep,
                me: Actor = Depends(current_actor)):
    task = svc.get(session, task_nr)
    c = svc.comment(session, me, task, body.body)
    if body.signal == "stalled" and task.assigned_to == me.id and task.status == "in_progress":
        notifications.task_event(session, task, "agent_blocked", body.body.strip().splitlines()[0])
    return {"id": c.id, "type": c.comment_type, "author": me.name, "body": c.body}


def _act(session: Session, task_nr: str, action: str, me: Actor, payload: dict) -> dict:
    task = svc.get(session, task_nr, for_update=True)
    state_machine.transition(session, task, action, me, payload)
    return svc.payload(session, task)


@router.post("/{task_nr}/transition")
def transition(task_nr: str, body: TransitionIn, session: Session = SessionDep,
               me: Actor = Depends(current_actor)):
    task = svc.get(session, task_nr, for_update=True)
    state_machine.transition_to(session, task, body.to, me, body.comment, body.override)
    if body.override:
        audit(me.name, "status_override", task=task.task_nr, to=body.to)
    return svc.payload(session, task)


@router.post("/{task_nr}/heartbeat")
def heartbeat(task_nr: str, body: HeartbeatIn | None = None, session: Session = SessionDep,
              me: Actor = Depends(current_actor)):
    body = body or HeartbeatIn()
    task = svc.heartbeat(session, me, svc.get(session, task_nr, for_update=True), body.waiting, body.for_minutes,
                         body.watch_file, body.done_waiting)
    if reviews.claimed_by(task, me):  # Review job of a reviewer agent: its own lease.
        return {"task_nr": task.task_nr, "lease_expires_at": task.review_lease_expires_at, "job": "review"}
    return {"task_nr": task.task_nr, "lease_expires_at": task.lease_expires_at, "waiting": svc.waiting_info(task)}


@router.post("/{task_nr}/model-used")
def model_used(task_nr: str, body: ModelUsedIn, session: Session = SessionDep,
               me: Actor = Depends(current_actor)):
    """Runner: record the model the agent was started with (event `model_used` in the task history)."""
    task = svc.record_model_used(session, me, svc.get(session, task_nr), body.model, body.note)
    return {"task_nr": task.task_nr, "model": body.model}


@router.post("/{task_nr}/sftp-uploads", status_code=201)
def sftp_uploads(task_nr: str, body: SftpUploadIn, session: Session = SessionDep,
                 me: Actor = Depends(current_actor)):
    """`tbr sftp put`/MCP `sftp_put`: note the uploaded files (path, size, SHA-256) as a system comment."""
    from app.services import sftp

    task = svc.get(session, task_nr)
    text = sftp.record_upload(session, me, task, [f.model_dump() for f in body.files])
    audit(me.name, "sftp_upload", task=task.task_nr, files=len(body.files))
    return {"task_nr": task.task_nr, "body": text}


@router.post("/{task_nr}/ask")
def ask(task_nr: str, body: BodyIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    return _act(session, task_nr, "ask", me, {"body": body.body})


@router.post("/{task_nr}/submit")
def submit(task_nr: str, body: SubmitIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    return _act(session, task_nr, "submit", me, body.model_dump())


@router.post("/{task_nr}/release")
def release(task_nr: str, body: ReleaseIn, session: Session = SessionDep,
            me: Actor = Depends(current_actor)):
    return _act(session, task_nr, "release", me, {"body": body.reason, "signal": body.signal})


@router.post("/{task_nr}/abort")
def abort(task_nr: str, body: ReleaseIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Agent cannot complete the task → 'aborted', pending reassignment by a human."""
    return _act(session, task_nr, "abort", me, {"body": body.reason})


@router.post("/{task_nr}/answer")
def answer(task_nr: str, body: BodyIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    return _act(session, task_nr, "answer", me, {"body": body.body})


@router.post("/{task_nr}/review")
def review(task_nr: str, body: ReviewIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Review decision: admins, the project owner and the task's reviewer (`tbr review`, MCP `review_task`)."""
    result = _act(session, task_nr, body.decision, me, {"body": body.comment})
    audit(me.name, f"review_{body.decision}", task=result["task_nr"],
          reviewer_agent=me.actor_type == "agent" and result.get("reviewer") == me.name)
    return result


@router.post("/{task_nr}/review/release")
def review_release(task_nr: str, body: ReleaseIn, session: Session = SessionDep,
                   me: Actor = Depends(current_actor)):
    """Reviewer agent: give the review job back undecided; it stays in the reviewer's queue."""
    task = reviews.release(session, svc.get(session, task_nr, for_update=True), me, body.reason)
    return svc.payload(session, task)
