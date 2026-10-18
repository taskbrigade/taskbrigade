"""Own profile: display preference, Telegram link (one-time code, unlink), email address (one-time confirmation
link) and notification settings (event × channel, email bundling, quiet hours, mute)."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape

from app.auth import UiUser, csrf_protect, current_user
from app.config import get_settings
from app.errors import Unprocessable
from app.i18n import gettext as _
from app.logging_setup import audit
from app.services import actors as actor_svc
from app.services import mail, notifications, telegram, telegram_bot
from app.services.actors import utcnow
from app.web.common import _local_time, is_htmx, render, ui_actor, web_session
from app.web.tasks_ui import require_user

router = APIRouter(prefix="/profile")
POST = [Depends(csrf_protect)]
MUTE_CHOICES = ("keep", "off", "1h", "8h", "1d", "7d", "until", "forever")


def _minutes(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        h, m = value.split(":", 1)
        h, m = int(h), int(m[:2])
    except ValueError:
        raise Unprocessable(_("Invalid time '%(value)s' – expected HH:MM", value=value)) from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise Unprocessable(_("Invalid time '%(value)s' – expected HH:MM", value=value))
    return h * 60 + m


def _hhmm(minutes: int | None) -> str:
    return "" if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_mute(choice: str, at: str):
    """Form value of a mute select → "keep", None (off) or the end time (naive UTC; FOREVER = permanently)."""
    if choice not in MUTE_CHOICES or choice == "keep":
        return "keep"
    if choice == "off":
        return None
    if choice == "forever":
        return notifications.FOREVER
    if choice == "until":
        try:
            local = datetime.fromisoformat((at or "").strip())
        except ValueError:
            raise Unprocessable(_("Mute: enter the date and time until which messages are muted")) from None
        until = local.replace(tzinfo=ZoneInfo(get_settings().tz_display)).astimezone(UTC).replace(tzinfo=None)
        if until <= utcnow():
            raise Unprocessable(_("Mute: the time must be in the future"))
        return until
    return utcnow() + notifications.parse_duration(choice)


def _page(request: Request, user: UiUser, status: int = 200, **ctx):
    with web_session() as s:
        actor = ui_actor(s, user)
        pref = notifications.prefs(s, actor.id)
        now = utcnow()
        ctx.setdefault("events", {e: _(label) for e, label in notifications.EVENTS.items()})
        ctx.setdefault("log_events", {e: _(label) for e, label in notifications.LOG_EVENTS.items()})
        ctx.setdefault("status_labels", {k: _(v) for k, v in notifications.STATUS_LABELS.items()})
        ctx.setdefault("status_tone", {"sent": "ok", "failed": "warn"})
        log_all = request.query_params.get("log") == "all"
        ctx.update(
            actor=actor, bot_enabled=telegram.enabled(s), bot_username=telegram.bot_info.get("username"),
            link=telegram_bot.link_of(s, actor.id), pending=telegram_bot.pending_code(s, actor),
            enabled=notifications.enabled_events(pref), quiet_start=_hhmm(pref.quiet_start),
            quiet_end=_hhmm(pref.quiet_end), all_projects=pref.all_projects,
            mute_all=pref.mute_all_until if pref.mute_all_until and pref.mute_all_until > now else None,
            mute_review=pref.mute_review_until if pref.mute_review_until and pref.mute_review_until > now else None,
            forever=notifications.FOREVER, log=notifications.recent_log(s, actor.id, None if log_all else 20),
            log_all=log_all,
            mute_options=mute_options(), channels={c: _(label) for c, label in notifications.CHANNELS.items()},
            chosen={e: notifications.channels_for(pref, e) for e in notifications.EVENTS},
            bundle=notifications.bundle_minutes(pref), bundle_choices=notifications.BUNDLE_CHOICES,
            mail_enabled=mail.enabled(s), email_verified=mail.verified_address(actor) is not None,
            email_pending=mail.pending_code(s, actor))
        return render(request, "profile.html", status, **ctx)


@router.get("")
def page(request: Request, user: UiUser = Depends(require_user)):
    return _page(request, user)


@router.post("/telegram/code", dependencies=POST)
def new_code(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        actor = ui_actor(s, user)
        if not telegram.enabled(s):
            raise Unprocessable(_("The Telegram bot is not enabled (Admin → Settings)"))
        code, expires = telegram_bot.create_code(s, actor)
    audit(user.name, "telegram_code_created")
    return _page(request, user, code=code, code_expires=expires)


@router.post("/telegram/unlink", dependencies=POST)
def unlink(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        removed = telegram_bot.unlink(s, ui_actor(s, user).id)
    audit(user.name, "telegram_unlinked", via="ui")
    return _page(request, user, message=_("Telegram chat unlinked.") if removed else _("No chat was linked."))


@router.post("/notifications", dependencies=POST)
async def save(request: Request, user: UiUser = Depends(require_user)):
    form = await request.form()
    try:
        channels = {e: {c for c in notifications.CHANNELS if form.get(f"ch_{e}_{c}")} for e in notifications.EVENTS}
        try:
            bundle = int(str(form.get("bundle", "0")))
        except ValueError:
            bundle = -1
        start, end = _minutes(str(form.get("quiet_start", ""))), _minutes(str(form.get("quiet_end", "")))
        if (start is None) != (end is None):
            raise Unprocessable(_("Quiet hours need a start and an end"))
        mute_all = parse_mute(str(form.get("mute_all", "keep")), str(form.get("mute_all_at", "")))
        mute_review = parse_mute(str(form.get("mute_review", "keep")), str(form.get("mute_review_at", "")))
        with web_session() as s:
            me = ui_actor(s, user)
            # The display switches have their own form (`POST /profile/display`) – this one must not touch them.
            notifications.save_prefs(s, me, channels=channels, bundle=bundle, quiet=(start, end),
                                     mute_all=mute_all, mute_review=mute_review,
                                     all_projects=bool(form.get("all_projects")))
    except Unprocessable as e:
        return _page(request, user, 422, error=e.message)
    audit(user.name, "notification_prefs_saved")
    return _page(request, user, message=_("Saved."))


@router.post("/email", dependencies=POST)
def save_email(request: Request, user: UiUser = Depends(require_user), email: str = Form("")):
    """Set the own address (empty removes it) and mail a confirmation link; a new address is unconfirmed."""
    address = email.strip()
    try:
        if address:
            try:
                address = mail.check_address(address)
            except ValueError:
                raise Unprocessable(_("Invalid email address")) from None
        with web_session() as s:
            actor = ui_actor(s, user)
            changed = actor_svc.update(s, actor, {"email": address or None})
            verified = mail.verified_address(actor) is not None
    except Unprocessable as e:
        return _page(request, user, 422, error=e.message)
    if changed:
        audit(user.name, "email_changed", via="ui")
    if not address:
        return _page(request, user, message=_("Email address removed."))
    if verified:
        return _page(request, user, message=_("No changes."))
    return send_verification(request, user)


@router.post("/email/verify", dependencies=POST)
def send_verification(request: Request, user: UiUser = Depends(require_user)):
    """Mail (another) confirmation link to the own unconfirmed address. A failure is shown as the SMTP error."""
    try:
        with web_session() as s:
            actor = ui_actor(s, user)
            if mail.verified_address(actor):
                return _page(request, user, message=_("Your email address is already confirmed."))
            expires = mail.send_verification(s, actor, str(request.base_url))
    except Unprocessable as e:
        return _page(request, user, 422, error=e.message)
    except mail.MailError as e:
        return _page(request, user, 422, error=_("The confirmation mail could not be sent: %(error)s", error=str(e)))
    audit(user.name, "email_verification_sent")
    return _page(request, user, message=_("Confirmation link sent – it is valid until %(time)s.",
                                          time=_local_time(expires)))


@router.get("/email/verify")
def verify(request: Request, code: str = ""):
    """Target of the confirmation link. Works without a login: possessing the link proves the address."""
    with web_session() as s:
        actor = mail.redeem(s, code)
        name = actor.name if actor else None
    if name is None:
        return render(request, "error.html", 410, message=_("This confirmation link is unknown, used or expired – "
                                                            "request a new one under Profile → Notifications."))
    audit(name, "email_verified")
    message = _("Email address confirmed. Choose the events for email under Profile → Notifications.")
    user = current_user(request)
    if user is not None and user.actor_id is not None and user.name == name:
        return _page(request, user, message=message)
    return render(request, "email_verified.html", message=message)


def mute_options() -> list[tuple[str, str]]:
    return [("keep", _("unchanged")), ("off", _("off")), ("1h", _("1 hour")), ("8h", _("8 hours")),
            ("1d", _("1 day")), ("7d", _("7 days")), ("until", _("until …")), ("forever", _("permanently"))]


@router.post("/display", dependencies=POST)
async def save_display(request: Request, user: UiUser = Depends(require_user)):
    """Profile switches "Show all projects" and, for admins, "Show all agents" (display preferences only). The
    switches post here on every change (htmx: answers with the status text only); an unchecked switch is missing
    from the form and means off."""
    form = await request.form()
    show_all = "1" in form.getlist("show_all_projects")
    with web_session() as s:
        me = ui_actor(s, user)
        me.show_all_projects = show_all
        if me.role == "admin":
            me.show_all_agents = "1" in form.getlist("show_all_agents")
        all_agents = me.show_all_agents
    audit(user.name, "display_prefs_saved", show_all_projects=show_all, show_all_agents=all_agents)
    if is_htmx(request):
        return HTMLResponse(escape(_("Saved.")))
    return _page(request, user, message=_("Saved."))
