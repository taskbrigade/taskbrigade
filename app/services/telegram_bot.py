"""Telegram bot: chat linking with one-time codes, bot commands and the long-polling thread.

Linking: the profile creates a short-lived one-time code (only its SHA-256 is stored); `/start <code>` to the bot
links that chat to the human. Messages and button presses from chats that are not linked are ignored (except
`/start <code>`). Every command acts as the linked human; buttons and replies are handled in `telegram_actions`.

Exactly one poller per server: the thread holds `<state_dir>/telegram-bot.lock` (flock) and only the holder polls and
delivers. Network or API errors never leave the thread; it backs off and tries again.
"""
import fcntl
import hashlib
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import Unprocessable
from app.i18n import default_language, use_language
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import Actor, Project, TelegramLink, TelegramLinkCode
from app.services import notifications, settings_store, telegram, telegram_actions
from app.services import tasks as task_svc
from app.services.actors import utcnow

log = logging.getLogger(__name__)

CODE_MINUTES = 15
LOCK_FILE = "telegram-bot.lock"


# --- Link codes ---------------------------------------------------------------------------------

def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_code(session: Session, actor: Actor) -> tuple[str, datetime]:
    """New link code for a human; earlier unused codes of the person become invalid. Return (code, expires_at)."""
    if actor.actor_type != "human":
        raise Unprocessable(_("Only humans can link a Telegram chat"))
    now = utcnow()
    session.execute(delete(TelegramLinkCode).where((TelegramLinkCode.actor_id == actor.id)
                                                   | (TelegramLinkCode.expires_at < now - timedelta(days=1))))
    code = secrets.token_urlsafe(12)  # Fits Telegram's deep link parameter ([A-Za-z0-9_-], up to 64 characters)
    expires = now + timedelta(minutes=CODE_MINUTES)
    session.add(TelegramLinkCode(actor_id=actor.id, code_hash=hash_code(code), created_at=now, expires_at=expires))
    session.flush()
    return code, expires


def pending_code(session: Session, actor: Actor) -> TelegramLinkCode | None:
    return session.scalar(select(TelegramLinkCode).where(
        TelegramLinkCode.actor_id == actor.id, TelegramLinkCode.used_at.is_(None),
        TelegramLinkCode.expires_at > utcnow()))


def redeem(session: Session, code: str, chat_id: int, chat_name: str | None) -> Actor | None:
    """Link `chat_id` to the code's human. None if the code is unknown, used or expired."""
    row = session.scalar(select(TelegramLinkCode).where(TelegramLinkCode.code_hash == hash_code(code.strip())))
    now = utcnow()
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    actor = session.get(Actor, row.actor_id)
    if actor is None or not actor.is_active:
        return None
    # Conditional update: of two concurrent redemptions only one wins.
    if session.execute(update(TelegramLinkCode).where(TelegramLinkCode.id == row.id,
                                                      TelegramLinkCode.used_at.is_(None))
                       .values(used_at=now)).rowcount != 1:
        return None
    session.execute(delete(TelegramLink).where((TelegramLink.chat_id == chat_id)
                                               | (TelegramLink.actor_id == actor.id)))
    session.flush()
    session.add(TelegramLink(actor_id=actor.id, chat_id=chat_id, chat_name=(chat_name or "")[:100] or None,
                             linked_at=now))
    session.flush()
    return actor


def link_of(session: Session, actor_id: int) -> TelegramLink | None:
    return session.get(TelegramLink, actor_id)


def unlink(session: Session, actor_id: int) -> bool:
    return session.execute(delete(TelegramLink).where(TelegramLink.actor_id == actor_id)).rowcount > 0


# --- Commands -----------------------------------------------------------------------------------

# Command names in both languages; Telegram's command menu shows the ones of the server language.
MUTE = ("/mute", "/stumm", "/pause")
UNMUTE = ("/unmute", "/laut", "/resume")
HELP = ("/help", "/hilfe")
CANCEL = ("/cancel", "/abbrechen")
OFF, ON = ("off", "aus"), ("on", "an", "ein")
INBOX_LIMIT = 10


