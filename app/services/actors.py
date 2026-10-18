"""Actors (humans and agents): creation, management, API tokens, and Gitea credentials."""
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app import crypto
from app.i18n import _
from app.auth import hash_password
from app.config import get_settings
from app.errors import Conflict, NotFound, Unprocessable
from app.models import AGENT_KINDS, ROLES, Actor, Task
from app.services import model_choice, settings_store, zones
from app.services.gitea import GiteaError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
TOKEN_PREFIX = "tm_"
OPEN_STATUSES = ("draft", "defined", "in_progress", "questions", "aborted", "for_review", "changes_requested")

# Fields editable by PATCH (name is immutable).
EDITABLE = {"display_name", "role", "runtime", "model_location", "agent_kind", "lease_minutes", "max_parallel_tasks",
            "notes", "email", "tailscale_login", "owner", "is_public"}
HUMAN_ONLY = {"email", "tailscale_login"}
AGENT_ONLY = {"owner", "is_public", "model_location"}  # Owner (a human, by name) and "public": who may assign the agent (`access`).
MODEL_FIELDS = {"model_default", "model_tiers", "models_allowed"}  # Agents only (`model_choice`)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def get(session: Session, name: str) -> Actor:
    actor = session.scalar(select(Actor).where(Actor.name == name))
    if actor is None:
        raise NotFound(_("Actor '%(name)s' not found", name=name))
    return actor


def list_all(session: Session) -> list[Actor]:
    return list(session.scalars(select(Actor).order_by(Actor.actor_type.desc(), Actor.name)))


def resolve_owner(session: Session, name: str | None) -> Actor | None:
    """Owner of an agent by name: an existing human; empty = no owner (the agent should then be public)."""
    name = (name or "").strip()
    if not name or name.lower() in ("none", "-"):
        return None
    owner = session.scalar(select(Actor).where(Actor.name == name))
    if owner is None or owner.actor_type != "human":
        raise Unprocessable(_("Owner '%(name)s' is not a human", name=name), code="invalid_owner")
    return owner


def owner_names(session: Session) -> dict[int, str]:
    """Agent id → owner name, for lists."""
    owner = aliased(Actor)
    return dict(session.execute(select(Actor.id, owner.name).join(owner, owner.id == Actor.owner_id)).all())


def by_token(session: Session, token: str) -> Actor | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    actor = session.scalar(select(Actor).where(Actor.api_key_hash == hash_token(token)))
    if actor is None or not actor.is_active:
        return None
    return actor


def by_tailscale_login(session: Session, login: str) -> Actor | None:
    return session.scalar(select(Actor).where(func.lower(Actor.tailscale_login) == login.lower(),
                                              Actor.is_active.is_(True), Actor.actor_type == "human"))


def authenticate_password(session: Session, name: str, password: str) -> Actor | None:
    from app.auth import verify_password

    actor = session.scalar(select(Actor).where(Actor.name == name))
    if actor is None or not actor.is_active or actor.actor_type != "human":
        verify_password(_DUMMY_HASH, password)  # Same timing as for an existing user.
        return None
    return actor if verify_password(actor.password_hash or "", password) else None


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _validate(actor: Actor) -> None:
    if not NAME_RE.match(actor.name):
        raise Unprocessable(_("Name: only a-z, 0-9, and '-'; start with a letter or digit; max. 50 characters"))
    if actor.role not in ROLES:
        raise Unprocessable(_("Role must be one of %(roles)s", roles=", ".join(ROLES)))
    if actor.role == "member" and actor.actor_type != "human":
        raise Unprocessable(_( "The 'member' role is only available to humans" ))
    if actor.role == "reviewer" and actor.actor_type != "agent":
        raise Unprocessable(_("The 'reviewer' role is only available to agents (humans review as owner, admin or "
                              "entered reviewer)"))
    if actor.runtime not in ("local", "cloud"):
        raise Unprocessable(_( "Runtime must be 'local' or 'cloud'" ))
    zones.check_model_location(actor)
    if actor.actor_type == "agent":
        if actor.agent_kind not in AGENT_KINDS:
            raise Unprocessable(_("Kind must be one of %(kinds)s", kinds=", ".join(AGENT_KINDS)))
        if actor.agent_kind == "runner" and (actor.role != "worker" or actor.runtime != "local"):
            # Runners execute sealed jobs on the TaskBrigade host: never reviewers, owners or cloud processes.
            raise Unprocessable(_("A runner is a local worker: role 'worker', runtime 'local'"),
                                code="invalid_runner")
        if actor.email or actor.tailscale_login or actor.password_hash:
            raise Unprocessable(_( 'Email, password, and Tailscale login are only available to humans' ))
    elif actor.actor_type == "human":
        actor.agent_kind = None
    else:
        raise Unprocessable(_( "Type must be 'human' or 'agent'" ))
    if not 1 <= (actor.lease_minutes or 0) <= 10080:
        raise Unprocessable(_("Lease minutes: 1–10080"))
    if not 1 <= (actor.max_parallel_tasks or 0) <= 50:
        raise Unprocessable(_("Maximum parallel tasks: 1–50"))


