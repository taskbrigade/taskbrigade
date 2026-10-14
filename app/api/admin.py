"""/api/v1/admin/* – admin role only."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import dbconfig
from app.api.deps import SessionDep, require_admin
from app.config import get_settings
from app.db import db
from app.errors import Unprocessable
from app.logging_setup import audit
from app.models import Actor
from app.schemas import (ActorCreate, ActorOut, ActorPatch, ActorWithToken, DatabaseIn, GiteaCredentialsIn,
                         JoinCodeOut, TokenOut)
from app.services import actors as svc
from app.schemas import MemberIn, SftpCredentialsIn
from app.services import database_admin, members, projects, settings_store

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/actors", response_model=list[ActorOut])
def list_actors(session: Session = SessionDep, _: Actor = Depends(require_admin)):
    return svc.list_all(session)


@router.post("/actors", response_model=ActorWithToken, status_code=201)
def create_actor(body: ActorCreate, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    data = body.model_dump()
    owner = data.pop("owner")
    if body.actor_type == "agent":  # Owner: the given human, else the creating admin (if human).
        data["owner"] = svc.resolve_owner(session, owner) if owner else (me if me.actor_type == "human" else None)
    created = svc.create(session, **data)
    audit(me.name, "actor_created", target=created.actor.name, actor_type=created.actor.actor_type,
          role=created.actor.role)
    return ActorWithToken(actor=ActorOut.model_validate(created.actor), token=created.token)


@router.get("/actors/{name}", response_model=ActorOut)
def get_actor(name: str, session: Session = SessionDep, _: Actor = Depends(require_admin)):
    return svc.get(session, name)


@router.patch("/actors/{name}", response_model=ActorOut)
def patch_actor(name: str, body: ActorPatch, session: Session = SessionDep,
                me: Actor = Depends(require_admin)):
    actor = svc.get(session, name)
    data = body.model_dump(exclude_unset=True)
    password = data.pop("password", None)
    active = data.pop("is_active", None)
    changed = svc.update(session, actor, data)
    if password:
        svc.set_password(session, actor, password)
        changed.append("password")
    if active is not None and active != actor.is_active:
        svc.set_active(session, actor, active, by=me)
        changed.append("is_active")
    audit(me.name, "actor_updated", target=actor.name, fields=changed)
    return actor


@router.post("/actors/{name}/rotate-token", response_model=TokenOut)
def rotate_token(name: str, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    actor = svc.get(session, name)
    token = svc.rotate_token(session, actor)
    audit(me.name, "token_rotated", target=actor.name)
    return TokenOut(token=token, prefix=token[:8])


@router.put("/actors/{name}/gitea", response_model=ActorOut)
def put_gitea(name: str, body: GiteaCredentialsIn, session: Session = SessionDep,
              me: Actor = Depends(require_admin)):
    actor = svc.get(session, name)
    svc.set_gitea_credentials(session, actor, body.gitea_username, body.gitea_token, body.gitea_email)
    audit(me.name, "gitea_credentials_set", target=actor.name, gitea_user=actor.gitea_username)
    return actor


@router.delete("/actors/{name}/gitea", response_model=ActorOut)
def delete_gitea(name: str, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    actor = svc.get(session, name)
    svc.clear_gitea_credentials(session, actor)
    audit(me.name, "gitea_credentials_removed", target=actor.name)
    return actor


@router.post("/actors/{name}/gitea/check", response_model=ActorOut)
def check_gitea(name: str, session: Session = SessionDep, _: Actor = Depends(require_admin)):
    actor = svc.get(session, name)
    svc.check_gitea_credentials(session, actor)
    return actor


@router.post("/actors/{name}/gitea/provision", response_model=ActorOut)
def provision_gitea(name: str, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    """Create the agent's Gitea user and token with the Gitea admin token (setting `gitea_admin_token`)."""
    from app.services import onboarding

    actor = svc.get(session, name)
    onboarding.provision_gitea(session, actor)
    audit(me.name, "gitea_user_provisioned", target=actor.name, gitea_user=actor.gitea_username)
    return actor