def menu() -> list[tuple[str, str]]:
    """Command menu for setMyCommands (call in the server language)."""
    de = default_language() == "de"
    return [("inbox", _("What is waiting for you")), ("stumm" if de else "pause", _("Mute messages [2h | until 18:00]")),
            ("laut" if de else "resume", _("Unmute messages")), ("reviews", _("Review requests off/on")),
            ("status", _("Link, mute and server")), ("hilfe" if de else "help", _("All commands"))]


def _help() -> str:
    return _("Commands:\n/inbox – what is waiting for you\n/pause [2h | until 18:00] (also /mute) – mute all messages; "
             "without a duration until /resume\n/resume (also /unmute) – unmute\n/reviews off|on [2h] – review "
             "requests only\n/status – link, mute and server\n/stop – unlink this chat\n\nButtons on review messages "
             "approve, reject or return a conflict. Reply to a question message to answer it.")


_until_text = notifications.until_text


def _status(session: Session, actor: Actor) -> str:
    pref = notifications.prefs(session, actor.id)
    now = utcnow()
    mute_all = pref.mute_all_until if pref.mute_all_until and pref.mute_all_until > now else None
    mute_review = pref.mute_review_until if pref.mute_review_until and pref.mute_review_until > now else None
    events = ", ".join(_(notifications.EVENTS[e]) for e in notifications.EVENTS if e in
                       notifications.enabled_events(pref)) or _("none")
    from app.services.distribution import version_label

    server = (settings_store.get(session, "app_url") or "").rstrip("/") or _("address not set")
    version = version_label()
    return _("Linked with %(name)s.\nMuted (all messages): %(all)s\nMuted (review requests): %(review)s\n"
             "Events: %(events)s", name=actor.display_name or actor.name, all=_until_text(mute_all),
             review=_until_text(mute_review), events=events) + "\n" + _(
        "Server: %(server)s%(version)s", server=server, version=f" (TaskBrigade {version})" if version else "")


def _parse_until(rest: str) -> datetime | None | str:
    """FOREVER without an argument, the end time, or "invalid"."""
    if not rest.strip():
        return notifications.FOREVER
    return notifications.parse_until(rest, utcnow()) or "invalid"


def _unknown_duration(rest: str) -> str:
    return _("Unknown duration '%(value)s' – e.g. 30m, 2h, 1d, 1w or until 18:00", value=rest)


def _mute(session: Session, actor: Actor, args: list[str], on: bool) -> str:
    reviews = bool(args) and args[0].lower() in ("reviews", "review")
    rest = " ".join(args[1:] if reviews else args)
    until = _parse_until(rest) if on else None
    if until == "invalid":
        return _unknown_duration(rest)
    audit(actor.name, "notifications_muted" if on else "notifications_unmuted", scope="reviews" if reviews else "all",
          until=until.isoformat() if until else None, via="telegram")
    if reviews:
        notifications.save_prefs(session, actor, mute_review=until)
        return (_("Review requests muted %(until)s.", until=_until_text(until)) if on
                else _("Review requests unmuted."))
    notifications.save_prefs(session, actor, mute_all=until)
    return _("All messages muted %(until)s.", until=_until_text(until)) if on else _("Messages unmuted.")


def _reviews(session: Session, actor: Actor, args: list[str]) -> str:
    """/reviews off [duration] | on"""
    switch = args[0].lower() if args else ""
    if switch in OFF:
        return _mute(session, actor, ["reviews", *args[1:]], True)
    if switch in ON:
        return _mute(session, actor, ["reviews"], False)
    pref = notifications.prefs(session, actor.id)
    until = pref.mute_review_until if pref.mute_review_until and pref.mute_review_until > utcnow() else None
    return _("Review requests muted: %(until)s\nSwitch with /reviews off [2h] or /reviews on.",
             until=_until_text(until))