def _unique_check(session: Session, actor: Actor) -> None:
    if actor.tailscale_login:
        other = session.scalar(select(Actor).where(func.lower(Actor.tailscale_login) == actor.tailscale_login.lower(),
                                                   Actor.id != (actor.id or 0)))
        if other:
            raise Conflict(_("Tailscale login is already assigned to '%(name)s'", name=other.name))


def _issue_token(actor: Actor) -> str:
    token = new_token()
    actor.api_key_hash = hash_token(token)
    actor.api_key_prefix = token[:8]
    actor.api_key_created_at = utcnow()
    return token


@dataclass
class Created:
    actor: Actor
    token: str | None


def create(session: Session, *, name: str, display_name: str, actor_type: str, role: str = "worker",
           runtime: str = "local", agent_kind: str | None = None, lease_minutes: int = 120,
           model_location: str = "cloud",
           max_parallel_tasks: int = 2, notes: str | None = None, email: str | None = None,
           password: str | None = None, tailscale_login: str | None = None, issue_token: bool = True,
           owner: Actor | None = None, is_public: bool = False) -> Created:
    """`issue_token=False`: no API token yet – the agent gets one through a join code (`tbr join`).
    Agents: `owner` (a human, usually the creating one) and `is_public` (default off: only the owner and admins see
    the agent in assignment choices)."""
    name = name.strip().lower()
    if actor_type == "agent" and agent_kind == "runner":
        model_location = "none"  # A runner runs no model (research/konzept-datenzonen.md §2).
    if session.scalar(select(Actor.id).where(Actor.name == name)):
        raise Conflict(_("Name '%(name)s' is already in use", name=name))
    actor = Actor(name=name, display_name=display_name.strip() or name, actor_type=actor_type, role=role,
                  runtime=runtime, agent_kind=agent_kind or None, lease_minutes=lease_minutes,
                  model_location=model_location or "cloud",
                  max_parallel_tasks=max_parallel_tasks, notes=notes or None, email=email or None,
                  tailscale_login=(tailscale_login or "").strip().lower() or None, is_active=True)
    if actor_type == "agent":
        if owner is not None and owner.actor_type != "human":
            raise Unprocessable(_("Owner '%(name)s' is not a human", name=owner.name), code="invalid_owner")
        actor.owner_id, actor.is_public = (owner.id if owner else None), bool(is_public)
    if password:
        _check_password(password)
        actor.password_hash = hash_password(password)
    _validate(actor)
    _unique_check(session, actor)
    token = _issue_token(actor) if issue_token else None
    session.add(actor)
    session.flush()
    return Created(actor, token)


def update(session: Session, actor: Actor, changes: dict) -> list[str]:
    changes = dict(changes)
    models = {k: changes.pop(k) for k in MODEL_FIELDS & set(changes)}
    unknown = set(changes) - EDITABLE
    if unknown:
        raise Unprocessable(_("Cannot change: %(fields)s", fields=", ".join(sorted(unknown))))
    if actor.actor_type == "agent" and any(changes.get(k) for k in HUMAN_ONLY):
        raise Unprocessable(_( 'Email and Tailscale login are only available to humans' ))
    if actor.actor_type != "agent" and any(changes.get(k) for k in AGENT_ONLY):
        raise Unprocessable(_("Owner and “public” are only available to agents"))
    for key in AGENT_ONLY & set(changes):
        if actor.actor_type != "agent":
            changes.pop(key)
    if "owner" in changes:
        owner = resolve_owner(session, changes.pop("owner"))
        changes["owner_id"] = owner.id if owner else None
    if "is_public" in changes:
        changes["is_public"] = bool(changes["is_public"])
    if "model_location" in changes:
        changes["model_location"] = (changes["model_location"] or "").strip() or actor.model_location
    zones.check_switch(session, actor, changes)
    changed = []
    for key, value in changes.items():
        if isinstance(value, str):
            value = value.strip() or None
        if key == "tailscale_login" and value:
            value = value.lower()
        if key == "display_name" and not value:
            raise Unprocessable(_("Display name must not be empty"))
        if getattr(actor, key) != value:
            setattr(actor, key, value)
            changed.append(key)
    if "email" in changed:  # A new address needs its own confirmation (`mail.create_verify_code`).
        actor.email_verified_at = None
    if models:
        current = model_choice.settings(actor)
        changed += model_choice.set_agent_models(
            actor, default=models.get("model_default", current["default"]),
            tier_models=(models["model_tiers"] or {}) if "model_tiers" in models else current["tiers"],
            allowed_models=models.get("models_allowed", current["allowed"]))
    _validate(actor)
    _unique_check(session, actor)
    session.flush()
    return changed


def _check_password(password: str) -> None:
    if len(password) < 10:
        raise Unprocessable(_("Password: at least 10 characters"))


