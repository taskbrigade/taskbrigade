"""Agent onboarding: Gitea user and token through the optional Gitea admin token, and one-time join codes that
`tbr join` exchanges for a fresh API token.

Join codes are short-lived (setting `join_code_minutes`, default 15), redeemable once and stored only as SHA-256.
The redeem endpoint (`POST /api/v1/join`) only answers trusted peers, like `/agents/me/git-credentials`.
"""
import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.errors import Conflict, Gone, NotFound, Unprocessable
from app.i18n import _
from app.models import Actor, JoinCode
from app.services import actors, members, settings_store
from app.services.gitea import GiteaClient, GiteaError

JOIN_PREFIX = "tmj_"
DEFAULT_JOIN_MINUTES = 15
AGENT_TOKEN_NAME = "taskbrigade-agent"
AGENT_TOKEN_SCOPES = ["write:repository", "read:user"]


# --- Gitea user for an agent ------------------------------------------------------------------

def gitea_admin_client(session: Session) -> GiteaClient | None:
    """Client with the Gitea admin token, or None if no admin token (or no Gitea URL) is configured."""
    url, token = settings_store.get(session, "gitea_url"), settings_store.get(session, "gitea_admin_token")
    return GiteaClient(url, token) if url and token else None


def provision_gitea(session: Session, actor: Actor) -> str:
    """Create the Gitea user `<actor name>` with a token for this agent and store it like a manually linked one.

    The admin token only creates the user; the token is created with the new user's own (random, discarded)
    password, because Gitea accepts token creation only with basic auth. The user gets no admin rights."""
    client = gitea_admin_client(session)
    if client is None:
        raise Unprocessable(_("No Gitea admin token configured (Admin → Settings)"), code="gitea_admin_missing")
    if actor.actor_type != "agent":
        raise Unprocessable(_("Gitea users are only created automatically for agents"))
    if actor.gitea_username:
        raise Conflict(_("'%(name)s' already has a Gitea user (%(user)s)", name=actor.name, user=actor.gitea_username))
    username = actor.name
    try:
        if client.user_exists(username):
            raise Unprocessable(_("Gitea user '%(user)s' already exists – link it manually with a token of that user",
                                  user=username), code="gitea_user_exists")
        password = secrets.token_urlsafe(32)
        client.admin_create_user(username, f"{username}@noreply.localhost", password)
        try:
            token = client.create_token_as(username, password, AGENT_TOKEN_NAME, AGENT_TOKEN_SCOPES)
        except GiteaError:
            client.admin_delete_user(username)  # So that a retry does not hit "already exists".
            raise
    except GiteaError as e:
        raise Unprocessable(_("Creating the Gitea user failed: %(error)s", error=e),
                            code="gitea_provision_failed") from e
    actors.set_gitea_credentials(session, actor, username, token, None)
    members.sync_actor(session, actor)
    return username


# --- Join codes ---------------------------------------------------------------------------------

def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def join_minutes(session: Session) -> int:
    try:
        return int(settings_store.get(session, "join_code_minutes") or DEFAULT_JOIN_MINUTES)
    except ValueError:
        return DEFAULT_JOIN_MINUTES


def create_code(session: Session, actor: Actor, created_by: Actor | None = None,
                minutes: int | None = None) -> tuple[str, datetime]:
    """New join code for `actor`; earlier unused codes of the actor become invalid. Return (code, expires_at)."""
    if actor.actor_type != "agent":
        raise Unprocessable(_("Join codes are only available for agents"))
    if not actor.is_active:
        raise Conflict(_("Actor is inactive – reactivate first"))
    now = actors.utcnow()
    session.execute(delete(JoinCode).where(JoinCode.actor_id == actor.id, JoinCode.used_at.is_(None)))
    session.execute(delete(JoinCode).where(JoinCode.expires_at < now - timedelta(days=30)))
    code = JOIN_PREFIX + secrets.token_urlsafe(18)
    expires = now + timedelta(minutes=minutes or join_minutes(session))
    session.add(JoinCode(actor_id=actor.id, code_hash=hash_code(code), created_by=created_by.id if created_by else None,
                         created_at=now, expires_at=expires))
    session.flush()
    return code, expires


def redeem(session: Session, code: str, client: str | None) -> tuple[Actor, str]:
    """Exchange a join code for a fresh API token (the actor's previous token becomes invalid)."""
    code = (code or "").strip()
    row = session.scalar(select(JoinCode).where(JoinCode.code_hash == hash_code(code))) \
        if code.startswith(JOIN_PREFIX) else None
    if row is None:
        raise NotFound(_("Unknown join code – check it or ask an admin for a new one"), code="join_code_invalid")
    if row.used_at is not None:
        raise Gone(_("This join code has already been used – ask an admin for a new one"), code="join_code_used")
    now = actors.utcnow()
    if row.expires_at <= now:
        raise Gone(_("This join code has expired – ask an admin for a new one"), code="join_code_expired")
    actor = session.get(Actor, row.actor_id)
    if actor is None or not actor.is_active:
        raise Conflict(_("The agent of this join code is inactive"), code="actor_inactive")
    # Conditional update: of two concurrent requests only one marks the code as used.
    result = session.execute(update(JoinCode).where(JoinCode.id == row.id, JoinCode.used_at.is_(None))
                             .values(used_at=now, used_from=(client or "")[:64] or None))
    if result.rowcount != 1:
        raise Gone(_("This join code has already been used – ask an admin for a new one"), code="join_code_used")
    token = actors.rotate_token(session, actor)
    return actor, token


def pending_code(session: Session, actor: Actor) -> JoinCode | None:
    """The actor's unused, unexpired code (for the UI: expiry only, the code itself is not stored)."""
    return session.scalar(select(JoinCode).where(JoinCode.actor_id == actor.id, JoinCode.used_at.is_(None),
                                                 JoinCode.expires_at > actors.utcnow()))
