"""Notifications to humans via Telegram: events, recipients, per-person settings, outbox and delivery.

Workflow code calls `task_event`/`sprint_done`; they write one `notifications` row per recipient (in the caller's
transaction, or in an own one with `detached=True` when the caller rolls back afterwards, e.g. merge conflicts).
`deliver` (scheduler job, only in the process that holds the bot lock) sends due rows with retries and clears the
text once a row is sent or given up, so the table doubles as a delivery log without contents.

Recipients: the project owner, plus admins who opted into "all projects". Projects with privacy `local_only` only
get task number, event and link – no title, agent or content.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.i18n import N_, date_format, default_language, use_language
from app.i18n import gettext as _
from app.models import Actor, NotificationPref, Notification, Project, Sprint, Task, TelegramLink
from app.services import settings_store, telegram
from app.services.actors import utcnow

log = logging.getLogger(__name__)

EVENTS = {
    "question": N_("Question from an agent"),
    "review": N_("Task ready for review"),
    "agent_review": N_("Reviewed by a reviewer agent"),
    "aborted": N_("Aborted by the agent"),
    "conflict": N_("Merge, brain or changelog conflict"),
    "lease_expired": N_("Lease expired"),
    "agent_blocked": N_("Agent blocked or stalled"),
    "sprint_done": N_("Sprint completed"),
}
STATUS_LABELS = {"queued": N_("queued"), "held": N_("held (quiet hours)"), "sent": N_("sent"),
                 "failed": N_("failed"), "skipped": N_("skipped")}
REVIEW_EVENTS = frozenset({"review"})  # Muted separately ("review requests")
# Someone else (an orchestrator) changed a person's settings: always delivered, not a choosable event
SETTINGS_EVENT = "settings_changed"
LOG_EVENTS = {**EVENTS, SETTINGS_EVENT: N_("Notification settings changed")}
FOREVER = datetime(9999, 12, 31)
CONTENT_CHARS = 300
MAX_ATTEMPTS = 8
MAX_SENDS_PER_RUN = 20  # Telegram allows about 30 messages per second; stay well below
LOG_DAYS = 30


# --- Settings per human ------------------------------------------------------------------------

def prefs(session: Session, actor_id: int) -> NotificationPref:
    """Stored settings or an unsaved default row (all events, no quiet hours, not muted)."""
    return session.get(NotificationPref, actor_id) or NotificationPref(actor_id=actor_id, all_projects=False)


def enabled_events(pref: NotificationPref) -> set[str]:
    if pref.events is None:
        return set(EVENTS)
    try:
        return {e for e in json.loads(pref.events) if e in EVENTS}
    except (ValueError, TypeError):
        return set(EVENTS)


def is_muted(pref: NotificationPref, event: str, now: datetime) -> bool:
    if event == SETTINGS_EVENT:
        return False
    if pref.mute_all_until and pref.mute_all_until > now:
        return True
    return event in REVIEW_EVENTS and bool(pref.mute_review_until and pref.mute_review_until > now)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().tz_display)


def quiet_until(pref: NotificationPref, now: datetime) -> datetime | None:
    """End of the current quiet hours (naive UTC), or None outside them. Quiet hours use TZ_DISPLAY and may span
    midnight (22:00–07:00)."""
    start, end = pref.quiet_start, pref.quiet_end
    if start is None or end is None or start == end:
        return None
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz())
    minute = local.hour * 60 + local.minute
    inside = start <= minute < end if start < end else (minute >= start or minute < end)
    if not inside:
        return None
    end_local = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if end_local <= local:
        end_local += timedelta(days=1)
    return end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def save_prefs(session: Session, actor: Actor, *, events: set[str] | None = None, quiet: tuple | None = None,
               mute_all: datetime | None | str = "keep", mute_review: datetime | None | str = "keep",
               all_projects: bool | None = None) -> NotificationPref:
    """Update settings; "keep" leaves a mute unchanged, None switches it off. `quiet` = (start, end) in minutes or
    (None, None)."""
    pref = session.get(NotificationPref, actor.id)
    if pref is None:
        pref = NotificationPref(actor_id=actor.id, all_projects=False)
        session.add(pref)
    if events is not None:
        pref.events = None if set(events) >= set(EVENTS) else json.dumps(sorted(set(events) & set(EVENTS)))
    if quiet is not None:
        pref.quiet_start, pref.quiet_end = quiet
    if mute_all != "keep":
        pref.mute_all_until = mute_all
    if mute_review != "keep":
        pref.mute_review_until = mute_review
    if all_projects is not None:
        pref.all_projects = bool(all_projects) and actor.role == "admin"
    session.flush()
    return pref


def until_text(until: datetime | None) -> str:
    """Mute end for messages: "off", "until switched off" or the local time (TZ_DISPLAY)."""
    if until is None:
        return _("off")
    if until >= FOREVER:
        return _("until switched off")
    local = until.replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz())
    return _("until %(time)s", time=local.strftime(date_format("datetime")))


def parse_duration(text: str) -> timedelta | None:
    """'30m', '2h', '1d', '1w' (also '2 h'); None if not a duration."""
    import re

    m = re.fullmatch(r"\s*(\d{1,4})\s*([mhdw])\w*\s*", (text or "").lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        return None
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def parse_until(text: str, now: datetime) -> datetime | None:
    """Mute end from a command argument: a duration ('2h', see `parse_duration`) or a clock time ('18:00',
    'bis 18:00', 'until 6:30', in TZ_DISPLAY; the next such time). Return naive UTC, None if not understood."""
    import re

    delta = parse_duration(text)
    if delta is not None:
        return now + delta
    m = re.fullmatch(r"\s*(?:(?:bis|until|till)\s+)?(\d{1,2})[:.](\d{2})\s*(?:uhr)?\s*", (text or "").lower())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz())
    end = local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


# --- Events -----------------------------------------------------------------------------------

@dataclass
class _Row:
    actor_id: int
    event: str
    task_nr: str | None
    text: str
    status: str
    next_attempt_at: datetime | None
    last_error: str | None = None


def recipients(session: Session, project: Project, extra: int | None = None) -> list[Actor]:
    """Active humans to notify for a project: the owner and admins with "all projects" (plus `extra`, e.g. the
    task's human reviewer)."""
    ids = [project.owner_id] + ([extra] if extra else [])
    ids += session.scalars(select(NotificationPref.actor_id).join(Actor, Actor.id == NotificationPref.actor_id)
                           .where(NotificationPref.all_projects.is_(True), Actor.role == "admin")).all()
    out, seen = [], set()
    for actor_id in ids:
        actor = session.get(Actor, actor_id)
        if actor is not None and actor.id not in seen and actor.actor_type == "human" and actor.is_active:
            seen.add(actor.id)
            out.append(actor)
    return out


def _app_url(session: Session) -> str:
    return (settings_store.get(session, "app_url") or "").rstrip("/")


def _short(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= CONTENT_CHARS else text[:CONTENT_CHARS - 1].rstrip() + "…"


def task_text(session: Session, task: Task, event: str, content: str | None = None) -> str:
    """Message text in the server's default language. local_only: task number, event and link only."""
    project = session.get(Project, task.project_id)
    base = _app_url(session)
    link = f"{base}/tasks/{task.task_nr}" if base else None
    with use_language(default_language()):
        lines = [f"{task.task_nr} · {_(EVENTS[event])}"]
        if project.privacy != "local_only":
            lines.append(task.title)
            lines.append(_("Project: %(key)s – %(name)s", key=project.project_key, name=project.name))
            if task.assignee is not None:
                lines.append(_("Agent: %(name)s", name=task.assignee.name))
            if content and content.strip():
                lines.append("")
                lines.append(_short(content))
    if link:
        lines.append(link)
    return "\n".join(lines)


def _rows(session: Session, project: Project, event: str, task_nr: str | None, text: str,
          extra: int | None = None) -> list[_Row]:
    if not telegram.enabled(session):
        return []
    now, rows = utcnow(), []
    for actor in recipients(session, project, extra):
        if session.get(TelegramLink, actor.id) is None:
            continue
        pref = prefs(session, actor.id)
        if event not in enabled_events(pref):
            continue
        if is_muted(pref, event, now):
            rows.append(_Row(actor.id, event, task_nr, "", "skipped", None, "muted"))
            continue
        until = quiet_until(pref, now)
        rows.append(_Row(actor.id, event, task_nr, text, "held" if until else "queued", until))
    return rows


def _store(session: Session, rows: list[_Row], detached: bool) -> None:
    if not rows:
        return
    objs = [Notification(actor_id=r.actor_id, event=r.event, task_nr=r.task_nr, text=r.text or None,
                         status=r.status, next_attempt_at=r.next_attempt_at, last_error=r.last_error,
                         created_at=utcnow()) for r in rows]
    if not detached:
        session.add_all(objs)
        return
    # The caller rolls back (e.g. merge conflict) – keep the notification anyway. Written once the caller's
    # transaction has ended: before that, its row locks (actors, tasks) would block the insert.
    if not session.info.get("detached_listener"):
        session.info["detached_listener"] = True
        event.listen(session, "after_transaction_end", _write_detached)
    session.info.setdefault("detached_notifications", []).extend(objs)


def _write_detached(session: Session, transaction) -> None:
    if transaction.parent is not None or transaction.nested:
        return
    objs = session.info.pop("detached_notifications", [])
    if not objs:
        return
    from app.db import db

    try:
        with db.session() as own:
            own.add_all(objs)
            own.commit()
    except Exception:  # noqa: BLE001
        log.exception("notify_failed", extra={"event": objs[0].event, "task": objs[0].task_nr})


def store_settings_notice(session: Session, actor_id: int, text: str) -> None:
    """Queue a message about changed settings (event `SETTINGS_EVENT`: not muted, not held in quiet hours)."""
    _store(session, [_Row(actor_id, SETTINGS_EVENT, None, text, "queued", None)], False)


def task_event(session: Session, task: Task, event: str, content: str | None = None,
               detached: bool = False) -> None:
    """Queue `event` for the task's recipients. Never raises: a notification problem must not break the workflow."""
    try:
        project = session.get(Project, task.project_id)
        # A human entered as the task's reviewer hears about the review too.
        reviewer = session.get(Actor, task.reviewer_id) if task.reviewer_id and event == "review" else None
        extra = reviewer.id if reviewer is not None and reviewer.actor_type == "human" else None
        rows = _rows(session, project, event, task.task_nr, task_text(session, task, event, content), extra)
        _store(session, rows, detached)
        log.info("notify", extra={"event": event, "task": task.task_nr, "queued": len(rows)})
    except Exception:  # noqa: BLE001
        log.exception("notify_failed", extra={"event": event, "task": task.task_nr})


def sprint_done_check(session: Session, task: Task) -> None:
    """After a task reached done/cancelled: notify once when it was the sprint's last open task."""
    if task.sprint_id is None or task.status not in ("done", "cancelled"):
        return
    try:
        statuses = set(session.scalars(select(Task.status).where(Task.sprint_id == task.sprint_id)))
        if not statuses <= {"done", "cancelled"} or "done" not in statuses:
            return
        sprint = session.get(Sprint, task.sprint_id)
        project = session.get(Project, sprint.project_id)
        base = _app_url(session)
        with use_language(default_language()):
            head = _(EVENTS["sprint_done"])
            text = (f"{project.project_key} · {head}" if project.privacy == "local_only"
                    else f"{project.project_key} · {head}\n{sprint.name}")
        if base:
            text += f"\n{base}/projects/{project.project_key}/sprints/{sprint.id}"
        _store(session, _rows(session, project, "sprint_done", None, text), False)
    except Exception:  # noqa: BLE001
        log.exception("notify_failed", extra={"event": "sprint_done", "task": task.task_nr})


# --- Delivery ---------------------------------------------------------------------------------

def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** max(attempts - 1, 0), 3600))