def _inbox(session: Session, actor: Actor) -> str:
    """Questions, reviews and aborted tasks of the person's projects (admins following all projects: all)."""
    pref = notifications.prefs(session, actor.id)
    owner = None if actor.role == "admin" and pref.all_projects else actor.id
    data = task_svc.inbox(session, owner)
    local = set(session.scalars(select(Project.project_key).where(Project.privacy == "local_only")))
    base = (settings_store.get(session, "app_url") or "").rstrip("/")
    sections = [("questions", _("Questions")), ("for_review", _("Reviews")), ("aborted", _("Aborted"))]
    out = []
    for key, label in sections:
        items = data[key]
        if not items:
            continue
        out.append(f"{label} ({len(items)})")
        for t in items[:INBOX_LIMIT]:
            line = f"• {t['task_nr']}" if t["project"] in local else f"• {t['task_nr']} {t['title']}"
            out.append(line + (f"\n  {base}/tasks/{t['task_nr']}" if base else ""))
        if len(items) > INBOX_LIMIT:
            out.append(_("… and %(num)s more", num=len(items) - INBOX_LIMIT))
        out.append("")
    if not out:
        return _("Nothing is waiting for you.")
    if base:
        out.append(_("Inbox: %(url)s", url=base + "/"))
    return "\n".join(out).strip()


def _text(session: Session, actor: Actor, msg: dict, text: str) -> str:
    """A message that is not a command: an answer (reply to a question message) or a rejection reason."""
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if reply_to:
        answered = telegram_actions.answer_reply(session, actor, int(reply_to), text)
        if answered is not None:
            return answered
    rejected = telegram_actions.reject_with_reason(session, actor, text)
    if rejected is not None:
        return rejected
    return _("TaskBrigade only understands commands here – or a reply to a question message.") + "\n" + _help()


def linked_actor(session: Session, chat_id: int) -> Actor | None:
    link = session.scalar(select(TelegramLink).where(TelegramLink.chat_id == chat_id))
    if link is None:
        return None
    actor = session.get(Actor, link.actor_id)
    return actor if actor is not None and actor.is_active else None


def handle_update(session: Session, update_: dict) -> str | None:
    """Process one message update; return the reply text (None = no reply). Runs in the default language."""
    msg = update_.get("message") or {}
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    if not chat.get("id"):
        return None
    chat_id = int(chat["id"])
    parts = text.split() or [""]
    command, args = parts[0].split("@", 1)[0].lower(), parts[1:]
    with use_language(default_language()):
        if command == "/start" and args:
            name = chat.get("username") or chat.get("title") or chat.get("first_name")
            actor = redeem(session, args[0], chat_id, name)
            if actor is None:
                return _("This link code is unknown, used or expired – create a new one in your TaskBrigade "
                         "profile.")
            audit(actor.name, "telegram_linked", via="telegram")
            return _("This chat is now linked with %(name)s. You will get TaskBrigade notifications here.",
                     name=actor.display_name or actor.name) + "\n\n" + _help()
        actor = linked_actor(session, chat_id)
        if actor is None:
            return None  # Not linked: ignore.
        if command == "/stop":
            unlink(session, actor.id)
            audit(actor.name, "telegram_unlinked", via="telegram")
            return _("This chat is no longer linked. Link it again with a new code from your profile.")
        if command == "/status":
            return _status(session, actor)
        if command in MUTE:
            return _mute(session, actor, args, True)
        if command in UNMUTE:
            return _mute(session, actor, args, False)
        if command == "/reviews":
            return _reviews(session, actor, args)
        if command == "/inbox":
            return _inbox(session, actor)
        if command in CANCEL:
            return (_("Rejection cancelled.") if telegram_actions.cancel_pending(session, actor)
                    else _("Nothing to cancel."))
        if text and not command.startswith("/"):
            return _text(session, actor, msg, text)
        return _help()


def handle_callback(session: Session, query: dict) -> telegram_actions.CallbackResult | None:
    """A button press; None for chats that are not linked (ignored)."""
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id") or (query.get("from") or {}).get("id")
    if not chat_id:
        return None
    actor = linked_actor(session, int(chat_id))
    if actor is None:
        return None
    with use_language(default_language()):
        return telegram_actions.handle_callback(session, actor, str(query.get("data") or ""))


# --- Poller -------------------------------------------------------------------------------------