@router.put("/actors/{name}/sftp", response_model=ActorOut)
def put_sftp(name: str, body: SftpCredentialsIn, session: Session = SessionDep,
             me: Actor = Depends(require_admin)):
    from app.services import sftp

    actor = svc.get(session, name)
    sftp.set_credentials(session, actor, body.sftp_username, body.sftp_password)
    audit(me.name, "sftp_credentials_set", target=actor.name, sftp_user=actor.sftp_username)
    return actor


@router.delete("/actors/{name}/sftp", response_model=ActorOut)
def delete_sftp(name: str, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    from app.services import sftp

    actor = svc.get(session, name)
    sftp.clear_credentials(session, actor)
    audit(me.name, "sftp_credentials_removed", target=actor.name)
    return actor


@router.post("/actors/{name}/sftp/check", response_model=ActorOut)
def check_sftp(name: str, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    from app.services import sftp

    actor = svc.get(session, name)
    sftp.test_access(session, actor)
    audit(me.name, "sftp_access_checked", target=actor.name)
    return actor


@router.post("/actors/{name}/join-code", response_model=JoinCodeOut, status_code=201)
def create_join_code(name: str, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    """One-time code for `tbr join` (earlier unused codes of the agent become invalid)."""
    from app.services import onboarding

    actor = svc.get(session, name)
    code, expires = onboarding.create_code(session, actor, me)
    audit(me.name, "join_code_created", target=actor.name, expires_at=expires.isoformat())
    return JoinCodeOut(code=code, expires_at=expires)


@router.get("/settings")
def get_settings_api(session: Session = SessionDep, _: Actor = Depends(require_admin)):
    return settings_store.public_view(session)


@router.put("/settings")
def put_settings(body: dict[str, str | None], session: Session = SessionDep,
                 me: Actor = Depends(require_admin)):
    changed = settings_store.update(session, body, me.id)
    audit(me.name, "settings_updated", keys=changed)
    return settings_store.public_view(session)


@router.get("/database")
def get_database(_: Actor = Depends(require_admin)):
    p = db.last_probe
    return {**dbconfig.load(get_settings()).public(), "connected": p.ok, "server_version": p.server_version,
            "skip_locked": p.skip_locked, "current_revision": p.current_revision, "head_revision": p.head_revision}


@router.put("/database")
def put_database(body: DatabaseIn, me: Actor = Depends(require_admin)):
    form = database_admin.DbForm(body.host, body.port, body.database, body.user, body.password or "",
                                 body.change_in_db)
    outcome = database_admin.save(get_settings(), form)
    audit(me.name, "db_save", host=body.host, database=body.database, db_user=body.user,
          changed_in_db=body.change_in_db, ok=outcome.ok, via="api")
    if not outcome.ok:
        raise Unprocessable(outcome.message, code="db_save_failed")
    return {"ok": True, "message": outcome.message}


@router.put("/projects/{key}/members/{name}")
def put_member(key: str, name: str, body: MemberIn, session: Session = SessionDep,
               me: Actor = Depends(require_admin)):
    project, actor = projects.get(session, key), svc.get(session, name)
    pm = members.set_member(session, project, actor, body.gitea_permission)
    audit(me.name, "member_set", project=project.project_key, target=name, permission=body.gitea_permission)
    return {"actor": actor.name, "gitea_permission": pm.gitea_permission, "gitea_synced_at": pm.gitea_synced_at,
            "sync_error": pm.sync_error}


@router.delete("/projects/{key}/members/{name}", status_code=204)
def delete_member(key: str, name: str, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    project, actor = projects.get(session, key), svc.get(session, name)
    members.remove_member(session, project, actor)
    audit(me.name, "member_removed", project=project.project_key, target=name)
