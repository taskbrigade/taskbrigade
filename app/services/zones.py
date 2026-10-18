"""Data zones of agents (research/konzept-datenzonen.md §1/§2): which data classes an actor may work on.

`actors.runtime` says where an agent process runs, `actors.model_location` where its model runs – the place content
is sent to for inference. With the setting `privacy_enforce_model_location` on, an agent may work on `local_only`
only if both are local; off, the old rule applies (runtime only). Every privacy decision (assignment, project access,
reviewer, review jobs, claim, runtime/model switch, project privacy switch) goes through this module.

Humans keep the runtime rule in both modes (their model location is not used). `model_location = none` (runner, no
LLM) never gets `cloud_ok`/`local_only` work, independent of the setting – there are no such agents before the
runner exists, so nothing changes for existing installations.
"""
from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session, object_session

from app.errors import Forbidden, Unprocessable
from app.i18n import N_, _
from app.models import Actor, Project, ProjectMember, Task

SETTING = "privacy_enforce_model_location"
DATA_CLASSES = ("cloud_ok", "local_only", "sealed")
MODEL_LOCATIONS = ("cloud", "local", "none")
MODEL_LOCATION_LABELS = {"cloud": N_("cloud model"), "local": N_("local model"), "none": N_("no model (runner)")}
# These programs always talk to a cloud API, so they cannot be marked as local.
CLOUD_ONLY_KINDS = frozenset({"claude_code", "codex"})
OPEN_STATUSES = ("draft", "defined", "in_progress", "questions", "aborted", "for_review", "changes_requested")

# Strictness order: a higher rank is stricter (rule 1 "stricter is always allowed").
CLASS_RANK = {"cloud_ok": 0, "local_only": 1, "sealed": 2}
# Task kinds executed by the runner (no LLM): their class is always `sealed` (rule 3, concept §2).
RUNNER_TASK_KINDS = frozenset({"profile", "run"})
# Statuses before any work started; a task that was claimed once counts as started even after a reset (rule 4).
NOT_STARTED = ("draft", "defined")

_CLOUD = frozenset({"cloud_ok"})
_LOCAL = frozenset({"cloud_ok", "local_only"})
_SEALED = frozenset({"sealed"})
_HUMAN_LOCAL = frozenset({"cloud_ok", "local_only", "sealed"})


def enforced(session: Session | None) -> bool:
    """`privacy_enforce_model_location`; without a session (transient objects) the old runtime rule applies."""
    from app.services import settings_store

    return session is not None and settings_store.get_bool(session, SETTING)


def effective_class(project: Project, task: Task | None = None) -> str:
    """Data class of a task: its own class (`tasks.data_class`) if set, else the project's privacy (rule: inherit)."""
    return getattr(task, "data_class", None) or project.privacy


def sql_effective_class():
    """SQL form of `effective_class` for queries joining `Task` and `Project`."""
    return func.coalesce(Task.data_class, Project.privacy)


def allowed_classes(actor: Actor, enforce: bool) -> frozenset[str]:
    """The data classes the actor may work on. `sealed` (zone R, no LLM): runners and local humans with the data
    right (`actors.data_access`, research/konzept-datenzonen.md §1)."""
    if actor.actor_type == "agent" and actor.model_location == "none":
        return _SEALED
    if actor.runtime == "cloud":
        return _CLOUD
    if actor.actor_type != "agent":
        return _HUMAN_LOCAL if actor.data_access else _LOCAL
    if enforce and actor.model_location != "local":
        return _CLOUD
    return _LOCAL


INHERIT_VALUES = ("", "inherit", "project")


def normalize_class(value: str | None) -> str | None:
    """Task data class from a form/API value; empty or `inherit` = None (the project's privacy applies)."""
    v = (value or "").strip().lower()
    if v in INHERIT_VALUES:
        return None
    if v not in DATA_CLASSES:
        raise Unprocessable(_("Data class must be one of %(values)s or 'inherit'", values=", ".join(DATA_CLASSES)),
                            code="invalid_data_class")
    return v


def started(task: Task) -> bool:
    """Work on the task has begun (rule 4): past `defined`, or claimed once (a reset keeps `claimed_at`)."""
    return task.status not in NOT_STARTED or task.claimed_at is not None


def may_loosen(project: Project, actor: Actor | None) -> bool:
    """Rule 2: only the project owner and admins may set a class looser than the project's privacy."""
    from app.services import access

    return access.can_manage_project(project, actor)


def check_class(project: Project, task: Task, new: str | None, by: Actor | None) -> None:
    """Rules 1–4 of research/konzept-datenzonen.md §1 for setting the class `new` (None = inherit) on `task` (a
    transient task for a new one). Rule 1 (stricter is always allowed) needs no check; the assignee and reviewer
    are checked by the caller against the new class."""
    target = new or project.privacy
    if task.task_kind in RUNNER_TASK_KINDS and target != "sealed":
        raise Unprocessable(_("Runner tasks (kind '%(kind)s') must be 'sealed'", kind=task.task_kind),
                            code="data_class_runner")
    current = effective_class(project, task)
    if task.id is not None and started(task) and CLASS_RANK[target] < CLASS_RANK[current]:
        raise Unprocessable(_("Work on %(task)s has started – its data class can only be made stricter (now "
                              "'%(current)s')", task=task.task_nr, current=current), code="data_class_downgrade")
    if CLASS_RANK[target] < CLASS_RANK[project.privacy] and not may_loosen(project, by):
        raise Forbidden(_("Only the project owner and admins may set a data class looser than the project's "
                          "('%(privacy)s')", privacy=project.privacy), code="data_class_forbidden")