class BotThread(threading.Thread):
    """Long polling and delivery for the whole server. Holds the bot lock; without it the thread only waits."""

    IDLE_S = 30.0
    MAX_BACKOFF_S = 300.0
    POLL_TIMEOUT_S = 25

    def __init__(self) -> None:
        super().__init__(name="telegram-bot", daemon=True)
        self.stop_event = threading.Event()
        self.lock_fd: int | None = None
        self.offset: int | None = None
        self.token_hash: str | None = None
        self.backoff = 0.0

    @property
    def owns_lock(self) -> bool:
        return self.lock_fd is not None

    def acquire_lock(self) -> bool:
        if self.lock_fd is not None:
            return True
        state = get_settings().tbr_state_dir
        try:
            state.mkdir(parents=True, exist_ok=True)
            fd = os.open(state / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            log.warning("telegram_lock_failed", extra={"error": str(e)})
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.lock_fd = fd
        return True

    def release_lock(self) -> None:
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)  # Closing releases the flock.
            finally:
                self.lock_fd = None

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                wait = self.step()
                if wait:
                    self.stop_event.wait(wait)
        finally:
            self.release_lock()

    def step(self) -> float:
        """One polling round; return seconds to wait before the next one. Never raises."""
        from app.db import db

        try:
            if not self.acquire_lock() or not db.ready:
                return self.IDLE_S
            with db.session() as session:
                api = telegram.api(session)
                token = telegram.token(session) or ""
            if api is None:
                return self.IDLE_S
            digest = hashlib.sha256(token.encode()).hexdigest()
            if digest != self.token_hash:  # New token: other bot, start over.
                self.token_hash, self.offset = digest, None
                me = api.get_me() or {}
                telegram.bot_info["username"] = me.get("username")
                try:
                    with use_language(default_language()):
                        api.set_commands(menu())
                except telegram.TelegramError as e:  # Only the command menu is missing
                    log.warning("telegram_commands_failed", extra={"error": str(e)})
            updates = api.get_updates(self.offset, self.POLL_TIMEOUT_S)
            for upd in updates:
                self.offset = int(upd.get("update_id", 0)) + 1
                self.handle(api, upd)
            self.backoff = 0.0
            return 0.0
        except telegram.TelegramError as e:
            if e.status == 401:
                self.token_hash = None
            self.backoff = min(max(self.backoff * 2, 5.0), self.MAX_BACKOFF_S)
            if e.retry_after:
                self.backoff = max(self.backoff, float(e.retry_after))
            log.warning("telegram_poll_failed", extra={"error": str(e), "retry_in": self.backoff})
            return self.backoff
        except Exception:  # noqa: BLE001 – the thread must survive anything
            self.backoff = min(max(self.backoff * 2, 5.0), self.MAX_BACKOFF_S)
            log.exception("telegram_poll_crashed")
            return self.backoff

    def handle(self, api, upd: dict) -> None:
        if "callback_query" in upd:
            self.handle_callback(api, upd["callback_query"])
            return
        from app.db import db

        chat_id = ((upd.get("message") or {}).get("chat") or {}).get("id")
        try:
            with db.session() as session:
                reply = handle_update(session, upd)
                session.commit()
        except Exception:  # noqa: BLE001
            log.exception("telegram_update_failed")
            return
        if reply and chat_id:
            try:
                api.send_message(chat_id, reply)
            except telegram.TelegramError as e:
                log.warning("telegram_reply_failed", extra={"error": str(e)})

    def handle_callback(self, api, query: dict) -> None:
        from app.db import db

        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        try:
            with db.session() as session:
                result = handle_callback(session, query)
                session.commit()
        except Exception:  # noqa: BLE001
            log.exception("telegram_update_failed")
            result = None
            if chat_id:
                with use_language(default_language()):
                    result = telegram_actions.CallbackResult(_("Something went wrong – please try again in "
                                                               "TaskBrigade."))
        if result is None:
            return  # Not linked: ignore without an answer.
        try:
            api.answer_callback(query.get("id"), result.notice)
            if chat_id and message.get("message_id") and result.markup != telegram_actions.KEEP:
                api.edit_reply_markup(chat_id, message["message_id"], result.markup)
            for text, markup in result.messages if chat_id else []:
                api.send_message(chat_id, text, reply_markup=markup)
        except telegram.TelegramError as e:
            log.warning("telegram_reply_failed", extra={"error": str(e)})


bot_thread: BotThread | None = None


def start() -> BotThread:
    global bot_thread
    bot_thread = BotThread()
    bot_thread.start()
    return bot_thread


def shutdown() -> None:
    global bot_thread
    if bot_thread is not None:
        bot_thread.stop()
        bot_thread = None


def owns_lock() -> bool:
    return bot_thread is not None and bot_thread.owns_lock
