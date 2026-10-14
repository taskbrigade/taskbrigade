"""/api/v1/notifications/settings – status and muting of Telegram notifications (`tbr notify`, MCP `notify`).
Own settings for humans; orchestrators and admins also for a human linked with Telegram (`user`)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import current_actor, SessionDep
from app.models import Actor
from app.schemas import NotifyChange
from app.services import notify_settings

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/settings")
def notify_status(user: str | None = Query(None), session: Session = SessionDep,
                 me: Actor = Depends(current_actor)):
    return notify_settings.status(session, notify_settings.target(session, me, user))


@router.post("/settings")
def change_settings(body: NotifyChange, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    return notify_settings.change(session, me, body.user, body.action, body.until)
