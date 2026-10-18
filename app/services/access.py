"""Project permissions when several people work with TaskBrigade.

Admins can do everything. A project's owner (always human) has the same rights in that project as a human
admin: approve, reject, answer questions, edit and reassign tasks, change project settings, and transfer
ownership. All signed-in humans can read and comment.
"""
from sqlalchemy import and_, or_, select, true
from sqlalchemy.orm import Session

from app.i18n import _
from app.errors import Forbidden
from app.models import Actor, Project, Task


def owns(project: Project | None, actor: Actor | None) -> bool:
    return (project is not None and actor is not None and actor.actor_type == "human"
            and project.owner_id == actor.id)


def can_manage_project(project: Project | None, actor: Actor | None) -> bool:
    return actor is not None and (actor.role == "admin" or owns(project, actor))


def require_manage_project(project: Project, actor: Actor) -> None:
    if not can_manage_project(project, actor):
        raise Forbidden(_("Project %(project)s: admins or the owner only", project=project.project_key))


def task_owner_id(session: Session, task: Task) -> int | None:
    return session.scalar(select(Project.owner_id).where(Project.id == task.project_id))


def owned_project_keys(session: Session, actor: Actor | None) -> set[str]:
    if actor is None:
        return set()
    return set(session.scalars(select(Project.project_key).where(Project.owner_id == actor.id)))


def own_projects_only(actor: Actor | None) -> bool:
    """Web UI display preference (profile "Show all projects"): when off, lists, filters and project choices show
    only projects the human owns. A display default, not a permission – other pages stay reachable."""
    return actor is not None and actor.actor_type == "human" and not actor.show_all_projects


# --- Agent visibility (owner + public) ------------------------------------------------------------------------
# Every agent has an owner (a human) and/or is public. Assignment and reviewer choices show a human only own and
# public agents; admins see only their own agents (not other owners' public ones) unless they switch on "Show all
# agents" in the profile. The server enforces the same rule on every
# assignment (`require_may_use_agent`): admins may use any agent, other humans own and public ones, agents (e.g. an
# orchestrator) public ones, themselves and those of their own owner.

def is_agent(actor: Actor | None) -> bool:
    return actor is not None and actor.actor_type == "agent"


def may_use_agent(by: Actor | None, agent: Actor | None, admin_all: bool = True) -> bool:
    """`by` may assign `agent` (as assignee or reviewer). Humans as assignees are not restricted.
    `admin_all=False`: admins only like everybody else (the default view of lists)."""
    if not is_agent(agent) or by is None or agent.is_public or agent.id == by.id or (admin_all and by.role == "admin"):
        return True
    if by.actor_type == "human":
        return agent.owner_id == by.id
    return agent.owner_id is not None and agent.owner_id == by.owner_id


def require_may_use_agent(by: Actor | None, agent: Actor | None) -> None:
    if not may_use_agent(by, agent):
        raise Forbidden(_("Agent '%(name)s' is neither public nor yours (nor your owner's)", name=agent.name),
                        code="agent_not_visible")


def all_agents_shown(viewer: Actor | None) -> bool:
    """Choices show every agent: no actor (bootstrap session) or a human admin with "Show all agents" on."""
    return viewer is None or (viewer.actor_type == "human" and viewer.role == "admin" and viewer.show_all_agents)


def own_agents_only(viewer: Actor | None) -> bool:
    """Profile switch "Show all agents" off for a human admin: choices, agent filters and Admin → Users & agents
    show only the agents the admin owns – also when other owners' agents are public. A display default only."""
    return (viewer is not None and viewer.actor_type == "human" and viewer.role == "admin"
            and not viewer.show_all_agents)


def agent_choice_filter(viewer: Actor | None):
    """SQL condition on `Actor` for assignment/reviewer choices: humans, plus the agents `viewer` may use – for
    admins without "Show all agents" only their own ones (a display default, the server allows all)."""
    if all_agents_shown(viewer):
        return true()
    if own_agents_only(viewer):
        return or_(Actor.actor_type == "human", Actor.owner_id == viewer.id)
    own = Actor.owner_id == viewer.id if viewer.actor_type == "human" else or_(
        Actor.id == viewer.id, and_(Actor.owner_id.is_not(None), Actor.owner_id == viewer.owner_id))
    return or_(Actor.actor_type == "human", Actor.is_public.is_(True), own)