def _session(*objs) -> Session | None:
    return next((s for s in map(object_session, objs) if s is not None), None)


def may_handle(project: Project, actor: Actor, task: Task | None = None, session: Session | None = None) -> bool:
    session = session or _session(actor, project)
    return effective_class(project, task) in allowed_classes(actor, enforced(session))


def may_read_dataset(actor: Actor, data_class: str, session: Session | None = None) -> bool:
    """DZ 14 (research/konzept-datenzonen.md §11): whether an agent may read a dataset of `data_class` over the API.
    Only `local_only` datasets are readable, and only by an agent whose allowed classes include `local_only`
    (`model_location=local`, plus `runtime=local` once enforced). `sealed` is never readable through this path –
    runner access to sealed raw data belongs to the DZ 6 job-scoped sandbox – and `model_location=none` agents are
    refused here too."""
    if data_class != "local_only":
        return False
    return "local_only" in allowed_classes(actor, enforced(session))


def check(project: Project, actor: Actor, task: Task | None = None, session: Session | None = None) -> None:
    """Refuse an actor that may not work on the project (`privacy_violation`)."""
    session = session or _session(actor, project)
    enforce = enforced(session)
    cls = effective_class(project, task)
    if cls in allowed_classes(actor, enforce):
        return
    if actor.actor_type == "agent" and actor.model_location == "none":
        raise Unprocessable(_("'%(actor)s' runs no model and only executes sealed runner jobs – it cannot work on "
                              "project %(project)s", actor=actor.name, project=project.project_key),
                            code="privacy_violation")
    if cls == "sealed":
        raise Unprocessable(_("Task %(task)s is 'sealed' – only runners (no model) and humans with the data right "
                              "may work on it, not '%(actor)s'", task=task.task_nr or _("(new)"), actor=actor.name),
                            code="privacy_violation")
    if getattr(task, "data_class", None) and cls != project.privacy:
        raise Unprocessable(_("Task %(task)s is 'local_only' – '%(actor)s' may not work on it (content would leave "
                              "the local environment).", task=task.task_nr or _("(new)"), actor=actor.name),
                            code="privacy_violation")
    if enforce and actor.runtime != "cloud":
        raise Unprocessable(
            _("Project %(project)s is 'local_only' – '%(actor)s' uses a cloud model (model location '%(location)s'), "
              "content would leave the local environment.", project=project.project_key, actor=actor.name,
              location=actor.model_location), code="privacy_violation")
    raise Unprocessable(
        _("Project %(project)s is 'local_only' – cloud actor '%(actor)s' is not allowed (code and context would leave the local environment).", project=project.project_key, actor=actor.name), code="privacy_violation")


def sql_may_handle(actor, privacy, enforce: bool):
    """SQL condition with the same rule as `allowed_classes`; `actor` is an `Actor` (alias), `privacy` the column
    with the data class (e.g. `Project.privacy`)."""
    is_agent = actor.actor_type == "agent"
    runner = and_(is_agent, actor.model_location == "none")
    local = actor.runtime != "cloud"
    sealed = or_(runner, and_(~is_agent, local, actor.data_access.is_(True)))
    if enforce:
        local = and_(local, or_(~is_agent, actor.model_location == "local"))
    return or_(and_(privacy == "sealed", sealed),
               and_(~runner, or_(privacy == "cloud_ok", and_(privacy == "local_only", local))))


def sql_classes(classes: frozenset[str], column):
    """SQL condition: `column` (a data class) is one of `classes`."""
    return column.in_(sorted(classes)) if classes else false()


def mixed_projects(session: Session, project_ids: list[int]) -> set[int]:
    """Projects with tasks (not cancelled) whose own data class differs from the project's privacy ("mixed")."""
    if not project_ids:
        return set()
    return set(session.scalars(select(Task.project_id).distinct().join(Project, Project.id == Task.project_id)
                               .where(Task.project_id.in_(project_ids), Task.status != "cancelled",
                                      Task.data_class.is_not(None), Task.data_class != Project.privacy)))


