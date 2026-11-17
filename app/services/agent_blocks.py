"""Persistent blocked state of an agent (login expired, usage limit), reported by its runner.

The runner detects the blocker on the agent's screen (`cli/runner.py` BLOCKERS), gives the task back with
`signal: blocked` (immediate `agent_blocked` notification), writes `.agent-blocked` and reports it here with
`POST /api/v1/agents/me/blocked`. The state lives on the actor (`blocked_*`, migration 0042) until `tbr doctor`, the
runner's recovery check (`DELETE /api/v1/agents/me/blocked`) or an admin (Admin → Users & agents) clears it.

While it lasts: the inbox lists the agent ("Blocked agents"), task views mark tasks of the agent and tasks that wait
for them through dependencies (`task_hints`), and `remind` repeats the `agent_blocked` notification every
`agent_block_reminder_hours` (0 = off). Clearing queues one "agent ready again" message (`READY_EVENT`), sent on the
channels the person chose for `agent_blocked` and subject to the same mutes and quiet hours.
"""
from collections import deque
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.i18n import N_, date_format, default_language, use_language
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import Actor, NotificationPref, Project, Task, TaskDependency
from app.services import notifications, settings_store, state_machine
from app.services.actors import utcnow

KINDS = {"login": N_("login expired"), "limit": N_("usage limit reached"), "unsent": N_("start prompt unsent"),
         "other": N_("blocked")}
# Tasks that wait for the agent itself, and tasks that wait for those through dependencies.
DIRECT_STATUSES = ("defined", "changes_requested", "in_progress")
OPEN_STATUSES = tuple(sorted(state_machine.NON_END))
READY_EVENT = "agent_ready"
REMINDER_SETTING = "agent_block_reminder_hours"


def kind_label(kind: str | None) -> str:
    return _(KINDS.get(kind or "other", KINDS["other"]))


def blocked_actors(session: Session) -> list[Actor]:
    return list(session.scalars(select(Actor).where(Actor.blocked_since.is_not(None), Actor.actor_type == "agent")
                                .order_by(Actor.blocked_since, Actor.name)))


# --- Report and clear --------------------------------------------------------------------------------------------

def report(session: Session, actor: Actor, kind: str, reason: str, host: str | None = None,
           task_nr: str | None = None, since: datetime | None = None) -> bool:
    """Store the blocked state; idempotent. A repeated report of the same kind keeps `since` (and the reminder
    clock) and only refreshes reason, host and task. Returns True when the agent was not blocked with this kind."""
    now = utcnow()
    if since is not None and since.tzinfo is not None:
        since = since.astimezone(UTC).replace(tzinfo=None)
    since = min(since, now) if since else now
    new = actor.blocked_since is None or actor.blocked_kind != kind
    if new:
        actor.blocked_since, actor.blocked_reminded_at = since, None
        actor.blocked_host = actor.blocked_task_nr = None
    actor.blocked_kind = kind
    actor.blocked_reason = reason.strip()[:1000]
    actor.blocked_host = (host or "").strip()[:255] or actor.blocked_host
    actor.blocked_task_nr = (task_nr or "").strip().upper()[:30] or actor.blocked_task_nr
    session.flush()
    if new:
        audit(actor.name, "agent_blocked", kind=kind, host=actor.blocked_host, task=actor.blocked_task_nr)
    return new


def clear(session: Session, actor: Actor, by: Actor | None = None, via: str = "runner") -> bool:
    """Remove the blocked state and queue "agent ready again"; False if the agent was not blocked."""
    if actor.blocked_since is None:
        return False
    info = actor.blocked_info
    waiting = waiting_counts(session, [actor.id]).get(actor.id) or _empty()
    actor.blocked_kind = actor.blocked_reason = actor.blocked_host = actor.blocked_task_nr = None
    actor.blocked_since = actor.blocked_reminded_at = None
    session.flush()
    audit(by.name if by is not None else actor.name, "agent_unblocked", agent=actor.name, via=via)
    _notify(session, actor, info, waiting, READY_EVENT)
    return True


# --- Waiting tasks -----------------------------------------------------------------------------------------------

def _empty() -> dict:
    return {"direct": [], "deps": [], "projects": set()}


def _graph(session: Session) -> dict[int, list[int]]:
    """Open dependency edges as {task id: [ids of open tasks that depend on it]}."""
    dep, on = aliased(Task), aliased(Task)
    rows = session.execute(
        select(TaskDependency.depends_on_task_id, TaskDependency.task_id)
        .join(dep, dep.id == TaskDependency.task_id).join(on, on.id == TaskDependency.depends_on_task_id)
        .where(dep.status.in_(OPEN_STATUSES),
               on.status.not_in(tuple(state_machine.RESOLVED_DEPENDENCY_STATUSES)))).all()
    out: dict[int, list[int]] = {}
    for parent, child in rows:
        out.setdefault(parent, []).append(child)
    return out


