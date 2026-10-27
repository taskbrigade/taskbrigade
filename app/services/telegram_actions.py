"""Acting from Telegram: review buttons and answers to agent questions.

Review messages carry inline buttons – *Approve* (asks for confirmation first), *Reject* (the next message becomes
the review comment) – and conflict messages *Return conflict*. Callback data is `tb:<token>`: a random token, only
its SHA-256 is stored, bound to the human, the task and an expiry. All buttons of one message share a keyboard id;
the first press uses the whole keyboard with a conditional UPDATE, so double clicks or a second button only repeat
the stored outcome.

A reply to a question message answers the question (same transition as the inbox). Questions of `local_only`
projects never reach Telegram, so they can be answered there only after the human opened the question in the
browser (`question_reads`); otherwise the bot answers with the link.

Every action runs as the linked human through the state machine (same rights as the web UI) and is written to the
audit log with `via="telegram"`. Replies for `local_only` projects contain task number, outcome and link only.
"""
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from app.errors import AppError, NotFound
from app.i18n import N_
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import Actor, Notification, Project, QuestionRead, Task, TaskComment, TelegramAction
from app.services import settings_store, state_machine, zones
from app.services import tasks as task_svc
from app.services.actors import utcnow

PREFIX = "tb:"
CALLBACK_RE = re.compile(r"^tb:([A-Za-z0-9_-]{22})$")
BUTTON_MINUTES = 7 * 24 * 60  # Buttons on notifications
CONFIRM_MINUTES = 30  # Confirmation buttons and the pending rejection reason
STALE_QUESTION_S = 5  # A question newer than the notification (plus this slack) is a different question
CONFLICT_CODES = frozenset({"merge_conflict", "brain_conflict", "changelog_invalid"})
KEEP = "keep"  # CallbackResult.markup: leave the pressed message's buttons as they are

DONE = {"approve": N_("%(nr)s merged and done."), "reject": N_("%(nr)s rejected."),
        "reject_conflict": N_("%(nr)s returned to the agent with conflict instructions."),
        "answer": N_("%(nr)s answered – can be picked up again.")}
AUDIT = {"approve": "review_approve", "reject": "review_reject", "reject_conflict": "review_reject_conflict",
         "answer": "question_answered"}
LOCAL_ERRORS = {
    "merge_conflict": N_("%(nr)s: merge conflict – no merge performed, the task remains in review."),
    "brain_conflict": N_("%(nr)s: brain-changes no longer apply – no merge performed, the task remains in review."),
    "changelog_invalid": N_("%(nr)s: the changelog fragment is invalid – no merge performed, the task remains in "
                            "review."),
    "merge_failed": N_("%(nr)s: merge failed – the task remains in review."),
    "forbidden": N_("%(nr)s: you are not allowed to do this."),
}


@dataclass
class CallbackResult:
    notice: str  # Shown briefly on the user's screen (answerCallbackQuery)
    markup: dict | None | str = KEEP  # New buttons for the pressed message; None removes them
    messages: list[tuple[str, dict | None]] = field(default_factory=list)  # (text, reply_markup) to send


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _link(session: Session, nr: str) -> str:
    base = (settings_store.get(session, "app_url") or "").rstrip("/")
    return f"\n{base}/tasks/{nr}" if base else ""


def keyboard(session: Session, actor_id: int, task_nr: str, buttons: list[tuple[str, str]], minutes: int,
             hidden: tuple[str, ...] = ()) -> dict:
    """Store one token per button (plus `hidden` kinds without a button, e.g. the pending rejection reason) under
    a new keyboard id; return the inline keyboard markup (one row)."""
    now = utcnow()
    session.execute(delete(TelegramAction).where(TelegramAction.expires_at < now - timedelta(days=1)))
    kb, row = secrets.token_hex(8), []
    for label, kind in [*buttons, *(("", k) for k in hidden)]:
        token = secrets.token_urlsafe(16)  # 22 characters; callback data may have up to 64 bytes
        session.add(TelegramAction(token_hash=_hash(token), keyboard=kb, actor_id=actor_id, kind=kind,
                                   task_nr=task_nr, created_at=now, expires_at=now + timedelta(minutes=minutes)))
        if label:
            row.append({"text": label, "callback_data": PREFIX + token})
    session.flush()
    return {"inline_keyboard": [row]}


