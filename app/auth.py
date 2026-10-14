"""UI authentication (session cookie) and CSRF.

- Tailscale: `Tailscale-User-Login` header (localhost only, via `tailscale serve`).
  The login must be in UI_ALLOWED_LOGINS and maps through `actors.tailscale_login`.
- Password: a human actor from `actors` (argon2).
- Bootstrap admin (`admin` + BOOTSTRAP_ADMIN_PASSWORD_HASH or an allowed Tailscale login
  without a mapping): has no actor in setup mode; when the DB is available, maps to
  BOOTSTRAP_ADMIN_ACTOR (default `admin`) to prevent lockout.
Sessions live only in the signed cookie, not in the DB. Each request with a DB checks
whether the actor is still active.
"""
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request

from app.i18n import _
from app.config import get_settings

TAILSCALE_HEADER = "Tailscale-User-Login"
_hasher = PasswordHasher()


@dataclass
class UiUser:
    name: str
    role: str
    bootstrap: bool = False
    actor_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class LoginRequired(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hash_: str, password: str) -> bool:
    if not hash_:
        return False
    try:
        return _hasher.verify(hash_, password)
    except (VerificationError, InvalidHashError):
        return False


def _from_loopback(request: Request) -> bool:
    """Trust the Tailscale header only from tailscale serve (localhost or TRUSTED_PEER_NETWORKS)."""
    return get_settings().is_trusted_peer(request.client.host if request.client else "")


def tailscale_login(request: Request) -> str | None:
    login = request.headers.get(TAILSCALE_HEADER)
    if login and _from_loopback(request):
        return login.strip().lower()
    return None


def login_session(request: Request, user: UiUser) -> None:
    request.session.clear()
    request.session["user"] = {"name": user.name, "role": user.role, "bootstrap": user.bootstrap,
                               "actor_id": user.actor_id}
    request.session["csrf"] = secrets.token_urlsafe(32)


def ui_user_from_actor(actor, bootstrap: bool = False) -> UiUser:
    return UiUser(name=actor.name, role=actor.role, bootstrap=bootstrap, actor_id=actor.id)


def bootstrap_user() -> UiUser:
    """Map the bootstrap admin to the configured admin actor when the DB is available."""
    from app.db import db

    if db.ready:
        from app.models import Actor
        from sqlalchemy import select

        with db.session() as s:
            actor = s.scalar(select(Actor).where(Actor.name == get_settings().bootstrap_admin_actor))
            if actor is not None and actor.is_active and actor.role == "admin":
                return ui_user_from_actor(actor, bootstrap=True)
    return UiUser(name="admin", role="admin", bootstrap=True)


def _refresh(user: UiUser) -> UiUser | None:
    """Check actor sessions against the DB (disabled → logged out, role refreshed)."""
    from app.db import db

    if user.actor_id is None or not db.ready:
        return user
    from app.models import Actor

    with db.session() as s:
        actor = s.get(Actor, user.actor_id)
        if actor is None or not actor.is_active:
            return None
        return ui_user_from_actor(actor, user.bootstrap)


def _tailscale_user(login: str) -> UiUser | None:
    from app.db import db

    if login not in get_settings().allowed_logins:
        return None
    if db.ready:
        from app.services.actors import by_tailscale_login

        with db.session() as s:
            actor = by_tailscale_login(s, login)
            if actor is not None:
                return ui_user_from_actor(actor)
    return bootstrap_user()


def current_user(request: Request) -> UiUser | None:
    data = request.session.get("user")
    if data:
        user = _refresh(UiUser(**data))
        if user is None:
            request.session.clear()
        return user
    login = tailscale_login(request)
    if login:
        user = _tailscale_user(login)
        if user is not None:
            login_session(request, user)
            return user
    return None


def require_admin(request: Request) -> UiUser:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    if not user.is_admin:
        raise HTTPException(403, _( 'Admins only' ))
    return user


# --- CSRF -------------------------------------------------------------------

def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


async def csrf_protect(request: Request) -> None:
    """Dependency for all UI POSTs: token from the header (HTMX) or form field."""
    expected = request.session.get("csrf")
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        form = await request.form()
        sent = form.get("csrf_token")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(403, _( 'Invalid CSRF token' ))


# --- Login failure rate limit ------------------------------------------

class FailureLimiter:
    def __init__(self, max_failures: int = 5, window_s: int = 300) -> None:
        self.max_failures, self.window_s = max_failures, window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q, cutoff = self._fails[key], time.monotonic() - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.max_failures

    def fail(self, key: str) -> None:
        self._prune(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


login_limiter = FailureLimiter()
