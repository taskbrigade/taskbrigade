"""Email as a second notification channel: SMTP settings, address verification, message building and delivery.

TaskBrigade only sends mail, it never reads a mailbox (no IMAP, no replies, no commands by mail;
docs/konzept-email.md). Rows of `notifications` with `channel = "email"` are sent by `deliver` (scheduler job
`email_deliver`, only in the process holding `<state_dir>/email-sender.lock`) with the same rules as Telegram: retry
with back-off, at most `notifications.MAX_ATTEMPTS` attempts, text cleared once sent or given up. Sending stays
below `mail_rate_per_hour`. Only errors the SMTP server reports at once are seen; bounces go to the provider mailbox.

Everything is stdlib (`smtplib`, `email`); headers are set through `email.message.EmailMessage`, which refuses line
breaks in header values, and subjects/names are flattened before that. Tests replace `ssl_context_factory` to trust
the fake server's certificate (`tests/smtp_fake.py`).
"""
import fcntl
import hashlib
import html
import logging
import os
import re
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid, parseaddr
from urllib.parse import urlparse

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import NotFound, ProbeFailed, Unprocessable
from app.i18n import default_language, use_language
from app.i18n import gettext as _
from app.models import Actor, EmailVerifyCode, Notification
from app.services import notifications, settings_store
from app.services.actors import utcnow

log = logging.getLogger(__name__)

LOCK_FILE = "email-sender.lock"
VERIFY_HOURS = 24
MAX_SENDS_PER_RUN = 20
TIMEOUT_S = 20.0
SUBJECT_PREFIX = "[TaskBrigade]"
ADDRESS_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
URL_RE = re.compile(r"https?://[^\s<>\"]+")

ssl_context_factory = ssl.create_default_context  # Tests trust the fake server's certificate instead.


class MailError(Exception):
    """A failed SMTP step with a plain message (never the password). `permanent`: a retry cannot help (5xx for the
    recipient or the message); connection, TLS and login problems are retried – they are configuration or outages."""

    def __init__(self, message: str, code: int | None = None, permanent: bool = False, connection: bool = False):
        super().__init__(message)
        self.code, self.permanent, self.connection = code, permanent, connection


# --- Settings -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    host: str
    port: int
    security: str
    user: str | None
    password: str | None
    from_address: str
    from_name: str
    rate_per_hour: int


def check_address(value: str) -> str:
    """A single plain address (no display name, no line breaks); raises ValueError."""
    value = (value or "").strip()
    if not value or len(value) > 200 or any(c in value for c in "\r\n\t ") or not ADDRESS_RE.fullmatch(value):
        raise ValueError(value)
    if parseaddr(value)[1] != value:
        raise ValueError(value)
    return value


def config(session: Session) -> Config:
    get = settings_store.get
    return Config(host=get(session, "smtp_host") or "", port=int(get(session, "smtp_port") or 587),
                  security=get(session, "smtp_security") or "starttls", user=get(session, "smtp_user"),
                  password=get(session, "smtp_password"), from_address=get(session, "mail_from_address") or "",
                  from_name=get(session, "mail_from_name") or "TaskBrigade",
                  rate_per_hour=int(get(session, "mail_rate_per_hour") or 60))


def configured(session: Session) -> bool:
    return bool(settings_store.get(session, "smtp_host") and settings_store.get(session, "mail_from_address"))


def enabled(session: Session) -> bool:
    return settings_store.get_bool(session, "email_enabled") and configured(session)


def verified_address(actor: Actor | None) -> str | None:
    """The person's address if it is confirmed – no mail goes to any other address."""
    if actor is None or actor.actor_type != "human" or not actor.is_active:
        return None
    return actor.email if actor.email and actor.email_verified_at else None


def _base_url(session: Session) -> str:
    return (settings_store.get(session, "app_url") or "").rstrip("/")


def settings_url(session: Session, base: str | None = None) -> str | None:
    base = (base or _base_url(session)).rstrip("/")
    return f"{base}/profile#notifications" if base else None


# --- Messages -----------------------------------------------------------------------------------

def _flat(value: str | None, limit: int = 200) -> str:
    """One header line: control characters and line breaks become spaces."""
    return " ".join("".join(c if c.isprintable() else " " for c in (value or "")).split())[:limit]


def _domain(session: Session, cfg: Config, base_url: str | None = None) -> str:
    """Own host name for Message-IDs: the TaskBrigade URL's host, else the sender's domain."""
    host = urlparse(base_url or _base_url(session)).hostname
    return host or cfg.from_address.rpartition("@")[2] or "taskbrigade.invalid"