def review_keyboard(session: Session, actor_id: int, task_nr: str) -> dict:
    return keyboard(session, actor_id, task_nr, [("✅ " + _("Approve"), "approve"), ("↩ " + _("Reject"), "reject")],
                    BUTTON_MINUTES)


def conflict_keyboard(session: Session, actor_id: int, task_nr: str) -> dict:
    return keyboard(session, actor_id, task_nr, [("↩ " + _("Return conflict"), "reject_conflict")], BUTTON_MINUTES)


def event_markup(session: Session, actor_id: int, event: str, task_nr: str | None) -> dict | None:
    """Buttons for a notification of one task (call in the message language)."""
    if not task_nr:
        return None
    if event == "review":
        return review_keyboard(session, actor_id, task_nr)
    if event == "conflict":
        return conflict_keyboard(session, actor_id, task_nr)
    return None


def _use(session: Session, kb: str, result: str | None = None) -> bool:
    """Use every token of a keyboard; False if another press was first."""
    return session.execute(update(TelegramAction).where(TelegramAction.keyboard == kb,
                                                        TelegramAction.used_at.is_(None))
                           .values(used_at=utcnow(), result=result)).rowcount > 0


def _set_result(session: Session, kb: str, result: str) -> None:
    session.execute(update(TelegramAction).where(TelegramAction.keyboard == kb).values(result=result[:200]))


def _local(session: Session, task: Task) -> bool:
    return zones.effective_class(session.get(Project, task.project_id), task) != "cloud_ok"


def _error_text(session: Session, e: AppError, task: Task) -> str:
    nr = task.task_nr
    if e.code == "invalid_transition":
        from app.web.common import status_label

        return _("%(nr)s is no longer waiting for this (status: %(status)s).", nr=nr, status=status_label(task.status))
    if _local(session, task):
        return _(LOCAL_ERRORS.get(e.code, N_("%(nr)s: the action failed.")), nr=nr)
    return f"{nr}: {e.message}"


def run(session: Session, actor: Actor, nr: str, action: str, body: str = "") -> tuple[bool, str, dict | None]:
    """Run a state machine action as `actor`; return (ok, reply text, buttons). Failures roll back only the action
    (savepoint) – notifications stored for a conflict survive, as in the web UI."""
    try:
        task = task_svc.get(session, nr, for_update=True)
    except NotFound:
        return False, _("Task %(nr)s no longer exists.", nr=nr), None
    try:
        with session.begin_nested():
            state_machine.transition(session, task, action, actor, {"body": body})
    except AppError as e:
        session.refresh(task)
        audit(actor.name, AUDIT[action], task=nr, via="telegram", outcome=e.code)
        markup = conflict_keyboard(session, actor.id, nr) if e.code in CONFLICT_CODES else None
        text = _error_text(session, e, task)
        if markup:
            text += "\n" + _("Use the button to return the task to the agent with instructions.")
        return False, text + _link(session, nr), markup
    audit(actor.name, AUDIT[action], task=nr, via="telegram", outcome="ok")
    return True, _(DONE[action], nr=nr) + _link(session, nr), None


def _may(session: Session, actor: Actor, nr: str, action: str) -> str | None:
    """Error text if `action` is not possible for `actor` right now (no side effects)."""
    try:
        task = task_svc.get(session, nr)
        state_machine.check_allowed(state_machine.RULES[action], task, actor,
                                    session.get(Project, task.project_id).owner_ids)
    except NotFound:
        return _("Task %(nr)s no longer exists.", nr=nr)
    except AppError as e:
        return _error_text(session, e, task)
    return None


