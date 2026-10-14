"""Admin → Settings (spec section 11a.5)."""
from fastapi import APIRouter, Depends, Form, Request

from app.auth import UiUser, csrf_protect, require_admin
from app.errors import AppError
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import Actor
from app.services import notifications, settings_store, telegram
from app.services.gitea import GiteaError
from app.web.common import render, web_session

router = APIRouter(prefix="/admin/settings", dependencies=[Depends(require_admin)])


def _page(request: Request, status: int = 200, **ctx):
    with web_session() as s:
        view = settings_store.public_view(s)
        log = [(n, s.get(Actor, n.actor_id)) for n in notifications.recent_log(s, limit=30)]
        events = {e: _(label) for e, label in notifications.LOG_EVENTS.items()}
        statuses = {k: _(v) for k, v in notifications.STATUS_LABELS.items()}
        return render(request, "admin/settings.html", status, settings=view, section="settings",
                      notification_log=log, events=events, status_labels=statuses, **ctx)


@router.get("")
def page(request: Request):
    return _page(request)


@router.post("", dependencies=[Depends(csrf_protect)])
async def save(request: Request, user: UiUser = Depends(require_admin)):
    form = await request.form()
    values: dict[str, str | None] = {}
    for key, d in settings_store.DEFS.items():
        if form.get(f"reset_{key}"):
            values[key] = None
        elif d.kind == "bool":
            values[key] = "true" if form.get(key) else "false"
        elif d.secret:
            if form.get(key):  # Empty means unchanged.
                values[key] = str(form[key])
        elif key in form:
            values[key] = str(form[key])
    try:
        with web_session() as s:
            current = settings_store.public_view(s)
            # Save only changed values so .env defaults are not persisted.
            values = {k: v for k, v in values.items()
                      if v is None or settings_store.DEFS[k].secret or str(current[k]["value"] or "") != v}
            changed = settings_store.update(s, values, user.actor_id)
    except AppError as e:
        return _page(request, 422, error=e.message)
    audit(user.name, "settings_updated", keys=changed, via="ui")
    return _page(request, message=_("Saved.") if changed else _("No changes."))


@router.post("/test-gitea", dependencies=[Depends(csrf_protect)])
def test_gitea(request: Request):
    with web_session() as s:
        client = settings_store.gitea_client(s)
        try:
            version = client.version()
            if client.token:
                u = client.current_user()
                msg = (_("Gitea %(version)s reachable, service token belongs to '%(login)s' (Gitea admin).",
                         version=version, login=u.login) if u.is_admin else
                       _("Gitea %(version)s reachable, service token belongs to '%(login)s' (not a Gitea admin – "
                         "option B not possible).", version=version, login=u.login))
            else:
                msg = _("Gitea %(version)s reachable, but no service token set.", version=version)
            return render(request, "_flash.html", message=msg, kind="ok")
        except GiteaError as e:
            return render(request, "_flash.html", message=str(e), kind="error")


@router.post("/test-sftp", dependencies=[Depends(csrf_protect)])
def test_sftp(request: Request):
    """Read the SFTP server's host key (no login). Without a stored fingerprint the admin confirms the one shown;
    a different one is reported as a mismatch – changing it is a deliberate edit of the setting."""
    from app.services import sftp

    with web_session() as s:
        try:
            result = sftp.test_server(s)
        except AppError as e:
            return render(request, "_flash.html", message=e.message, kind="error")
    return render(request, "admin/_sftp_test.html", **result)


@router.post("/sftp-host-key", dependencies=[Depends(csrf_protect)])
def accept_sftp_host_key(request: Request, user: UiUser = Depends(require_admin), fingerprint: str = Form(...)):
    """Store the fingerprint shown by "Test SFTP connection" – only if the server still presents exactly it."""
    from app.services import sftp

    try:
        with web_session() as s:
            seen = sftp.test_server(s)["fingerprint"]
            if seen != fingerprint:
                raise AppError(_("The server now presents another host key (%(seen)s) – test again.", seen=seen))
            settings_store.update(s, {"sftp_host_key": fingerprint}, user.actor_id)
    except AppError as e:
        return _page(request, 422, error=e.message)
    audit(user.name, "settings_updated", keys=["sftp_host_key"], via="ui")
    return _page(request, message=_("SFTP host key fingerprint stored."))


@router.post("/test-telegram", dependencies=[Depends(csrf_protect)])
def test_telegram(request: Request):
    with web_session() as s:
        try:
            name = telegram.test_connection(s)
        except telegram.TelegramError as e:
            return render(request, "_flash.html", message=str(e), kind="error")
        msg = _("Telegram bot @%(name)s reachable.", name=name)
        if not telegram.enabled(s):
            msg += " " + _("The bot is switched off – enable it and save to send notifications.")
        return render(request, "_flash.html", message=msg, kind="ok")
