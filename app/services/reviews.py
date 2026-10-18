"""Reviewers per task and review jobs for reviewer agents.

A task may name a reviewer (`tasks.reviewer_id`): a human or an agent with role `reviewer`; empty = the project
owner and admins review as before. New tasks take the project's default reviewer (`projects.default_reviewer_id`).
The owner and admins keep the right to approve, reject or return a conflict at any time (e.g. to overrule an agent).

Rules:
- No self-review: the reviewer must not be the assignee (refused when either is set; at submit an invalid reviewer
  is dropped with a note in the thread and the review falls back to the humans).
- A reviewer agent sees a task in `for_review` as a *review job*: it claims it (`tasks.claim(..., reviews=True)`)
  with its own lease (`review_claimed_at`/`review_lease_expires_at`, `lease_minutes` of the agent), keeps it alive
  with the heartbeat and decides through the same review actions as a human (`state_machine` approve/reject/
  reject_conflict). An expired review lease returns the job to the reviewer's queue. Review jobs count towards
  `max_parallel_tasks`; with `serialize_reviews` a reviewer holds at most one review job per project.
- Model for review sessions: the task's model choice resolved for the reviewer (`model_for`) – a concrete model only
  if the reviewer allows it, otherwise the reviewer's tier mapping or default model. No separate review model field.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Conflict, Forbidden, Unprocessable
from app.i18n import _
from app.models import Actor, Project, ProjectMember, Task
from app.services import access, members, model_choice, zones
from app.services.state_machine import add_comment, add_event, utcnow

NONE_VALUES = ("", "none", "-")


def eligible(actor: Actor | None) -> bool:
    """May be entered as reviewer: an active human or an active agent with role reviewer."""
    return actor is not None and actor.is_active and (
        actor.actor_type == "human" or (actor.actor_type == "agent" and actor.role == "reviewer"))


def is_agent(actor: Actor | None) -> bool:
    return actor is not None and actor.actor_type == "agent"


def find(session: Session, name: str | None) -> Actor | None:
    """Reviewer by name; None for an empty value or `none`."""
    if name is None or name.strip().lower() in NONE_VALUES:
        return None
    actor = session.scalar(select(Actor).where(Actor.name == name.strip()))
    if actor is None:
        raise Unprocessable(_("Actor '%(name)s' does not exist", name=name.strip()))
    return actor


def check(project: Project, reviewer: Actor, assignee_id: int | None, task: Task | None = None) -> None:
    """Refuse a reviewer that cannot review this task (role, inactive, privacy, self-review)."""
    if not reviewer.is_active:
        raise Unprocessable(_("Actor '%(name)s' is inactive", name=reviewer.name), code="invalid_reviewer")
    if not eligible(reviewer):
        raise Unprocessable(_("%(name)s cannot review: reviewers are humans or agents with the role 'reviewer'",
                              name=reviewer.name), code="invalid_reviewer")
    members.check_privacy(project, reviewer, task)
    if assignee_id is not None and reviewer.id == assignee_id:
        raise Unprocessable(_("%(name)s cannot review its own task – choose another reviewer or assignee",
                              name=reviewer.name), code="self_review")


def _ensure_access(session: Session, project: Project, reviewer: Actor, task: Task | None = None) -> None:
    """A reviewer agent needs read access to the repo (clone, diff); an existing membership is kept."""
    if reviewer.actor_type == "agent" and session.get(ProjectMember, (project.id, reviewer.id)) is None:
        members.set_member(session, project, reviewer, "read", task)


def set_reviewer(session: Session, task: Task, project: Project, reviewer: Actor | None, by: Actor | None,
                 note: str | None = None, project_default: bool = False) -> None:
    """Enter or remove the reviewer (validated against the current assignee); a held review job is released.
    `by` must be allowed to choose a reviewer agent (owner/public, `access.may_use_agent`) – except for the
    project's default reviewer, which the project owner has chosen."""
    if (reviewer.id if reviewer else None) == task.reviewer_id:
        return
    if task.status in ("done", "cancelled"):
        raise Conflict(_("The reviewer can only be changed until the task is approved"), code="invalid_transition")
    if reviewer is not None and not project_default:
        access.require_may_use_agent(by, reviewer)
    if reviewer is not None:
        check(project, reviewer, task.assigned_to, task)
        _ensure_access(session, project, reviewer, task)
    old = session.get(Actor, task.reviewer_id) if task.reviewer_id else None
    task.reviewer_id = reviewer.id if reviewer else None
    if task.review_claimed_at is not None:
        clear_claim(task)
    add_event(session, task, by, "reviewer", old.name if old else None, reviewer.name if reviewer else None, note)


def apply_default(session: Session, task: Task, project: Project, by: Actor) -> None:
    """New task: take the project's default reviewer unless it cannot review this task (then a note in the history)."""
    if not project.default_reviewer_id:
        return
    reviewer = session.get(Actor, project.default_reviewer_id)
    try:
        check(project, reviewer, task.assigned_to, task)
    except Unprocessable as e:
        add_event(session, task, by, "reviewer", None, None, _("project default not applied: %(reason)s",
                                                              reason=e.message)[:500])
        return
    set_reviewer(session, task, project, reviewer, by, _("project default"), project_default=True)