# --- Buttons ----------------------------------------------------------------------------------------

def handle_callback(session: Session, actor: Actor, data: str) -> CallbackResult:
    """A button press of the linked human `actor`."""
    m = CALLBACK_RE.match(data or "")
    row = session.scalar(select(TelegramAction).where(TelegramAction.token_hash == _hash(m.group(1)))) if m else None
    if row is None or row.actor_id != actor.id or row.kind not in BUTTON_KINDS:
        return CallbackResult(_("This button is not valid."))
    if row.used_at is not None:
        return CallbackResult(_("Already done: %(result)s", result=row.result) if row.result
                              else _("This button was already used."))
    if row.expires_at <= utcnow():
        return CallbackResult(_("This button has expired."), None,
                              [(_("The buttons for %(nr)s have expired – open the task in TaskBrigade.",
                                  nr=row.task_nr) + _link(session, row.task_nr), None)])
    if not _use(session, row.keyboard):
        return CallbackResult(_("This button was already used."))
    return BUTTON_KINDS[row.kind](session, actor, row)


def _approve(session, actor, row) -> CallbackResult:
    error = _may(session, actor, row.task_nr, "approve")
    if error:
        _set_result(session, row.keyboard, error)
        return CallbackResult(error, None, [(error + _link(session, row.task_nr), None)])
    confirm = keyboard(session, actor.id, row.task_nr,
                       [("✅ " + _("Yes, merge %(nr)s", nr=row.task_nr), "approve_ok"),
                        ("✖ " + _("Cancel"), "approve_no")], CONFIRM_MINUTES)
    return CallbackResult(_("Please confirm the merge."), confirm)


def _back_to_review(session, actor, row) -> CallbackResult:
    pending = session.scalars(select(TelegramAction).where(TelegramAction.keyboard == row.keyboard,
                                                           TelegramAction.kind == "reject_reason"))
    for p in pending:
        p.result = _("Cancelled.")
    return CallbackResult(_("Cancelled."), review_keyboard(session, actor.id, row.task_nr))


def _approve_ok(session, actor, row) -> CallbackResult:
    ok, text, markup = run(session, actor, row.task_nr, "approve")
    _set_result(session, row.keyboard, text.split("\n")[0])
    return CallbackResult(_("Merged.") if ok else _("Not merged."), None, [(text, markup)])


def _reject(session, actor, row) -> CallbackResult:
    error = _may(session, actor, row.task_nr, "reject")
    if error:
        _set_result(session, row.keyboard, error)
        return CallbackResult(error, None, [(error + _link(session, row.task_nr), None)])
    # Only one pending reason per person: an older one is replaced.
    session.execute(update(TelegramAction).where(TelegramAction.actor_id == actor.id,
                                                 TelegramAction.kind == "reject_reason",
                                                 TelegramAction.used_at.is_(None))
                    .values(used_at=utcnow(), result=_("Replaced by a newer rejection.")))
    cancel = keyboard(session, actor.id, row.task_nr, [("✖ " + _("Cancel rejection"), "reject_no")],
                      CONFIRM_MINUTES, hidden=("reject_reason",))
    prompt = _("Why is %(nr)s rejected? Send the reason as your next message – it becomes the review comment "
               "for the agent. /cancel stops.", nr=row.task_nr)
    return CallbackResult(_("Send the reason."), cancel, [(prompt, {"force_reply": True,
                                                                   "input_field_placeholder": _("Reason")})])


def _reject_conflict(session, actor, row) -> CallbackResult:
    ok, text, _markup = run(session, actor, row.task_nr, "reject_conflict")
    _set_result(session, row.keyboard, text.split("\n")[0])
    return CallbackResult(_("Returned.") if ok else _("Not returned."), None, [(text, None)])