def set_password(session: Session, actor: Actor, password: str) -> None:
    if actor.actor_type != "human":
        raise Unprocessable(_( 'Only humans have a UI password' ))
    _check_password(password)
    actor.password_hash = hash_password(password)
    session.flush()


def rotate_token(session: Session, actor: Actor) -> str:
    if not actor.is_active:
        raise Conflict(_("Actor is inactive – reactivate first"))
    token = _issue_token(actor)
    session.flush()
    return token


def set_active(session: Session, actor: Actor, active: bool, by: Actor | None = None) -> None:
    """Deactivation immediately invalidates the API token (deletes its hash) and revokes Gitea
    collaborator rights. Reactivation requires rotating the token; collaborator sync restores rights."""
    from app.services import members

    if not active and by is not None and by.id == actor.id:
        raise Conflict(_("You cannot deactivate yourself"))
    if not active and actor.role == "admin":
        admins = session.scalar(select(func.count()).select_from(Actor).where(
            Actor.role == "admin", Actor.is_active.is_(True), Actor.id != actor.id))
        if not admins:
            raise Conflict(_("The last active admin cannot be deactivated"))
    was_active, actor.is_active = actor.is_active, active
    if not active:
        actor.api_key_hash = None
        if was_active:
            members.revoke_all(session, actor)
    elif not was_active:
        members.mark_for_resync(session, actor)
    session.flush()


def touch_last_seen(session: Session, actor: Actor) -> None:
    actor.last_seen_at = utcnow()


def task_counts(session: Session) -> dict[int, dict]:
    """Open tasks, in_progress count (`running`) and current in_progress task for each actor."""
    out: dict[int, dict] = {}
    rows = session.execute(select(Task.assigned_to, Task.status, Task.task_nr)
                           .where(Task.assigned_to.is_not(None), Task.status.in_(OPEN_STATUSES)))
    for actor_id, status, task_nr in rows:
        entry = out.setdefault(actor_id, {"open": 0, "running": 0, "current": None})
        entry["open"] += 1
        if status == "in_progress":
            entry["running"] += 1
            entry["current"] = task_nr
    return out


# --- Gitea credentials (variant A) --------------------------------------------

def _fernet():
    return crypto.load_fernet(get_settings().tbr_state_dir)


def set_gitea_credentials(session: Session, actor: Actor, username: str, token: str, email: str | None) -> None:
    """Validate the token with Gitea before saving; the username must match."""
    username, token = username.strip(), token.strip()
    if not username or not token:
        raise Unprocessable(_("Gitea username and token are required"))
    other = session.scalar(select(Actor).where(Actor.gitea_username == username, Actor.id != actor.id))
    if other:
        raise Conflict(_("Gitea user '%(username)s' is already assigned to '%(actor)s'", username=username, actor=other.name))
    try:
        user = settings_store.gitea_client(session).current_user(token=token)
    except GiteaError as e:
        raise Unprocessable(_("Gitea check failed: %(error)s", error=e), code="gitea_check_failed") from e
    if user.login.lower() != username.lower():
        raise Unprocessable(_("Token belongs to Gitea user '%(actual)s', not '%(expected)s'", actual=user.login, expected=username),
                            code="gitea_user_mismatch")
    actor.gitea_username = user.login
    actor.gitea_token_enc = crypto.encrypt(_fernet(), token)
    actor.gitea_token_last4 = token[-4:]
    actor.gitea_token_checked_at = utcnow()
    actor.gitea_email = (email or "").strip() or user.email
    session.flush()


def clear_gitea_credentials(session: Session, actor: Actor) -> None:
    actor.gitea_username = actor.gitea_token_enc = actor.gitea_token_last4 = None
    actor.gitea_token_checked_at = actor.gitea_email = None
    session.flush()


def check_gitea_credentials(session: Session, actor: Actor) -> str:
    """Check now: update `gitea_token_checked_at` only on success."""
    if not actor.gitea_token_enc:
        raise NotFound(_( 'No Gitea credentials configured' ))
    token = crypto.decrypt(_fernet(), actor.gitea_token_enc)
    try:
        user = settings_store.gitea_client(session).current_user(token=token)
    except GiteaError as e:
        raise Unprocessable(_("Gitea check failed: %(error)s", error=e), code="gitea_check_failed") from e
    if user.login.lower() != (actor.gitea_username or "").lower():
        raise Unprocessable(_("Token now belongs to '%(login)s'", login=user.login), code="gitea_user_mismatch")
    actor.gitea_token_checked_at = utcnow()
    session.flush()
    return user.login


def git_credentials(session: Session, actor: Actor) -> dict:
    if not actor.gitea_token_enc:
        raise NotFound(_( 'No Gitea credentials configured for this actor' ))
    return {"username": actor.gitea_username, "token": crypto.decrypt(_fernet(), actor.gitea_token_enc),
            "email": actor.gitea_email, "gitea_url": settings_store.gitea_public_url(session) or None}