def check_switch(session: Session, actor: Actor, changes: dict) -> None:
    """Refuse a runtime/model location change that would leave the actor linked (member or open assignment) to
    projects it could no longer work on."""
    keys = ("runtime", "model_location")
    if not any(k in changes and changes[k] != getattr(actor, k) for k in keys):
        return
    enforce = enforced(session)
    after = Actor(actor_type=actor.actor_type, data_access=actor.data_access,
                  **{k: changes.get(k, getattr(actor, k)) for k in keys})
    lost = allowed_classes(actor, enforce) - allowed_classes(after, enforce)
    if not lost:
        return
    linked = linked_projects(session, actor, lost)
    if linked and after.runtime == "cloud" != actor.runtime:
        raise Unprocessable(
            _("'%(name)s' cannot switch to 'cloud': linked to local_only projects (%(projects)s) – reassign tasks and remove project access first", name=actor.name, projects=", ".join(sorted(linked))),
            code="privacy_violation")
    if linked:
        raise Unprocessable(
            _("'%(name)s' cannot make this change: linked to projects it could no longer work on (%(projects)s) – "
              "reassign tasks and remove project access first", name=actor.name, projects=", ".join(sorted(linked))),
            code="privacy_violation")


def linked_projects(session: Session, actor: Actor, classes) -> set[str]:
    """Projects with one of the data `classes` the actor is linked to: member or open assignment."""
    privacy = sql_classes(frozenset(classes), Project.privacy)
    member_of = session.scalars(select(Project.project_key).join(ProjectMember, ProjectMember.project_id == Project.id)
                                .where(ProjectMember.actor_id == actor.id, privacy))
    tasks_in = session.scalars(select(Project.project_key).join(Task, Task.project_id == Project.id)
                               .where(Task.assigned_to == actor.id, Task.status.in_(OPEN_STATUSES), privacy))
    return set(member_of) | set(tasks_in)


def is_runner(actor: Actor | None) -> bool:
    return actor is not None and actor.actor_type == "agent" and actor.model_location == "none"


def check_kind(actor: Actor, task_kind: str | None) -> None:
    """Runners (no model) get only `profile`/`run` tasks, LLM agents never get them (concept §2)."""
    if actor.actor_type != "agent":
        return
    runner_task = (task_kind or "work") in RUNNER_TASK_KINDS
    if is_runner(actor) and not runner_task:
        raise Unprocessable(_("'%(actor)s' is a runner and only executes 'profile'/'run' tasks", actor=actor.name),
                            code="runner_task_kind")
    if runner_task and not is_runner(actor):
        raise Unprocessable(_("Runner tasks (kind '%(kind)s') can only be assigned to a runner, not to '%(actor)s'",
                              kind=task_kind, actor=actor.name), code="runner_task_kind")


def sql_kind_for(actor: Actor):
    """SQL form of `check_kind` for the claim query."""
    kinds = sorted(RUNNER_TASK_KINDS)
    return Task.task_kind.in_(kinds) if is_runner(actor) else Task.task_kind.not_in(kinds)


def check_model_location(actor: Actor) -> None:
    """Valid model location for the actor's kind (`actors._validate`)."""
    if actor.model_location not in MODEL_LOCATIONS:
        raise Unprocessable(_("Model location must be one of %(values)s", values=", ".join(MODEL_LOCATIONS)))
    if actor.actor_type != "agent":
        return
    if actor.agent_kind in CLOUD_ONLY_KINDS and actor.model_location != "cloud":
        raise Unprocessable(_("%(kind)s always uses a cloud model – the model location must be 'cloud'",
                              kind=actor.agent_kind), code="invalid_model_location")
    if actor.model_location == "none" and actor.agent_kind != "runner":
        raise Unprocessable(_("Model location 'none' is reserved for runner agents"), code="invalid_model_location")
    if actor.agent_kind == "runner" and actor.model_location != "none":
        raise Unprocessable(_("A runner runs no model – its model location must be 'none'"),
                            code="invalid_model_location")


def require_confirmation(new: str | None, old: str | None, confirmed: bool) -> None:
    """UI rule: switching an agent to a local model needs the admin's explicit confirmation."""
    if new == "local" and old != "local" and not confirmed:
        raise Unprocessable(_("Confirm that this agent uses no cloud model before choosing “local model”"),
                            code="confirm_local_model")


def findings(session: Session) -> list[dict]:
    """Agents that work on `local_only` today but could not once `privacy_enforce_model_location` is on: their
    `local_only` memberships, open assignments and open review entries (research/konzept-datenzonen.md §9)."""
    out = []
    agents = session.scalars(select(Actor).where(Actor.actor_type == "agent").order_by(Actor.name))
    for actor in agents:
        if "local_only" not in allowed_classes(actor, False) or "local_only" in allowed_classes(actor, True):
            continue
        members = list(session.scalars(
            select(Project.project_key).join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.actor_id == actor.id, Project.privacy == "local_only").order_by(Project.project_key)))
        open_tasks = (select(Task.task_nr).join(Project, Project.id == Task.project_id)
                      .where(Task.status.in_(OPEN_STATUSES), Project.privacy == "local_only").order_by(Task.id))
        assigned = list(session.scalars(open_tasks.where(Task.assigned_to == actor.id)))
        reviews = list(session.scalars(open_tasks.where(Task.reviewer_id == actor.id)))
        if members or assigned or reviews:
            out.append({"actor": actor, "projects": members, "tasks": assigned, "reviews": reviews})
    return out