def _walk(graph: dict[int, list[int]], roots: list[int]) -> dict[int, int]:
    """Every task reachable from `roots` through `graph`, as {task id: root id it waits for} (first root wins)."""
    seen: dict[int, int] = {}
    queue = deque((r, r) for r in roots)
    while queue:
        node, root = queue.popleft()
        for child in graph.get(node, []):
            if child not in seen and child not in roots:
                seen[child] = root
                queue.append((child, root))
    return seen


def waiting_counts(session: Session, actor_ids: list[int]) -> dict[int, dict]:
    """Per agent: open tasks assigned to it (`direct`), tasks that wait for those through dependencies (`deps`,
    without the direct ones) and the project ids of both."""
    if not actor_ids:
        return {}
    rows = session.execute(select(Task.id, Task.assigned_to, Task.project_id)
                           .where(Task.assigned_to.in_(actor_ids), Task.status.in_(DIRECT_STATUSES))).all()
    graph = _graph(session) if rows else {}
    out = {a: _empty() for a in actor_ids}
    for actor_id in actor_ids:
        direct = [r for r in rows if r[1] == actor_id]
        out[actor_id]["direct"] = [r[0] for r in direct]
        out[actor_id]["projects"] = {r[2] for r in direct}
        deps = [t for t in _walk(graph, [r[0] for r in direct]) if t not in out[actor_id]["direct"]]
        out[actor_id]["deps"] = deps
    dep_ids = {t for v in out.values() for t in v["deps"]}
    if dep_ids:
        projects = dict(session.execute(select(Task.id, Task.project_id).where(Task.id.in_(dep_ids))).all())
        for v in out.values():
            v["projects"] |= {projects[t] for t in v["deps"] if t in projects}
    return out


def task_hints(session: Session) -> dict[str, dict]:
    """Badges for task views while an agent is blocked: {task_nr: {agent, kind, label, since, via}}. `via` is the
    task number the task waits for through dependencies (None: the task is assigned to the blocked agent). Empty
    (one cheap query) while no agent is blocked."""
    agents = {a.id: a for a in blocked_actors(session)}
    if not agents:
        return {}
    rows = session.execute(select(Task.id, Task.task_nr, Task.assigned_to)
                           .where(Task.assigned_to.in_(list(agents)), Task.status.in_(DIRECT_STATUSES))).all()
    direct = {r[0]: (r[1], agents[r[2]]) for r in rows}
    out = {nr: _hint(agent, None) for nr, agent in direct.values()}
    if not direct:
        return out
    via = _walk(_graph(session), list(direct))
    numbers = session.execute(select(Task.id, Task.task_nr).where(Task.id.in_(list(via)))).all() if via else []
    for task_id, nr in numbers:
        root_nr, agent = direct[via[task_id]]
        out.setdefault(nr, _hint(agent, root_nr))
    return out


def _hint(agent: Actor, via: str | None) -> dict:
    return {"agent": agent.name, "kind": agent.blocked_kind, "label": kind_label(agent.blocked_kind),
            "since": agent.blocked_since, "via": via}


# --- Inbox ---------------------------------------------------------------------------------------------------------

def visible_for(session: Session, me: Actor | None) -> list[dict]:
    """Inbox section "Blocked agents": every blocked agent for admins; otherwise agents the human owns and agents
    with (directly or through dependencies) waiting tasks in projects the human owns or co-owns."""
    if me is None:
        return []
    agents = blocked_actors(session)
    if not agents:
        return []
    counts = waiting_counts(session, [a.id for a in agents])
    owned = set(session.scalars(select(Project.id).where(Project.owned_by(me.id))))
    out = []
    for a in agents:
        c = counts.get(a.id) or _empty()
        if me.role != "admin" and a.owner_id != me.id and not (c["projects"] & owned):
            continue
        out.append({"name": a.name, "agent_kind": a.agent_kind, "kind": a.blocked_kind,
                    "label": kind_label(a.blocked_kind), "reason": a.blocked_reason, "host": a.blocked_host,
                    "task_nr": a.blocked_task_nr, "since": a.blocked_since, "direct": len(c["direct"]),
                    "deps": len(c["deps"]), "waiting": len(c["direct"]) + len(c["deps"]),
                    "can_clear": me.role == "admin"})
    return out


# --- Notifications -------------------------------------------------------------------------------------------------

