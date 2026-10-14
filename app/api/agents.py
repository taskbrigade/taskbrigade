"""/api/v1/agents/me/* – endpoints for the calling actor."""

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.i18n import _
from app.api.deps import current_actor, SessionDep
from app.config import get_settings
from app.errors import Forbidden
from app.logging_setup import audit
from app.models import Actor
from app.db import db
from app.schemas import ActorOut, ActorPublic, ClaimIn, GitCredentialsOut, JoinIn, JoinOut
from app.services import actors as svc
from app.services import tasks as task_svc

router = APIRouter(prefix="/agents/me", tags=["agents"])
directory = APIRouter(prefix="/actors", tags=["agents"])
join_router = APIRouter(tags=["agents"])


@directory.get("", response_model=list[ActorPublic])
def list_actors(all: bool = False, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Active humans and the agents the caller may assign (`access.may_use_agent`): own and public agents, for an
    agent also those of its owner. Admins see every agent with `all=true` or the profile switch "Show all agents"
    (they may assign any agent either way)."""
    from app.services import access

    rows = session.scalars(select(Actor).where(Actor.is_active.is_(True)).order_by(Actor.name))
    admin_all = all or access.all_agents_shown(me)
    return [a for a in rows if access.may_use_agent(me, a, admin_all=admin_all)]


def _secure_channel(request: Request) -> bool:
    """Allow only a direct localhost peer: tailscale serve (external TLS) or an agent on this machine.
    Uvicorn runs without --proxy-headers, so forwarded headers do not count. TRUSTED_PEER_NETWORKS count as
    localhost (Docker: the bridge gateway in front of the published port)."""
    return get_settings().is_trusted_peer(request.client.host if request.client else "")


@router.get("", response_model=ActorOut)
def whoami(me: Actor = Depends(current_actor)):
    return me


@router.get("/git-credentials", response_model=GitCredentialsOut)
def git_credentials(request: Request, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    if not _secure_channel(request):
        raise Forbidden(_( 'Git credentials are only available over HTTPS (tailscale serve) or localhost' ), code="insecure_channel")
    creds = svc.git_credentials(session, me)
    audit(me.name, "git_credentials_fetched", client=request.client.host if request.client else None)
    return creds


@router.get("/sftp")
def sftp_info(task_nr: str | None = None, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """SFTP storage for `tbr sftp info`/`put`/`ls`: host, port, host key, base folder, own user – never the password.
    With `task_nr` also the task's folder from the storage pattern."""
    from app.services import sftp

    return sftp.info(session, me, task_svc.get(session, task_nr) if task_nr else None)


@router.get("/sftp-credentials")
def sftp_credentials(request: Request, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Own SFTP login incl. password – like git credentials only over HTTPS (tailscale serve) or trusted peers,
    audited per fetch. The password is never logged."""
    from app.services import sftp

    if not _secure_channel(request):
        raise Forbidden(_("SFTP credentials are only available over HTTPS (tailscale serve) or localhost"),
                        code="insecure_channel")
    creds = sftp.credentials(session, me)
    audit(me.name, "sftp_credentials_fetched", client=request.client.host if request.client else None)
    return creds


@router.get("/tasks")
def my_tasks(status: list[str] | None = Query(None), session: Session = SessionDep,
             me: Actor = Depends(current_actor)):
    statuses = [s for v in status or [] for s in v.split(",") if s]
    items, total = task_svc.search(session, me, assigned_to="me", status=statuses or None, limit=500)
    return {"items": items, "total": total}


@router.get("/reviews")
def my_reviews(session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Review jobs: tasks in for_review with the caller as reviewer (`claimed`: the caller holds the job)."""
    items, total = task_svc.search(session, me, reviewer="me", status=["for_review"], limit=500)
    return {"items": [{**t, "job": "review"} for t in items], "total": total}


@router.post("/claim", responses={204: {"description": _( 'No task available' )}})
def claim(body: ClaimIn | None = None, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Claim work (or, with `reviews`, a review job first). The response's `job` is `work` or `review`; a review job
    carries the model resolved for the reviewer in `model_effective`."""
    task = task_svc.claim(session, me, body.task_nr if body else None, skip_locked=db.skip_locked,
                          reviews_too=bool(body and body.reviews))
    if task is None:
        return Response(status_code=204)
    out = task_svc.payload(session, task)
    if task_svc.is_review_job(task, me):
        from app.services import reviews

        out.update(job="review", model_effective=reviews.model_for(task, me))
    else:
        out["job"] = "work"
    return out


@join_router.post("/join", response_model=JoinOut)
def join(body: JoinIn, request: Request, session: Session = SessionDep):
    """Exchange a one-time join code (Admin → Neuer Agent) for a fresh API token – `tbr join`. No bearer token;
    like git credentials only over HTTPS (tailscale serve) or from trusted peers, failures count towards the
    API rate limit."""
    from fastapi import HTTPException

    from app.api.deps import api_limiter
    from app.errors import AppError
    from app.services import onboarding, settings_store

    client = request.client.host if request.client else "?"
    if api_limiter.blocked(client):
        raise HTTPException(429, _( 'Too many failed sign-in attempts' ), headers={"X-Error-Code": "rate_limited"})
    if not _secure_channel(request):
        raise Forbidden(_("Join codes can only be redeemed over HTTPS (tailscale serve) or localhost"),
                        code="insecure_channel")
    try:
        actor, token = onboarding.redeem(session, body.code, client)
    except AppError as e:
        api_limiter.fail(client)
        audit("-", "join_code_rejected", reason=e.code, client=client)
        raise
    audit(actor.name, "join_code_redeemed", target=actor.name, client=client)
    return JoinOut(token=token, actor=ActorOut.model_validate(actor),
                   gitea_url=settings_store.gitea_public_url(session) or None)