BUTTON_KINDS = {"approve": _approve, "approve_ok": _approve_ok, "approve_no": _back_to_review,
                "reject": _reject, "reject_no": _back_to_review, "reject_conflict": _reject_conflict}


# --- Replies ----------------------------------------------------------------------------------------

def pending_reason(session: Session, actor: Actor) -> TelegramAction | None:
    return session.scalar(select(TelegramAction).where(
        TelegramAction.actor_id == actor.id, TelegramAction.kind == "reject_reason",
        TelegramAction.used_at.is_(None), TelegramAction.expires_at > utcnow())
        .order_by(TelegramAction.id.desc()).limit(1))


def reject_with_reason(session: Session, actor: Actor, text: str) -> str | None:
    """The message after *Reject*: reject the task with it as review comment. None = no rejection pending."""
    row = pending_reason(session, actor)
    if row is None or not _use(session, row.keyboard):
        return None
    _ok, reply, _markup = run(session, actor, row.task_nr, "reject", text)
    _set_result(session, row.keyboard, reply.split("\n")[0])
    return reply


def cancel_pending(session: Session, actor: Actor) -> bool:
    row = pending_reason(session, actor)
    return row is not None and _use(session, row.keyboard, _("Cancelled."))


def _last_question(session: Session, task: Task) -> TaskComment | None:
    return session.scalar(select(TaskComment).where(TaskComment.task_id == task.id,
                                                    TaskComment.comment_type == "question")
                          .order_by(TaskComment.id.desc()).limit(1))


def answer_reply(session: Session, actor: Actor, message_id: int, text: str) -> str | None:
    """A reply to a sent question message answers that question. None = not a reply to a question message."""
    sent = list(session.scalars(select(Notification).where(Notification.actor_id == actor.id,
                                                           Notification.telegram_message_id == message_id,
                                                           Notification.event == "question")))
    if not sent:
        return None
    if len({n.task_nr for n in sent}) > 1:
        return _("This message covers several questions – reply to the message of a single question or answer "
                 "in TaskBrigade.")
    nr = sent[0].task_nr
    try:
        task = task_svc.get(session, nr)
    except NotFound:
        return _("Task %(nr)s no longer exists.", nr=nr)
    question = _last_question(session, task)
    if task.status != "questions" or question is None:
        return _("%(nr)s has no open question any more.", nr=nr) + _link(session, nr)
    if question.created_at > max(n.created_at for n in sent) + timedelta(seconds=STALE_QUESTION_S):
        return _("%(nr)s has a newer question – reply to its message or answer in TaskBrigade.", nr=nr) \
            + _link(session, nr)
    if _local(session, task) and session.get(QuestionRead, (actor.id, question.id)) is None:
        return _("%(nr)s belongs to a local-only project, so Telegram does not show the question. Read it in "
                 "TaskBrigade first, then reply here again.", nr=nr) + _link(session, nr)
    return run(session, actor, nr, "answer", text)[1]


def mark_questions_read(session: Session, actor: Actor | None, task_nrs: list[str],
                        now: datetime | None = None) -> None:
    """The human sees these tasks' current questions in the browser (task page, inbox). Only local_only projects
    need the record."""
    if actor is None or actor.actor_type != "human" or not task_nrs:
        return
    latest = (select(TaskComment.task_id, TaskComment.id.label("cid"))
              .join(Task, Task.id == TaskComment.task_id).join(Project, Project.id == Task.project_id)
              .where(Task.task_nr.in_(task_nrs), Task.status == "questions", zones.sql_effective_class() != "cloud_ok",
                     TaskComment.comment_type == "question"))
    ids: dict[int, int] = {}
    for task_id, cid in session.execute(latest):
        ids[task_id] = max(cid, ids.get(task_id, 0))
    for cid in ids.values():
        session.execute(insert(QuestionRead).prefix_with("IGNORE").values(
            actor_id=actor.id, comment_id=cid, read_at=now or utcnow()))
