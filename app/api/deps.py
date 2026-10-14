"""API authentication via bearer token only; identity comes from the token."""
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.i18n import _
from app.auth import FailureLimiter
from app.db import db
from app.models import Actor
from app.services import actors as actor_service

api_limiter = FailureLimiter(max_failures=10, window_s=300)


def get_session() -> Iterator[Session]:
    """Commit the session at the end of a successful request."""
    session = db.session()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()



# Commit before the response is sent. FastAPI (≥ 0.121) runs the code after `yield` of a dependency with the default
# scope "request" only once the response has gone out, so a client acting on it at once (e.g. `tbr join` → `whoami`
# with the new token) could still read the old state. Use this everywhere instead of `Depends(get_session)`: one
# request then shares one session, and the dependency cache does not mix scopes.
SessionDep = Depends(get_session, scope="function")

def _unauthorized(message: str) -> HTTPException:
    return HTTPException(401, message, headers={"X-Error-Code": "unauthorized", "WWW-Authenticate": "Bearer"})


def current_actor(request: Request, session: Session = SessionDep) -> Actor:
    key = request.client.host if request.client else "?"
    if api_limiter.blocked(key):
        raise HTTPException(429, _( 'Too many failed sign-in attempts' ), headers={"X-Error-Code": "rate_limited"})
    header = request.headers.get("Authorization", "")
    scheme, _sep, token = header.partition(" ")  # nicht `_`: das ist die Übersetzungsfunktion
    if scheme.lower() != "bearer" or not token.strip():
        api_limiter.fail(key)
        raise _unauthorized(_( 'Missing Authorization: Bearer <token>' ))
    actor = actor_service.by_token(session, token.strip())
    if actor is None:
        api_limiter.fail(key)
        raise _unauthorized(_( 'Invalid token or inactive actor' ))
    actor_service.touch_last_seen(session, actor)
    return actor


def require_admin(actor: Actor = Depends(current_actor)) -> Actor:
    if actor.role != "admin":
        raise HTTPException(403, _( 'Admins only' ), headers={"X-Error-Code": "forbidden"})
    return actor