def _html(text: str, footer: list[str]) -> str:
    def para(block: str) -> str:
        out = []
        for line in block.split("\n"):
            pieces, pos = [], 0
            for m in URL_RE.finditer(line):
                pieces.append(html.escape(line[pos:m.start()]))
                url = html.escape(m.group(0))
                pieces.append(f'<a href="{url}">{url}</a>')
                pos = m.end()
            pieces.append(html.escape(line[pos:]))
            out.append("".join(pieces))
        return "<p>" + "<br>\n".join(out) + "</p>"

    body = "\n".join(para(b) for b in text.split("\n\n") if b.strip())
    foot = "<br>\n".join(para(f)[3:-4] for f in footer)
    return (f'<!doctype html>\n<html><body style="font-family: sans-serif; line-height: 1.4">\n{body}\n'
            f'<hr>\n<p style="color: #56636F; font-size: smaller">{foot}</p>\n</body></html>\n')


def build_message(session: Session, cfg: Config, to: str, to_name: str | None, subject: str, text: str,
                  base_url: str | None = None) -> EmailMessage:
    """Plain text plus simple HTML, no tracking. Call in the server's language."""
    unsubscribe = settings_url(session, base_url)
    footer = [_("Replies to this mail are not processed – please answer via the link in TaskBrigade.")]
    if unsubscribe:
        footer.append(_("Notification settings: %(url)s", url=unsubscribe))
    msg = EmailMessage()
    msg["From"] = formataddr((_flat(cfg.from_name, 100), check_address(cfg.from_address)))
    msg["To"] = formataddr((_flat(to_name, 100), check_address(to)))
    msg["Subject"] = _flat(subject)
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid(domain=_domain(session, cfg, base_url))
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
    if unsubscribe:
        msg["List-Unsubscribe"] = f"<{unsubscribe}>"
    msg.set_content(text + "\n\n-- \n" + "\n".join(footer) + "\n")
    msg.add_alternative(_html(text, footer), subtype="html")
    return msg


def subject_for(rows: list[Notification]) -> str:
    """`[TaskBrigade] <TASK-NR> · <event>` (the first line of the notification text), or a count for a bundle."""
    if len(rows) == 1:
        first = (rows[0].text or "").split("\n", 1)[0]
        return f"{SUBJECT_PREFIX} {first}"
    return f"{SUBJECT_PREFIX} " + _("%(num)s notifications", num=len(rows))


def body_for(rows: list[Notification]) -> str:
    if len(rows) == 1:
        return rows[0].text or ""
    if all(n.status == "held" for n in rows):
        header = _("Collected during quiet hours (%(num)s):", num=len(rows))
    else:
        header = _("Collected notifications (%(num)s):", num=len(rows))
    return header + "".join("\n\n" + (n.text or "") for n in rows)


# --- SMTP ---------------------------------------------------------------------------------------

