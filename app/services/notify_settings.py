"""Notification settings over the API (`tbr notify`, MCP `notify`): status, mute and unmute all messages or review
requests only.

A human changes their own settings. Orchestrators and admins may also change those of a human linked with Telegram –
e.g. openclaw-agent acting on a request in Telegram. Every change is audited (`via="api"`); a change made by someone else is
announced to the person in Telegram (event `settings_changed`, delivered even while muted).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import Forbidden, NotFound, Unprocessable
from app.i18n import default_language, use_language
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import Actor, TelegramLink
from app.services import notifications, telegram
from app.services.actors import utcnow

ACTIONS = ("pause", "resume", "reviews_off", "reviews_on")
OTHERS_ROLES = ("admin", "orchestrator")


def target(session: Session, me: Actor, user: str | None) -> Actor:
    """The human whose settings are read or changed. Without `user` the caller itself (humans only)."""
    name = (user or "").strip()
    if not name or name == me.name:
        if me.actor_type != "human":
            raise Unprocessable(_("Agents have no notifications of their own – name the human with user"),
                                code="notify_user_required")
        return me
    if me.role not in OTHERS_ROLES:
        raise Forbidden(_("Only orchestrators and admins may change the notifications of other people"))
    actor = session.scalar(select(Actor).where(Actor.name == name))
    if (actor is None or actor.actor_type != "human" or not actor.is_active
            or session.get(TelegramLink, actor.id) is None):
        raise NotFound(_("No human named '%(name)s' is linked with Telegram", name=name), code="not_linked")
    return actor


def _mute_info(until: datetime | None, now: datetime) -> dict:
    if until is None or until <= now:
        return {"muted": False, "until": None, "until_local": None, "forever": False}
    forever = until >= notifications.FOREVER
    local = None
    if not forever:
        local = (until.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(get_settings().tz_display))
                 .strftime("%Y-%m-%d %H:%M"))
    return {"muted": True, "until": None if forever else until.isoformat(), "until_local": local, "forever": forever}


def status(session: Session, actor: Actor) -> dict:
    pref = notifications.prefs(session, actor.id)
    now = utcnow()
    quiet = None
    if pref.quiet_start is not None and pref.quiet_end is not None and pref.quiet_start != pref.quiet_end:
        quiet = f"{pref.quiet_start // 60:02d}:{pref.quiet_start % 60:02d}–{pref.quiet_end // 60:02d}:" \
                f"{pref.quiet_end % 60:02d}"
    return {"user": actor.name, "display_name": actor.display_name,
            "telegram_linked": session.get(TelegramLink, actor.id) is not None,
            "bot_enabled": telegram.enabled(session), "timezone": get_settings().tz_display,
            "all": _mute_info(pref.mute_all_until, now), "reviews": _mute_info(pref.mute_review_until, now),
            "events": sorted(notifications.enabled_events(pref)), "quiet_hours": quiet}


def _until(action: str, text: str | None) -> datetime | None:
    if action in ("resume", "reviews_on"):
        return None
    if not (text or "").strip():
        return notifications.FOREVER
    until = notifications.parse_until(text, utcnow())
    if until is None:
        raise Unprocessable(_("Unknown duration '%(value)s' – e.g. 30m, 2h, 1d, 1w or until 18:00", value=text),
                            code="invalid_duration")
    return until


def change_text(action: str, until: datetime | None) -> str:
    """Same wording as the bot commands (call in the message language)."""
    if action == "pause":
        return _("All messages muted %(until)s.", until=notifications.until_text(until))
    if action == "resume":
        return _("Messages unmuted.")
    if action == "reviews_off":
        return _("Review requests muted %(until)s.", until=notifications.until_text(until))
    return _("Review requests unmuted.")


def change(session: Session, me: Actor, user: str | None, action: str, until_text: str | None = None) -> dict:
    if action not in ACTIONS:
        raise Unprocessable(_("Unknown action '%(value)s' – expected pause, resume, reviews_off or reviews_on",
                              value=action), code="invalid_action")
    actor = target(session, me, user)
    until = _until(action, until_text)
    reviews = action.startswith("reviews")
    if reviews:
        notifications.save_prefs(session, actor, mute_review=until)
    else:
        notifications.save_prefs(session, actor, mute_all=until)
    on = action in ("pause", "reviews_off")
    audit(me.name, "notifications_muted" if on else "notifications_unmuted", target=actor.name,
          scope="reviews" if reviews else "all", until=until.isoformat() if until else None, via="api")
    if actor.id != me.id:
        _announce(session, me, actor, action, until)
    return {**status(session, actor), "changed": action}


def _announce(session: Session, me: Actor, actor: Actor, action: str, until: datetime | None) -> None:
    """Tell the person in Telegram that someone else changed their settings (not subject to mute or quiet hours)."""
    if not telegram.enabled(session) or session.get(TelegramLink, actor.id) is None:
        return
    with use_language(default_language()):
        text = _("%(name)s changed your TaskBrigade notifications:", name=me.display_name or me.name) + "\n" + \
            change_text(action, until) + "\n" + _("Check with /status, change with /pause, /resume or /reviews.")
    notifications.store_settings_notice(session, actor.id, text)