def reminder_hours(session: Session) -> int:
    try:
        return max(int(settings_store.get(session, REMINDER_SETTING) or 0), 0)
    except ValueError:
        return 4


def _recipients(session: Session, actor: Actor, projects: set[int]) -> dict[int, tuple[Actor, bool]]:
    """{human id: (human, restricted)}: the agent's owner, owners/co-owners of the affected projects and admins with
    "all projects". `restricted` (event and link only) when the human hears about it only through projects that are
    not `cloud_ok`."""
    out: dict[int, tuple[Actor, bool]] = {}
    owner = session.get(Actor, actor.owner_id) if actor.owner_id else None
    if owner is not None and owner.actor_type == "human" and owner.is_active:
        out[owner.id] = (owner, False)
    for project in session.scalars(select(Project).where(Project.id.in_(projects or {0})).order_by(Project.id)):
        restricted = project.privacy != "cloud_ok"
        for human in notifications.recipients(session, project):
            known = out.get(human.id)
            out[human.id] = (human, (known[1] if known else True) and restricted and human.role != "admin")
    if not out:  # Nobody connected to it (no owner, no open tasks): tell the admins who follow all projects.
        for human in session.scalars(select(Actor).join(NotificationPref, NotificationPref.actor_id == Actor.id)
                                     .where(NotificationPref.all_projects.is_(True), Actor.role == "admin",
                                            Actor.actor_type == "human", Actor.is_active.is_(True))):
            out[human.id] = (human, False)
    return out


def local_time(value: datetime | None) -> str:
    """Naive UTC → "dd.mm.yyyy hh:mm" in TZ_DISPLAY (format of the server language)."""
    if value is None:
        return "?"
    local = value.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(get_settings().tz_display))
    return local.strftime(date_format("datetime"))


def _text(session: Session, actor: Actor, info: dict, waiting: dict, event: str, restricted: bool) -> str:
    base = notifications._app_url(session)
    with use_language(default_language()):
        if event == READY_EVENT:
            head = _("%(agent)s · agent ready again", agent=actor.name)
        else:
            head = _("%(agent)s · still blocked: %(kind)s", agent=actor.name, kind=kind_label(info.get("kind")))
        lines = [head]
        if not restricted:
            since = local_time(info.get("since"))
            if event == READY_EVENT:
                lines.append(_("Was blocked since %(since)s: %(reason)s", since=since,
                               reason=notifications._short(info.get("reason"))))
            else:
                lines.append(notifications._short(info.get("reason")))
                lines.append(_("Blocked since %(since)s on %(host)s", since=since, host=info.get("host") or "?"))
            n = len(waiting["direct"]) + len(waiting["deps"])
            if n:
                lines.append(_("Waiting tasks: %(num)s (%(direct)s assigned, %(deps)s through dependencies)", num=n,
                               direct=len(waiting["direct"]), deps=len(waiting["deps"])))
    if base:
        lines.append(f"{base}/")
    return "\n".join(lines)


def _notify(session: Session, actor: Actor, info: dict | None, waiting: dict, event: str) -> int:
    """Queue `agent_blocked` (reminder) or `READY_EVENT` for everyone concerned; rows use the person's channels,
    mutes and quiet hours for `agent_blocked`. Returns the number of rows; never raises."""
    if info is None:
        return 0
    try:
        now, rows = utcnow(), []
        task_nr = info.get("task_nr") if len(info.get("task_nr") or "") <= 16 else None  # notifications.task_nr
        for human, restricted in _recipients(session, actor, waiting["projects"]).values():
            text = _text(session, actor, info, waiting, event, restricted)
            for row in notifications._actor_rows(session, human, "agent_blocked", task_nr, text, now):
                row.event = event
                rows.append(row)
        notifications._store(session, rows, False)
        return len(rows)
    except Exception:  # noqa: BLE001 – a notification problem must not break the clear/the job.
        notifications.log.exception("notify_failed", extra={"event": event, "agent": actor.name})
        return 0


def remind(session: Session, now: datetime | None = None) -> dict:
    """Scheduler job: repeat the blocked message every `agent_block_reminder_hours` (counted from the block or the
    last reminder; 0 = off)."""
    hours = reminder_hours(session)
    if hours <= 0:
        return {}
    now = now or utcnow()
    sent = 0
    for actor in blocked_actors(session):
        last = actor.blocked_reminded_at or actor.blocked_since
        if last is None or now - last < timedelta(hours=hours):
            continue
        actor.blocked_reminded_at = now
        waiting = waiting_counts(session, [actor.id]).get(actor.id) or _empty()
        _notify(session, actor, actor.blocked_info, waiting, "agent_blocked")
        sent += 1
    return {"reminded": sent} if sent else {}