def _finish(rows: list[Notification], status: str, error: str | None, now: datetime) -> None:
    for n in rows:
        n.status, n.text, n.last_error = status, None, (error or None) and error[:200]
        if status == "sent":
            n.sent_at = now


def _chunks(rows: list[Notification]) -> list[tuple[list[Notification], str]]:
    """Combine due messages of one person into as few Telegram messages as possible; messages held during quiet
    hours are sent as one collected message."""
    held = [n for n in rows if n.status == "held"]
    out: list[tuple[list[Notification], str]] = []
    if len(held) > 1:
        with use_language(default_language()):
            header = _("Collected during quiet hours (%(num)s):", num=len(held))
        group, text = [], header
        for n in held:
            part = "\n\n" + (n.text or "")
            if group and len(text) + len(part) > telegram.MAX_TEXT:
                out.append((group, text))
                group, text = [], header
            group.append(n)
            text += part
        out.append((group, text))
        rows = [n for n in rows if n.status != "held"]
    out += [([n], n.text or "") for n in rows]
    return out


def deliver(session: Session, api=None, now: datetime | None = None) -> dict:
    """Send due notifications; retry with back-off, honour Telegram's retry_after. Return counts."""
    now = now or utcnow()
    counts = {"sent": 0, "failed": 0, "retry": 0, "skipped": 0}
    session.execute(delete(Notification).where(Notification.created_at < now - timedelta(days=LOG_DAYS)))
    due = list(session.scalars(select(Notification).where(
        Notification.status.in_(("queued", "held")),
        (Notification.next_attempt_at.is_(None)) | (Notification.next_attempt_at <= now))
        .order_by(Notification.id).limit(200)))
    if not due:
        return counts
    from app.services import telegram_actions  # Imports the state machine, which imports this module

    api = api or telegram.api(session)
    if api is None:  # Bot switched off: keep the queue, try again later.
        return counts
    by_actor: dict[int, list[Notification]] = {}
    for n in due:
        by_actor.setdefault(n.actor_id, []).append(n)
    sends = 0
    for actor_id, rows in by_actor.items():
        link = session.get(TelegramLink, actor_id)
        if link is None:
            _finish(rows, "failed", "not linked", now)
            counts["failed"] += len(rows)
            continue
        pref = prefs(session, actor_id)
        muted = [n for n in rows if is_muted(pref, n.event, now)]
        if muted:  # Muted after queueing (e.g. while held during quiet hours).
            _finish(muted, "skipped", "muted", now)
            counts["skipped"] += len(muted)
            rows = [n for n in rows if n not in muted]
        for group, text in _chunks(rows):
            if sends >= MAX_SENDS_PER_RUN:
                return counts
            sends += 1
            markup = None
            if len(group) == 1:  # Buttons and reply hints only on messages about one task
                with use_language(default_language()):
                    markup = telegram_actions.event_markup(session, actor_id, group[0].event, group[0].task_nr)
                    if group[0].event == "question":
                        text += "\n\n↩ " + _("Reply to this message to answer the question.")
            try:
                sent = api.send_message(link.chat_id, text, reply_markup=markup)
            except telegram.TelegramError as e:
                if e.retry_after:  # Rate limit: wait as told, for every chat.
                    for n in group:
                        n.next_attempt_at = now + timedelta(seconds=e.retry_after)
                    counts["retry"] += len(group)
                    log.warning("telegram_rate_limited", extra={"retry_after": e.retry_after})
                    return counts
                for n in group:
                    n.attempts += 1
                if e.permanent or group[0].attempts >= MAX_ATTEMPTS:
                    _finish(group, "failed", str(e), now)
                    counts["failed"] += len(group)
                else:
                    for n in group:
                        n.last_error, n.next_attempt_at = str(e)[:200], now + _backoff(n.attempts)
                    counts["retry"] += len(group)
                log.warning("telegram_send_failed", extra={"error": str(e), "chat_actor": actor_id})
                continue
            _finish(group, "sent", None, now)
            for n in group:
                n.telegram_message_id = (sent or {}).get("message_id")
            counts["sent"] += len(group)
    return counts


def recent_log(session: Session, actor_id: int | None = None, limit: int = 30) -> list[Notification]:
    q = select(Notification).order_by(Notification.id.desc()).limit(limit)
    if actor_id is not None:
        q = q.where(Notification.actor_id == actor_id)
    return list(session.scalars(q))
