"""Own profile: display preference, Telegram link (one-time code, unlink) and notification settings."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from app.auth import UiUser, csrf_protect
from app.config import get_settings
from app.errors import Unprocessable
from app.i18n import gettext as _
from app.logging_setup import audit
from app.services import notifications, telegram, telegram_bot
from app.services.actors import utcnow
from app.web.common import render, ui_actor, web_session
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
        ctx.update(
            actor=actor, bot_enabled=telegram.enabled(s), bot_username=telegram.bot_info.get("username"),
            link=telegram_bot.link_of(s, actor.id), pending=telegram_bot.pending_code(s, actor),
            enabled=notifications.enabled_events(pref), quiet_start=_hhmm(pref.quiet_start),
            quiet_end=_hhmm(pref.quiet_end), all_projects=pref.all_projects,
            mute_all=pref.mute_all_until if pref.mute_all_until and pref.mute_all_until > now else None,
            mute_review=pref.mute_review_until if pref.mute_review_until and pref.mute_review_until > now else None,
            forever=notifications.FOREVER, log=notifications.recent_log(s, actor.id, 20),
            mute_options=mute_options())
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
        events = {e for e in notifications.EVENTS if form.get(f"event_{e}")}
        start, end = _minutes(str(form.get("quiet_start", ""))), _minutes(str(form.get("quiet_end", "")))
        if (start is None) != (end is None):
            raise Unprocessable(_("Quiet hours need a start and an end"))
        mute_all = parse_mute(str(form.get("mute_all", "keep")), str(form.get("mute_all_at", "")))
        mute_review = parse_mute(str(form.get("mute_review", "keep")), str(form.get("mute_review_at", "")))
        with web_session() as s:
            notifications.save_prefs(s, ui_actor(s, user), events=events, quiet=(start, end), mute_all=mute_all,
                                     mute_review=mute_review, all_projects=bool(form.get("all_projects")))
    except Unprocessable as e:
        return _page(request, user, 422, error=e.message)
    audit(user.name, "notification_prefs_saved")
    return _page(request, user, message=_("Saved."))


def mute_options() -> list[tuple[str, str]]:
    return [("keep", _("unchanged")), ("off", _("off")), ("1h", _("1 hour")), ("8h", _("8 hours")),
            ("1d", _("1 day")), ("7d", _("7 days")), ("until", _("until …")), ("forever", _("permanently"))]


@router.post("/display", dependencies=POST)
async def save_display(request: Request, user: UiUser = Depends(require_user)):
    """Profile switches "Show all projects" and, for admins, "Show all agents" (display preferences only)."""
    form = await request.form()
    show_all = bool(form.get("show_all_projects"))
    with web_session() as s:
        me = ui_actor(s, user)
        me.show_all_projects = show_all
        if me.role == "admin":
            me.show_all_agents = bool(form.get("show_all_agents"))
        all_agents = me.show_all_agents
    audit(user.name, "display_prefs_saved", show_all_projects=show_all, show_all_agents=all_agents)
    return _page(request, user, message=_("Saved."))