def _reply(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return " ".join(str(value or "").split())[:150]


def _error(e: Exception) -> MailError:
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        code, reply = next(iter(e.recipients.values()), (None, b""))
        return MailError(_("SMTP %(code)s: recipient refused (%(reply)s)", code=code, reply=_reply(reply)), code,
                         permanent=bool(code and code >= 500))
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return MailError(_("SMTP %(code)s: login refused (%(reply)s)", code=e.smtp_code, reply=_reply(e.smtp_error)),
                         e.smtp_code)
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return MailError(_("The SMTP server does not support this (%(error)s)", error=_reply(str(e))))
    if isinstance(e, smtplib.SMTPConnectError):
        return MailError(_("SMTP %(code)s: connection refused (%(reply)s)", code=e.smtp_code,
                           reply=_reply(e.smtp_error)), e.smtp_code, connection=True)
    if isinstance(e, smtplib.SMTPResponseException):  # Sender refused, data refused, …
        return MailError(_("SMTP %(code)s: %(reply)s", code=e.smtp_code, reply=_reply(e.smtp_error)), e.smtp_code,
                         permanent=e.smtp_code >= 500)
    if isinstance(e, ssl.SSLError):
        return MailError(_("TLS error with the SMTP server (%(error)s)", error=_reply(getattr(e, "reason", None)
                                                                                    or type(e).__name__)),
                         connection=True)
    return MailError(_("SMTP server not reachable (%(error)s)", error=type(e).__name__), connection=True)


class Connection:
    """One SMTP session (STARTTLS on the submission port or implicit TLS); several messages per session."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        try:
            context = ssl_context_factory()
            if cfg.security == "tls":
                self.smtp = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=TIMEOUT_S, context=context)
            else:
                self.smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT_S)
                self.smtp.ehlo()
                self.smtp.starttls(context=context)
                self.smtp.ehlo()
            if cfg.user:
                self.smtp.login(cfg.user, cfg.password or "")
        except (smtplib.SMTPException, OSError) as e:
            self.close()
            raise _error(e) from None

    def send(self, msg: EmailMessage, to: str | list[str]) -> dict:
        """Send to one or several envelope recipients; return the refused ones (`{address: (code, reply)}`) when
        the server accepted at least one."""
        try:
            return self.smtp.send_message(msg, from_addr=self.cfg.from_address,
                                          to_addrs=[to] if isinstance(to, str) else list(to))
        except (smtplib.SMTPException, OSError) as e:
            err = _error(e)
            # SMTPException derives from OSError: only a lost session or a socket error is a connection problem.
            if isinstance(e, smtplib.SMTPServerDisconnected) or not isinstance(e, smtplib.SMTPException):
                err.connection = True
            raise err from None

    def close(self) -> None:
        smtp = getattr(self, "smtp", None)
        if smtp is None:
            return
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            smtp.close()
        self.smtp = None


def send_now(session: Session, to: str, to_name: str | None, subject: str, text: str,
             base_url: str | None = None) -> str:
    """Send one message synchronously (test mail, verification link); return its Message-ID. Raises MailError."""
    if not configured(session):
        raise MailError(_("Set the SMTP host and the sender address first"))
    cfg = config(session)
    try:
        msg = build_message(session, cfg, to, to_name, subject, text, base_url)
    except ValueError:
        raise MailError(_("Invalid email address")) from None
    conn = Connection(cfg)
    try:
        conn.send(msg, to)
    finally:
        conn.close()
    return msg["Message-ID"]


def send_test(session: Session, to: str) -> str:
    with use_language(default_language()):
        return send_now(session, to, None, f"{SUBJECT_PREFIX} " + _("Test mail"),
                        _("This is a test mail from TaskBrigade. The SMTP settings work."))


# --- Address verification -----------------------------------------------------------------------

def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_verify_code(session: Session, actor: Actor) -> tuple[str, datetime]:
    """New one-time link code for the person's current address; earlier unused codes become invalid."""
    if actor.actor_type != "human":
        raise Unprocessable(_("Only humans can receive email notifications"))
    if not actor.email:
        raise Unprocessable(_("Enter an email address first"))
    now = utcnow()
    session.execute(delete(EmailVerifyCode).where((EmailVerifyCode.actor_id == actor.id)
                                                  | (EmailVerifyCode.expires_at < now - timedelta(days=1))))
    code = secrets.token_urlsafe(24)
    expires = now + timedelta(hours=VERIFY_HOURS)
    session.add(EmailVerifyCode(actor_id=actor.id, email=actor.email, code_hash=hash_code(code), created_at=now,
                                expires_at=expires))
    session.flush()
    return code, expires


def send_verification(session: Session, actor: Actor, base_url: str) -> datetime:
    """Mail a confirmation link to the person's (not yet confirmed) address – the only mail such an address gets."""
    if not enabled(session):
        raise MailError(_("Email notifications are not enabled (Admin → Settings → Email)"))
    code, expires = create_verify_code(session, actor)
    base = (_base_url(session) or base_url).rstrip("/")
    with use_language(default_language()):
        text = _("Please confirm that TaskBrigade may send notifications for %(name)s to this address:",
                 name=actor.display_name or actor.name) + f"\n{base}/profile/email/verify?code={code}\n\n" + _(
            "The link works once and is valid for 24 hours. If you did not ask for this, ignore this mail.")
        send_now(session, actor.email, actor.display_name, f"{SUBJECT_PREFIX} " + _("Confirm your email address"),
                 text, base)
    return expires


def pending_code(session: Session, actor: Actor) -> EmailVerifyCode | None:
    return session.scalar(select(EmailVerifyCode).where(
        EmailVerifyCode.actor_id == actor.id, EmailVerifyCode.used_at.is_(None),
        EmailVerifyCode.expires_at > utcnow(), EmailVerifyCode.email == actor.email))


def redeem(session: Session, code: str) -> Actor | None:
    """Confirm the address the code was sent to. None if the code is unknown, used, expired or the person's
    address changed since."""
    row = session.scalar(select(EmailVerifyCode).where(EmailVerifyCode.code_hash == hash_code((code or "").strip())))
    now = utcnow()
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    actor = session.get(Actor, row.actor_id)
    if actor is None or not actor.is_active or (actor.email or "").lower() != row.email.lower():
        return None
    # Conditional update: of two concurrent redemptions only one wins.
    if session.execute(update(EmailVerifyCode).where(EmailVerifyCode.id == row.id, EmailVerifyCode.used_at.is_(None))
                       .values(used_at=now)).rowcount != 1:
        return None
    actor.email_verified_at = now
    session.flush()
    return actor


# --- Delivery -----------------------------------------------------------------------------------

_lock_fd: int | None = None


def owns_lock() -> bool:
    """Take (or keep) the sender lock – only one process per server sends mail."""
    global _lock_fd
    if _lock_fd is not None:
        return True
    state = get_settings().tbr_state_dir
    try:
        state.mkdir(parents=True, exist_ok=True)
        fd = os.open(state / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        log.warning("email_lock_failed", extra={"error": str(e)})
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _lock_fd = fd
    return True


def release_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            os.close(_lock_fd)  # Closing releases the flock.
        finally:
            _lock_fd = None


def sent_last_hour(session: Session, now: datetime) -> int:
    """Mails sent in the last hour (bundled rows share one Message-ID), result mails included."""
    from app.models import OutgoingMail

    notes = session.scalar(select(func.count(func.distinct(Notification.email_message_id))).where(
        Notification.channel == "email", Notification.sent_at >= now - timedelta(hours=1),
        Notification.email_message_id.is_not(None))) or 0
    results = session.scalar(select(func.count(OutgoingMail.id)).where(
        OutgoingMail.status == "sent", OutgoingMail.sent_at >= now - timedelta(hours=1))) or 0
    return notes + results


def _groups(rows: list[Notification], bundle: int, now: datetime) -> list[list[Notification]]:
    """Mails to send now for one person. Immediately: one mail per row, rows held during quiet hours as one
    collected mail. Bundled: everything due in one mail once the oldest row waited `bundle` minutes."""
    held = [n for n in rows if n.status == "held"]
    if bundle:
        if not held and not any(n.attempts for n in rows) and \
                min(n.created_at for n in rows) + timedelta(minutes=bundle) > now:
            return []
        return [rows]
    queued = [n for n in rows if n.status != "held"]
    return ([held] if held else []) + [[n] for n in queued]


def deliver(session: Session, now: datetime | None = None) -> dict:
    """Send due email notifications; retry with back-off, stay below `mail_rate_per_hour`. Return counts."""
    now = now or utcnow()
    counts = {"sent": 0, "failed": 0, "retry": 0, "skipped": 0, "throttled": 0}
    session.execute(delete(Notification).where(Notification.created_at < now - timedelta(days=notifications.LOG_DAYS)))
    if not enabled(session):  # Channel off: keep the queue.
        return counts
    due = list(session.scalars(select(Notification).where(
        Notification.channel == "email", Notification.status.in_(("queued", "held")),
        (Notification.next_attempt_at.is_(None)) | (Notification.next_attempt_at <= now))
        .order_by(Notification.id).limit(500)))
    if not due:
        return counts
    cfg = config(session)
    budget = min(MAX_SENDS_PER_RUN, cfg.rate_per_hour - sent_last_hour(session, now))
    by_actor: dict[int, list[Notification]] = {}
    for n in due:
        by_actor.setdefault(n.actor_id, []).append(n)
    conn: Connection | None = None
    sends = 0
    try:
        for actor_id, rows in by_actor.items():
            actor = session.get(Actor, actor_id)
            to = verified_address(actor)
            if to is None:
                notifications._finish(rows, "failed", "no confirmed address", now)
                counts["failed"] += len(rows)
                continue
            pref = notifications.prefs(session, actor_id)
            muted = [n for n in rows if notifications.is_muted(pref, n.event, now)]
            if muted:  # Muted after queueing (e.g. while held during quiet hours).
                notifications._finish(muted, "skipped", "muted", now)
                counts["skipped"] += len(muted)
                rows = [n for n in rows if n not in muted]
            if not rows:
                continue
            for group in _groups(rows, notifications.bundle_minutes(pref), now):
                if sends >= budget:
                    counts["throttled"] += len(group)
                    continue
                sends += 1
                try:
                    with use_language(default_language()):
                        msg = build_message(session, cfg, to, actor.display_name, subject_for(group),
                                            body_for(group))
                except ValueError:  # Stored address or sender address is not a plain address
                    notifications._finish(group, "failed", "invalid address", now)
                    counts["failed"] += len(group)
                    continue
                try:
                    conn = conn or Connection(cfg)
                    conn.send(msg, to)
                except MailError as e:
                    for n in group:
                        n.attempts += 1
                    if e.permanent or group[0].attempts >= notifications.MAX_ATTEMPTS:
                        notifications._finish(group, "failed", str(e), now)
                        counts["failed"] += len(group)
                    else:
                        for n in group:
                            n.last_error = str(e)[:200]
                            n.next_attempt_at = now + notifications._backoff(n.attempts)
                        counts["retry"] += len(group)
                    log.warning("email_send_failed", extra={"error": str(e), "mail_actor": actor_id})
                    if e.connection:  # Server unreachable or session lost: try the rest on the next run.
                        if conn is not None:
                            conn.close()
                        return counts
                    continue
                notifications._finish(group, "sent", None, now)
                for n in group:
                    n.email_message_id = msg["Message-ID"][:200]
                counts["sent"] += len(group)
    finally:
        if conn is not None:
            conn.close()
    if counts["throttled"]:
        log.info("email_throttled", extra={"rate_per_hour": cfg.rate_per_hour, "waiting": counts["throttled"]})
    return counts


# --- Doctor check --------------------------------------------------------------------------------

LOG_WINDOW = 10  # delivery rows the "keeps failing" warning looks at (no contents)


def _last_delivery(session: Session) -> dict:
    """Last delivery of the email channel from the log: sent_at of the newest sent row, plus the status and
    error of the newest row of any state – no message contents (the log keeps none)."""
    last = {
        "sent_at": session.scalar(select(Notification.sent_at).where(
            Notification.channel == "email", Notification.sent_at.is_not(None),
            Notification.status == "sent").order_by(Notification.sent_at.desc()).limit(1)),
        "last_status": session.scalar(select(Notification.status).where(
            Notification.channel == "email").order_by(Notification.id.desc()).limit(1)),
        "last_error": session.scalar(select(Notification.last_error).where(
            Notification.channel == "email").order_by(Notification.id.desc()).limit(1)),
    }
    return {key: (value.isoformat() if isinstance(value, datetime) else value) for key, value in last.items()}


def _failures_in_a_row(session: Session) -> int:
    """Delivery rows at the end of the log that are failed or waiting to retry. The count reflects the log
    and stays until new deliveries succeed or fall out of the window – a successful probe does not clear
    it, and the failure itself may have happened days ago (the log keeps 30 days)."""
    ids = session.scalars(select(Notification.id).where(Notification.channel == "email")
                          .order_by(Notification.id.desc()).limit(LOG_WINDOW))
    ids = list(ids)
    rows = list(session.scalars(select(Notification).where(Notification.id.in_(ids) if ids else False))) \
        if ids else []
    count = 0
    for row in sorted(rows, key=lambda n: n.id, reverse=True):
        if row.status == "failed" or (row.attempts and row.status in ("queued", "held")):
            count += 1
        else:
            break
    return count


def smtp_probe(session: Session) -> dict:
    """Reachability and login of the configured SMTP server without address and text: one session that is
    opened (EHLO + STARTTLS or implicit TLS + login) and closed again. Called from the server (probe
    endpoint for `tbr doctor` and Admin → Settings) over this module's configured settings. The delivery
    status comes from the log rows, never from a probe of the outbox itself. A connect, TLS or login
    failure raises 502 `bad_gateway` (probe of the external SMTP server failed there; the TaskBrigade
    server itself is healthy) with the plain SMTP reason – never the password."""
    cfg = config(session)
    if not enabled(session):
        raise NotFound(_("Email notifications are not enabled (Admin → Settings → Email)"),
                       code="email_disabled")
    try:
        conn = Connection(cfg)  # Raises MailError with a plain text (never the password).
    except MailError as e:
        raise ProbeFailed(str(e)) from None
    conn.close()
    return {
        "ok": True,
        "host": cfg.host,
        "security": cfg.security,
        "last_delivery": _last_delivery(session),
        "failures_in_a_row": _failures_in_a_row(session),
    }