def effective(session: Session, task: Task) -> Actor | None:
    """The entered reviewer if it can review the task now, else None (the owner and admins review)."""
    if not task.reviewer_id:
        return None
    reviewer = session.get(Actor, task.reviewer_id)
    if not eligible(reviewer) or reviewer.id == task.assigned_to:
        return None
    project = session.get(Project, task.project_id)
    if not zones.may_handle(project, reviewer, task, session=session):
        return None
    return reviewer


def agent_reviewer(session: Session, task: Task) -> Actor | None:
    reviewer = effective(session, task)
    return reviewer if is_agent(reviewer) else None


def may_review(session: Session, task: Task, actor: Actor | None) -> bool:
    """`actor` is the task's entered reviewer and may decide (the owner/admin rule is checked separately)."""
    return actor is not None and task.reviewer_id == actor.id and effective(session, task) is not None


def on_submit(session: Session, task: Task, by: Actor) -> None:
    """At submission: a reviewer that cannot review the task (self-review, role, inactive, privacy) is dropped with
    a note – the review falls back to the owner and admins."""
    clear_claim(task)
    if not task.reviewer_id or effective(session, task) is not None:
        return
    reviewer = session.get(Actor, task.reviewer_id)
    if reviewer.id == task.assigned_to:
        reason = _("%(name)s is also the assignee (no self-review)", name=reviewer.name)
    else:
        reason = _("%(name)s cannot review this task (inactive, role or privacy)", name=reviewer.name)
    add_comment(session, task, by, "system", _("Reviewer removed: %(reason)s. The review falls back to the project "
                                               "owner and admins.", reason=reason))
    add_event(session, task, by, "reviewer", reviewer.name, None, reason[:500])
    task.reviewer_id = None


def clear_claim(task: Task) -> None:
    task.review_claimed_at = None
    task.review_lease_expires_at = None


def claimed_by(task: Task, actor: Actor) -> bool:
    """`actor` holds the task's review job."""
    return task.status == "for_review" and task.reviewer_id == actor.id and task.review_claimed_at is not None


def start(session: Session, task: Task, actor: Actor) -> None:
    """Claim the review job (or refresh an own one) with the reviewer's lease."""
    now = utcnow()
    renewed = task.review_claimed_at is not None
    if not renewed:
        task.review_claimed_at = now
    task.review_lease_expires_at = now + timedelta(minutes=actor.lease_minutes)
    if not renewed:
        add_event(session, task, actor, "review_claimed", None, actor.name)
    session.flush()


def heartbeat(session: Session, task: Task, actor: Actor) -> Task:
    task.review_lease_expires_at = utcnow() + timedelta(minutes=actor.lease_minutes)
    session.flush()
    return task


def release(session: Session, task: Task, actor: Actor, reason: str) -> Task:
    """The reviewer agent gives the review job back undecided (e.g. the runner could not start); it stays in the
    reviewer's queue."""
    if not claimed_by(task, actor):
        if task.reviewer_id != actor.id:
            raise Forbidden(_("%(task_nr)s: only the entered reviewer", task_nr=task.task_nr))
        raise Conflict(_("%(task_nr)s: no review job held", task_nr=task.task_nr), code="invalid_transition")
    reason = (reason or "").strip()
    if not reason:
        raise Unprocessable(_("%(label)s is required", label=_("Reason")), code="missing_field")
    clear_claim(task)
    add_comment(session, task, actor, "system", _("Review job released: %(reason)s", reason=reason))
    add_event(session, task, actor, "review_released", actor.name, None)
    session.flush()
    return task


def sweep_expired(session: Session) -> list[str]:
    """Expired review leases: the review job goes back to the reviewer's queue."""
    expired = list(session.scalars(select(Task).where(Task.status == "for_review",
                                                      Task.review_claimed_at.is_not(None),
                                                      Task.review_lease_expires_at < utcnow()).with_for_update()))
    for task in expired:
        clear_claim(task)
        add_event(session, task, None, "review_lease_expired")
    session.flush()
    return [t.task_nr for t in expired]


def model_for(task: Task, reviewer: Actor | None) -> dict:
    """Model for the review session: the task's choice resolved for the reviewer; a concrete model only if the
    reviewer allows it (otherwise its tier mapping or default model)."""
    if task.model and task.model not in model_choice.allowed(reviewer):
        task = Task(model=None, model_tier=task.model_tier)
    return model_choice.resolve(task, reviewer)


def decision_note(action: str, actor: Actor) -> str:
    """Thread note for a decision of a reviewer agent – readable in the thread and history."""
    texts = {"approve": _("Approved by %(name)s (reviewer agent)", name=actor.name),
             "reject": _("Rejected by %(name)s (reviewer agent)", name=actor.name),
             "reject_conflict": _("Conflict returned by %(name)s (reviewer agent)", name=actor.name)}
    return texts[action]


def candidates(session: Session, viewer: Actor | None = None) -> list[dict]:
    """Choices for reviewer fields: active humans and the active reviewer agents `viewer` sees (own and public ones,
    all with "Show all agents"; `access.agent_choice_filter`) – `name`, `kind` human/agent."""
    rows = session.execute(select(Actor.name, Actor.actor_type).where(
        Actor.is_active.is_(True), (Actor.actor_type == "human") | (Actor.role == "reviewer"),
        access.agent_choice_filter(viewer)).order_by(Actor.name))
    return [{"name": n, "kind": k} for n, k in rows]
