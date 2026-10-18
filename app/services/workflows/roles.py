"""Resolve project roles at activation; previews/publication report candidates without binding them."""
from sqlalchemy import or_, select

from app.models import Actor, ProjectMember, Task, WorkflowRoleBinding
from app.services import access, reviews, zones


def data_class(run, step):
    return step.data_class or run.project.privacy


def resolve(session, run, step, *, lock=True, exclude=(), task=None):
    fixed_id = step.assignee_id
    if step.step_type == "approval" and step.reviewer_id and not (fixed_id or step.agent_role):
        fixed_id = step.reviewer_id
    if bool(fixed_id) == bool(step.agent_role):
        return None
    if fixed_id:
        candidates = [(session.get(Actor, fixed_id), 0)]
    else:
        candidates = session.execute(select(Actor, WorkflowRoleBinding.rank).join(
            WorkflowRoleBinding, WorkflowRoleBinding.actor_id == Actor.id).where(
                WorkflowRoleBinding.project_id == run.project_id,
                WorkflowRoleBinding.role == step.agent_role).order_by(Actor.id)).all()
    eligible = []
    cls = zones.effective_class(run.project, task) if task is not None else data_class(run, step)
    for candidate, rank in candidates:
        # A locking read serializes capacity decisions with task claims and other activations.
        query = select(Actor).where(Actor.id == candidate.id) if candidate else None
        if query is not None and lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        actor = session.scalar(query) if query is not None else None
        if actor is None or not actor.is_active or actor.id in exclude:
            continue
        if session.get(ProjectMember, (run.project_id, actor.id)) is None:
            continue
        if not access.may_use_agent(run.template.owner, actor):
            continue
        if cls not in zones.allowed_classes(actor, zones.enforced(session)):
            continue
        if step.step_type in ("agent", "script") and (actor.actor_type != "agent" or actor.model_location == "none"):
            continue
        if step.step_type == "runner" and (actor.actor_type != "agent" or actor.agent_kind != "runner"
                                           or actor.model_location != "none" or actor.runtime != "local"):
            continue
        if step.step_type == "approval" and (not reviews.eligible(actor) or (
                step.review_mode == "manual" and actor.actor_type != "human")):
            continue
        load_query = select(Task.id).where(or_(
            (Task.assigned_to == actor.id) & Task.status.notin_(("done", "cancelled")),
            (Task.reviewer_id == actor.id) & (Task.status == "for_review")))
        load = len(session.scalars(load_query.with_for_update() if lock else load_query).all())
        if actor.actor_type == "agent" and load >= actor.max_parallel_tasks:
            continue
        eligible.append((rank, load, actor.id, actor))
    return min(eligible, key=lambda item: item[:3])[3] if eligible else None
