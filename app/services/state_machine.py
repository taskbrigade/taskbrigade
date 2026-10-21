"""Task state machine (spec section 6): the only place that changes `tasks.status`.

API and UI call `transition()` for domain actions or `transition_to()` for generic status
changes such as Kanban. Invalid transitions return 409 `invalid_transition`; missing permission returns 403.
"""
import json
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.i18n import _
from app.errors import AppError, Conflict, Forbidden, Unprocessable
from app.models import TASK_STATUSES, Actor, Project, Task, TaskComment, TaskDependency, TaskEvent
from app.services import brain_changes, changelog_fragments, notifications, settings_store
from app.services.gitea import GiteaError

END_STATUSES = frozenset({"done", "cancelled"})
# A dependency in one of these statuses no longer blocks the tasks that depend on it. A cancelled task can never
# become `done`, so counting it as open would block its dependents for good. The only place for this rule.
RESOLVED_DEPENDENCY_STATUSES = END_STATUSES
NON_END = frozenset(TASK_STATUSES) - END_STATUSES
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class InvalidTransition(Conflict):
    code = "invalid_transition"


@dataclass(frozen=True)
class Rule:
    action: str
    sources: frozenset[str]
    target: str
    who: str  # assignee | admin | reviewer (admin or the task's reviewer) | manager (admin/orchestrator) |
    #          manager_or_creator | human_admin | assignee_or_human_admin | system | gate (output gate service)


RULES: dict[str, Rule] = {r.action: r for r in [
    Rule("claim", frozenset({"defined", "changes_requested"}), "in_progress", "assignee"),
    Rule("ask", frozenset({"in_progress"}), "questions", "assignee"),
    Rule("answer", frozenset({"questions"}), "defined", "admin"),
    Rule("submit", frozenset({"in_progress"}), "for_review", "assignee"),
    # Review decisions: admins, the project owner, and the task's entered reviewer (`app/services/reviews.py`).
    Rule("approve", frozenset({"for_review"}), "done", "reviewer"),
    Rule("reject", frozenset({"for_review"}), "changes_requested", "reviewer"),
    # On merge conflict, return to the agent with instructions (`tbr prepare` merges the base).
    Rule("reject_conflict", frozenset({"for_review"}), "changes_requested", "reviewer"),
    Rule("release", frozenset({"in_progress"}), "defined", "assignee"),
    Rule("lease_expired", frozenset({"in_progress"}), "defined", "system"),
    Rule("cancel", NON_END, "cancelled", "manager"),
    # Agent cannot complete the task (e.g. limited model); wait for human reassignment.
    Rule("abort", frozenset({"in_progress", "questions", "changes_requested", "for_review"}), "aborted",
         "assignee_or_human_admin"),
    Rule("reopen", frozenset({"aborted"}), "defined", "human_admin"),
    # Drafts reach agents only after publication as 'defined'.
    Rule("publish", frozenset({"draft"}), "defined", "manager_or_creator"),
    Rule("to_draft", frozenset({"defined"}), "draft", "manager"),
    # Output gate (`app/services/output_gate.py`, research/konzept-datenzonen.md §6): only that service decides runner
    # results – it checks the rights and four-eyes rule and passes the result object, which no request can supply.
    Rule("gate_release", frozenset({"for_review"}), "done", "gate"),
    Rule("gate_reject", frozenset({"for_review"}), "changes_requested", "gate"),
    Rule("gate_keep", frozenset({"for_review", "changes_requested"}), "done", "gate"),
    Rule("gate_return", frozenset({"for_review"}), "changes_requested", "gate"),
]}

# Available through generic status changes (UI/Kanban); claim/submit need agent data.
GENERIC_ACTIONS = ("ask", "answer", "approve", "reject", "release", "cancel", "abort", "reopen", "publish",
                   "to_draft")


def is_human_admin(actor: Actor | None) -> bool:
    return actor is not None and actor.role == "admin" and actor.actor_type == "human"


def can_override(session: Session, task: Task, actor: Actor | None) -> bool:
    """Human admin or project owner (override and reassignment of active tasks)."""
    return is_human_admin(actor) or (actor is not None and actor.actor_type == "human"
                                     and _project(session, task).owner_id == actor.id)


def utcnow():
    from app.services.actors import utcnow as _now

    return _now()


def find_generic_rule(from_status: str, to_status: str) -> Rule | None:
    for name in GENERIC_ACTIONS:
        rule = RULES[name]
        if from_status in rule.sources and rule.target == to_status:
            return rule
    return None


def check_allowed(rule: Rule, task: Task, actor: Actor | None, owner_id: int | None = None,
                  is_reviewer: bool = False) -> None:
    """Check status and role without side effects (also for unit tests).
    `owner_id` identifies the project owner, who has human admin rights in that project; `is_reviewer`: the actor
    is the task's entered reviewer and may review it (`reviews.may_review`)."""
    actor_is_owner = (actor is not None and owner_id is not None and actor.actor_type == "human"
                      and actor.id == owner_id)
    if task.status not in rule.sources:
        raise InvalidTransition(_("%(action)s: not possible from status '%(status)s' (allowed: %(sources)s)", action=rule.action, status=task.status, sources=", ".join(sorted(rule.sources))))
    if rule.who == "gate":
        return  # `output_gate` checked the decider; the action needs the internal result object (`_gate_result`).
    if rule.who == "system":
        if actor is not None:
            raise Forbidden(_("Only the system may perform %(action)s", action=rule.action))
    elif actor is None:
        raise Forbidden(_("%(action)s requires an actor", action=rule.action))
    elif rule.who == "assignee" and task.assigned_to != actor.id:
        raise Forbidden(_("%(action)s: assigned actor only", action=rule.action))
    elif actor_is_owner:
        pass  # Owner has human admin rights in this project.
    elif rule.who == "admin" and actor.role != "admin":
        raise Forbidden(_("%(action)s: admins only", action=rule.action))
    elif rule.who == "reviewer" and actor.role != "admin" and not is_reviewer:
        raise Forbidden(_("%(action)s: admins, the project owner and the task's reviewer only", action=rule.action))
    elif rule.who == "manager" and actor.role not in ("admin", "orchestrator"):
        raise Forbidden(_("%(action)s: admins and orchestrators only", action=rule.action))
    elif rule.who == "manager_or_creator" and actor.role not in ("admin", "orchestrator") \
            and task.created_by != actor.id:
        raise Forbidden(_("%(action)s: admins, orchestrators, or the task creator only", action=rule.action))
    elif rule.who == "human_admin" and not is_human_admin(actor):
        raise Forbidden(_("%(action)s: human admins only", action=rule.action))
    elif rule.who == "assignee_or_human_admin" and task.assigned_to != actor.id and not is_human_admin(actor):
        raise Forbidden(_("%(action)s: assigned agent or human admin only", action=rule.action))
    elif rule.who == "assignee_or_human_admin" and task.assigned_to == actor.id and not is_human_admin(actor) \
            and task.status != "in_progress":
        raise InvalidTransition(_("%(action)s: agents can only abort tasks in progress", action=rule.action))


# --- Helpers ---------------------------------------------------------------------------------

def add_event(session: Session, task: Task, actor: Actor | None, event_type: str, from_value=None, to_value=None,
              note: str | None = None) -> None:
    session.add(TaskEvent(task_id=task.id, actor_id=actor.id if actor else None, event_type=event_type,
                          from_value=None if from_value is None else str(from_value)[:100],
                          to_value=None if to_value is None else str(to_value)[:100], note=note))


def add_comment(session: Session, task: Task, actor: Actor, comment_type: str, body: str) -> TaskComment:
    c = TaskComment(task_id=task.id, author_id=actor.id, comment_type=comment_type, body=body)
    session.add(c)
    return c


def notify_dependents_cancelled(session: Session, task: Task, actor: Actor | None) -> None:
    """A cancelled dependency counts as resolved, so its open dependents become claimable: tell them in the thread."""
    dependents = session.scalars(
        select(Task).join(TaskDependency, TaskDependency.task_id == Task.id)
        .where(TaskDependency.depends_on_task_id == task.id, Task.status.not_in(tuple(END_STATUSES)))).all()
    for dependent in dependents:
        add_comment(session, dependent, actor or session.get(Actor, dependent.created_by), "system",
                    _("Dependency %(task_nr)s was cancelled – please check whether this task still applies.",
                      task_nr=task.task_nr))


def slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue",
                                                                            "ß": "ss"}))).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def branch_name_for(task: Task) -> str:
    return f"task/{task.task_nr}-{slugify(task.title)}"


def task_link(session: Session, task: Task) -> str | None:
    base = (settings_store.get(session, "app_url") or "").rstrip("/")
    return f"{base}/tasks/{task.task_nr}" if base else None


def _notify(session: Session, task: Task, event: str, content: str | None = None, detached: bool = False) -> None:
    """Telegram notification to the project's recipients (`app/services/notifications.py`); never raises.
    `detached`: the caller raises afterwards (rollback), so the notification is stored in its own transaction."""
    notifications.task_event(session, task, event, content, detached)


def _require_text(payload: dict, key: str, label: str) -> str:
    value = (payload.get(key) or "").strip()
    if not value:
        raise Unprocessable(_("%(label)s is required", label=label), code="missing_field")
    return value


def _project(session: Session, task: Task) -> Project:
    return session.get(Project, task.project_id)


# --- Actions --------------------------------------------------------------------------------

def _claim(session, task, actor, payload):
    now = utcnow()
    task.claimed_at = now
    task.lease_expires_at = now + timedelta(minutes=actor.lease_minutes)
    if not task.branch_name:
        task.branch_name = branch_name_for(task)


def _ask(session, task, actor, payload):
    question = _require_text(payload, "body", _( 'Question' ))
    add_comment(session, task, actor, "question", question)
    task.lease_expires_at = None
    return lambda: _notify(session, task, "question", question)


def _answer(session, task, actor, payload):
    add_comment(session, task, actor, "answer", _require_text(payload, "body", _( 'Answer' )))


def _release(session, task, actor, payload):
    reason = _require_text(payload, "body", _( 'Reason' ))
    add_comment(session, task, actor, "system", _("Task released: %(reason)s", reason=reason))
    task.lease_expires_at = None
    if payload.get("signal") == "blocked":  # The runner gave the task back because the agent is blocked.
        return lambda: _notify(session, task, "agent_blocked", reason)


def _lease_expired(session, task, actor, payload):
    task.lease_expires_at = None
    return lambda: _notify(session, task, "lease_expired")


def _cancel(session, task, actor, payload):
    if (payload.get("body") or "").strip():
        add_comment(session, task, actor, "comment", payload["body"].strip())
    task.lease_expires_at = None


def _runner_submit(session, task, actor, payload):
    """Runner jobs end without branch or PR: the result waits in quarantine (`runner_jobs.record_result`)."""
    from app.models import RunResult

    result = payload.get("_run_result")
    if not isinstance(result, RunResult):  # Set internally only – never from an API body.
        raise Unprocessable(_("Runner tasks are submitted by the runner with a job result"), code="runner_submit")
    task.result_summary = (payload.get("summary") or "").strip() or None
    task.submitted_at, task.lease_expires_at = utcnow(), None
    add_comment(session, task, actor, "system", task.result_summary or _("Runner job finished."))
    if result.status == "pending_approval":  # Event without contents and without review buttons (zone R)
        return lambda: _notify(session, task, "data_release")
    return None


def _gate_result(task, payload):
    from app.models import RunResult

    result = payload.get("_gate")
    if not isinstance(result, RunResult) or result.task_id != task.id:  # Set internally only – never from a body.
        raise Conflict(_("Runner results are released in the output gate, not by approving the task"),
                       code="output_gate_required")
    return result


def _gate_done(session, task, actor, payload):
    _gate_result(task, payload)
    task.done_at, task.lease_expires_at = utcnow(), None
    add_comment(session, task, payload["_author"], "system", payload["body"])


def _gate_back(session, task, actor, payload):
    """Rejected or failed: the run task waits for a repaired script. The commit is cleared, so the runner does not
    run the same script again; setting new run fields (`tbr update`/API `run`) makes it claimable."""
    _gate_result(task, payload)
    task.run_commit_sha, task.lease_expires_at = None, None
    add_comment(session, task, payload["_author"], "system", payload["body"])
    event = payload.get("_event")  # `gate_failed` when the checks sent it back; a human decision tells nobody
    return (lambda: _notify(session, task, event)) if event else None


def _submit(session, task, actor, payload):
    if task.task_kind in ("run", "profile"):
        return _runner_submit(session, task, actor, payload)
    branch = _require_text(payload, "branch_name", "branch_name")
    sha = _require_text(payload, "commit_sha", "commit_sha").lower()
    summary = _require_text(payload, "summary", "summary")
    points = clean_open_points(payload.get("open_points"))
    if not SHA_RE.match(sha):
        raise Unprocessable(_( 'commit_sha must be a 40-character hexadecimal SHA' ), code="invalid_sha")
    project = _project(session, task)
    if not project.gitea_repo:
        raise Unprocessable(_( 'Project has no Gitea repository' ), code="no_repo")
    if branch == project.default_branch:
        raise Unprocessable(_("Submitting directly to '%(branch)s' is not allowed", branch=branch), code="invalid_branch")
    from app.services.workflows.contracts import prepare_submit_outputs, store_submit_outputs
    declared_outputs = prepare_submit_outputs(session, task, payload.get("outputs"), commit=sha)
    client = settings_store.gitea_client(session)
    o, r = project.gitea_owner, project.gitea_repo
    link = task_link(session, task)
    try:
        if not client.branch_exists(o, r, branch):
            raise Unprocessable(_("Branch '%(branch)s' does not exist in Gitea – push it first", branch=branch),
                                code="branch_not_found")
        if task.pr_number and task.branch_name == branch:
            client.comment_pull(o, r, task.pr_number, _("Resubmitted (%(sha)s):\n\n%(summary)s", sha=sha[:10],
                                                        summary=summary + _points_md(points)))
        else:
            body = summary + _points_md(points) + (f"\n\n---\nTask: {link}" if link else f"\n\n---\nTask: {task.task_nr}")
            pr = client.create_pull(o, r, branch, project.default_branch, f"[{task.task_nr}] {task.title}", body)
            task.pr_number, task.pr_url = pr.number, settings_store.public_gitea_link(session, pr.html_url)
            add_event(session, task, actor, "pr_created", None, pr.number, task.pr_url)
    except GiteaError as e:
        raise Unprocessable(f"Gitea: {e}", code="gitea_error") from e
    _brain_submit_notes(session, task, actor, project, branch)
    task.branch_name, task.result_commit_sha, task.result_summary = branch, sha, summary
    task.open_points = json.dumps(points, ensure_ascii=False) if points else None
    task.submitted_at, task.lease_expires_at = utcnow(), None
    store_submit_outputs(session, task, declared_outputs, open_points=points, status="for_review")
    from app.services import reviews

    reviews.on_submit(session, task, actor)
    reviewer = reviews.agent_reviewer(session, task)
    if reviewer is not None:  # A reviewer agent takes it as a review job; humans are not asked to review.
        add_comment(session, task, actor, "system", _("Review by the reviewer agent %(name)s – the project owner and "
                                                      "admins can still decide.", name=reviewer.name))
        return None
    return lambda: _notify(session, task, "review", summary)


MAX_OPEN_POINTS, MAX_OPEN_POINT_CHARS = 50, 1000


def clean_open_points(value) -> list[str]:
    """Open points from submit: a list of strings; blank entries are dropped, leading list markers removed."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise Unprocessable(_("open_points must be a list of strings"), code="invalid_open_points")
    points = [re.sub(r"^\s*(?:[-*]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)", "", p).strip() for p in value]
    points = [p for p in points if p]
    if len(points) > MAX_OPEN_POINTS or any(len(p) > MAX_OPEN_POINT_CHARS for p in points):
        raise Unprocessable(_("At most %(num)s open points with up to %(chars)s characters each",
                              num=MAX_OPEN_POINTS, chars=MAX_OPEN_POINT_CHARS), code="invalid_open_points")
    return points


def _points_md(points: list[str]) -> str:
    return ("\n\n" + _("Open points:") + "\n" + "\n".join(f"- {p}" for p in points)) if points else ""


def _approve(session, task, actor, payload):
    if task.task_kind == "profile" and _profile_decision(session, task, actor, payload, "released"):
        task.done_at = utcnow()
        return
    if task.task_kind in ("run", "profile"):
        # Approving a runner result releases data from zone R – only the output gate may do that (DZ 8).
        raise Conflict(_("Runner results are released in the output gate, not by approving the task"),
                       code="output_gate_required")
    if task.task_kind == "approval":
        if (payload.get("body") or "").strip():
            add_comment(session, task, actor, "review", payload["body"].strip())
        task.done_at = utcnow()
        return
    project = _project(session, task)
    if not (task.pr_number and project.gitea_repo):
        raise Conflict(_( 'No pull request available to merge' ), code="no_pull_request")
    style = settings_store.get(session, "merge_style") or "squash"
    try:
        brain_ops, brain_issues = _brain_ops(session, project, task)
        changelog_entry, changelog_issue = _changelog_fragment(session, project, task)
    except GiteaError as e:
        raise Conflict(f"Gitea: {e}", code="gitea_error") from e
    if changelog_issue:
        _notify(session, task, "conflict", changelog_issue, detached=True)
        who = session.get(Actor, task.assigned_to).name if task.assigned_to else _( 'the agent' )
        raise Conflict(_("%(issue)s – no merge performed; task remains in review. Use ‘Return conflict’ to send "
                         "instructions to %(assignee)s.", issue=changelog_issue, assignee=who), code="changelog_invalid")
    if brain_issues:
        _notify(session, task, "conflict", _("brain-changes no longer apply: %(issues)s",
                                             issues="; ".join(brain_issues)), detached=True)
        who = session.get(Actor, task.assigned_to).name if task.assigned_to else _( 'the agent' )
        raise Conflict(_("brain-changes for %(task_nr)s no longer apply to %(path)s on %(branch)s: %(issues)s – no merge performed; task remains in review. Use ‘Return conflict’ to send instructions to %(assignee)s.", task_nr=task.task_nr, path=project.brain_path, branch=project.default_branch, issues="; ".join(brain_issues), assignee=who), code="brain_conflict")
    try:
        task.merge_commit_sha = settings_store.gitea_client(session).merge_pull(
            project.gitea_owner, project.gitea_repo, task.pr_number, style,
            title=f"[{task.task_nr}] {task.title} (#{task.pr_number})")
        from app.services.workflows.contracts import finalize_approved_outputs
        finalize_approved_outputs(session, task)
    except GiteaError as e:
        files = merge_conflicts(session, task)
        _notify(session, task, "conflict", _("Merge conflict with %(branch)s", branch=project.default_branch)
                if files is not None else _("Merge failed"), detached=True)
        if files is not None:
            who = session.get(Actor, task.assigned_to).name if task.assigned_to else _( 'the agent' )
            raise Conflict(_("Merge conflict with %(branch)s%(files)s – task remains in review. Use ‘Return conflict’ to send instructions to %(assignee)s; the next claim will merge %(branch)s into the task branch automatically.", branch=project.default_branch, files=_(" in: %(files)s", files=", ".join(files)) if files else "", assignee=who), code="merge_conflict") from e
        raise Conflict(_("Merge failed – task remains in for_review. Consider returning it with instructions to rebase onto %(branch)s. (%(error)s)", branch=project.default_branch, error=e), code="merge_failed") from e
    add_event(session, task, actor, "merged", None, task.pr_number, style)
    if brain_ops or changelog_entry:
        _apply_repo_changes(session, task, actor, project, brain_ops or [], changelog_entry)
    if (payload.get("body") or "").strip():
        add_comment(session, task, actor, "review", payload["body"].strip())
    task.done_at = utcnow()


def _reviewed(session, task, action, actor, after):
    """A reviewer agent decided: say so in the thread and tell the project's humans (event `agent_review`)."""
    if actor.actor_type != "agent" or task.reviewer_id != actor.id:
        return after
    from app.services import reviews

    note = reviews.decision_note(action, actor)
    add_comment(session, task, actor, "system", note)
    body = task_comment_body(session, task)

    def done():
        if callable(after):
            after()
        _notify(session, task, "agent_review", f"{note}\n{body}" if body else note)
    return done


def task_comment_body(session: Session, task: Task) -> str | None:
    """Body of the latest review comment written in this transaction (for the notification text)."""
    for obj in reversed(list(session.new)):
        if isinstance(obj, TaskComment) and obj.task_id == task.id and obj.comment_type == "review":
            return obj.body
    return None


def _reject(session, task, actor, payload):
    if task.task_kind == "run" or (
            task.task_kind == "profile" and not _profile_decision(session, task, actor, payload, "rejected")):
        raise Conflict(_("Runner results are rejected in the output gate, not in the task review"),
                       code="output_gate_required")
    add_comment(session, task, actor, "review", _require_text(payload, "body", _( 'Review comment' )))


def merge_conflicts(session: Session, task: Task) -> list[str] | None:
    """Return conflicting PR files (empty = conflict with unknown files); None = no detected conflict."""
    project = _project(session, task)
    if not (task.pr_number and project.gitea_repo):
        return None
    try:
        return settings_store.gitea_client(session).pull_conflicts(
            project.gitea_owner, project.gitea_repo, task.pr_number, project.default_branch)
    except GiteaError:
        return None


def conflict_instructions(base: str, files: list[str]) -> str:
    listed = ", ".join(f"`{f}`" for f in files) if files else _("(files could not be determined)")
    return _(
        "**Merge conflict with `%(base)s`** in %(files)s: `%(base)s` changed since you started "
        "(for example, another task was merged), so your pull request cannot be merged.\n\n"
        "When you claim the task again, `tbr prepare` automatically merges the current `%(base)s` into your "
        "task branch. If conflicts occur, the affected files contain conflict markers "
        "(`<<<<<<<`, `=======`, `>>>>>>>`); TASK.md lists the files. Follow these steps:\n"
        "1. Resolve the conflict markers and preserve **both** changes (in `brain.md`, keep both entries).\n"
        "2. Leave everything else as it is; the result itself was not rejected.\n"
        "3. Finish as usual (commit, push, and submit or use the completion file).",
        base=base, files=listed,
    )


# --- brain-changes (maintain project knowledge without conflicts) -------------------------------------------

def _brain_ops(session: Session, project: Project, task: Task) -> tuple[list | None, list[str]]:
    """Read the task branch's change file and check against brain.md on the base branch.
    Return (changes or None, problems); raise GiteaError on failure."""
    if not (project.gitea_repo and task.branch_name):
        return None, []
    client = settings_store.gitea_client(session)
    o, r = project.gitea_owner, project.gitea_repo
    text = client.raw_file(o, r, brain_changes.path_for(task.task_nr), task.branch_name)
    if text is None:
        return None, []
    try:
        ops = brain_changes.parse(text)
    except brain_changes.BrainChangeError as e:
        return None, [_("Invalid format in %(path)s: %(error)s", path=brain_changes.path_for(task.task_nr), error=e)]
    brain = client.raw_file(o, r, project.brain_path, project.default_branch) or ""
    return ops, brain_changes.check(brain, ops)


def _changelog_fragment(session: Session, project: Project, task: Task) -> tuple[str | None, str | None]:
    """changelog.d fragment of the task branch: (cleaned entry or None, problem or None). Raises GiteaError."""
    if not (project.gitea_repo and task.branch_name):
        return None, None
    path = changelog_fragments.path_for(task.task_nr)
    text = settings_store.gitea_client(session).raw_file(project.gitea_owner, project.gitea_repo, path,
                                                         task.branch_name)
    if text is None:
        return None, None
    try:
        return changelog_fragments.clean(text), None
    except ValueError as e:
        return None, _("Invalid changelog fragment %(path)s: %(error)s", path=path, error=e)


def _apply_repo_changes(session, task, actor, project, ops, changelog_entry: str | None) -> None:
    """After merging, apply brain-changes to brain.md and the changelog fragment to CHANGELOG.md and delete both
    files – one separate commit. On failure (e.g. Gitea unavailable) the merge stands and the thread gives manual
    steps."""
    client = settings_store.gitea_client(session)
    o, r, base = project.gitea_owner, project.gitea_repo, project.default_branch
    updates, deletes, done = {}, [], []
    try:
        if ops:
            updates[project.brain_path] = brain_changes.apply(client.raw_file(o, r, project.brain_path, base) or "",
                                                              ops)
            deletes.append(brain_changes.path_for(task.task_nr))
        if changelog_entry:
            current = client.raw_file(o, r, changelog_fragments.CHANGELOG, base) or ""
            updates[changelog_fragments.CHANGELOG] = changelog_fragments.insert(current, changelog_entry)
            deletes.append(changelog_fragments.path_for(task.task_nr))
        client.change_files(o, r, updates, deletes,
                            _("[%(task_nr)s] %(paths)s updated", task_nr=task.task_nr, paths=", ".join(updates)),
                            base)
    except (GiteaError, brain_changes.BrainConflict) as e:
        add_comment(session, task, actor, "system", _(
            "Change files **not** applied (%(error)s). %(paths)s are on `%(base)s`; apply them manually and "
            "delete them.", error=e, paths=", ".join(f"`{p}`" for p in deletes), base=base))
        return
    if ops:
        counts = {k: sum(1 for op in ops if op.kind == k) for k in brain_changes.OPS}
        done.append(_("`%(path)s` updated: %(added)d added, %(changed)d changed, %(removed)d removed.",
                      path=project.brain_path, added=counts["add"], changed=counts["change"],
                      removed=counts["remove"]))
    if changelog_entry:
        done.append(_("`%(path)s`: entry added under „%(section)s“.", path=changelog_fragments.CHANGELOG,
                      section=changelog_fragments.SECTION))
    add_comment(session, task, actor, "system", " ".join(done))


def _brain_submit_notes(session, task, actor, project, branch: str) -> None:
    """On submission, note direct brain.md edits and invalid change files in the thread.
    This does not block submission; the reviewer decides."""
    client = settings_store.gitea_client(session)
    try:
        changed = client.pull_files(project.gitea_owner, project.gitea_repo, task.pr_number) if task.pr_number else []
        text = client.raw_file(project.gitea_owner, project.gitea_repo, brain_changes.path_for(task.task_nr), branch)
        fragment = client.raw_file(project.gitea_owner, project.gitea_repo,
                                   changelog_fragments.path_for(task.task_nr), branch)
    except GiteaError:
        return
    notes = []
    if changelog_fragments.CHANGELOG in changed:
        notes.append(_(
            "`%(changelog)s` was edited directly. Parallel tasks may cause merge conflicts; put the entry into "
            "`%(fragment)s` instead.", changelog=changelog_fragments.CHANGELOG,
            fragment=changelog_fragments.path_for(task.task_nr)))
    if project.brain_path in changed:
        notes.append(_(
            "`%(brain_path)s` was edited directly. Parallel tasks may cause merge conflicts; record new findings "
            "in `%(change_path)s` (add/change/remove).",
            brain_path=project.brain_path, change_path=brain_changes.path_for(task.task_nr)))
    if text is not None:
        try:
            brain_changes.parse(text)
        except brain_changes.BrainChangeError as e:
            notes.append(_("`%(path)s` has an invalid format: %(error)s. The task cannot be approved until it is fixed.",
                           path=brain_changes.path_for(task.task_nr), error=e))
    if fragment is not None:
        try:
            changelog_fragments.clean(fragment)
        except ValueError as e:
            notes.append(_("Invalid changelog fragment %(path)s: %(error)s",
                           path=changelog_fragments.path_for(task.task_nr), error=e))
    if notes:
        add_comment(session, task, actor, "system", _("Note on change files: %(notes)s", notes=" ".join(notes)))


def brain_conflict_instructions(task_nr: str, brain_path: str, base: str, issues: list[str]) -> str:
    path = brain_changes.path_for(task_nr)
    return _(
        "**`%(path)s` no longer matches `%(brain_path)s` on `%(base)s`:** %(issues)s.\n\n"
        "`%(brain_path)s` changed since you started (for example, another task was approved). "
        "When you claim the task again, `tbr prepare` automatically merges the current state. Follow these steps:\n"
        "1. Read `%(brain_path)s` again.\n"
        "2. In `%(path)s`, update each `change`/`remove` to match the current text (the text under `### old` "
        "or `remove` must match brain.md exactly), or delete an operation that is no longer needed.\n"
        "3. Leave everything else as it is and finish as usual.",
        path=path, brain_path=brain_path, base=base, issues="; ".join(issues))


def _reject_conflict(session, task, actor, payload):
    if task.task_kind in ("run", "profile"):
        raise Conflict(_("Runner results are rejected in the output gate, not in the task review"),
                       code="output_gate_required")
    project = _project(session, task)
    base = project.default_branch
    files = merge_conflicts(session, task)
    try:
        brain_issues = _brain_ops(session, project, task)[1]
        changelog_issue = _changelog_fragment(session, project, task)[1]
    except GiteaError:
        brain_issues, changelog_issue = [], None
    parts = []
    if changelog_issue:
        parts.append(_("**%(issue)s.** Write plain Markdown bullet points (no `#`/`##` headings) into the fragment; "
                       "TaskBrigade adds them under „Unreleased“ in CHANGELOG.md on approval.", issue=changelog_issue))
    if files is not None or not (brain_issues or changelog_issue):
        parts.append(conflict_instructions(base, files or []))
    if brain_issues:
        parts.append(brain_conflict_instructions(task.task_nr, project.brain_path, base, brain_issues))
    body = "\n\n".join(parts)
    if (payload.get("body") or "").strip():
        body += _("\n\n**Reviewer note:** %(note)s", note=payload["body"].strip())
    add_comment(session, task, actor, "review", body)


def _abort(session, task, actor, payload):
    reason = _require_text(payload, "body", _( 'Reason' ))
    add_comment(session, task, actor, "system", _("Aborted – reassignment required: %(reason)s", reason=reason))
    task.lease_expires_at = None
    return lambda: _notify(session, task, "aborted", reason)


def _reopen(session, task, actor, payload):
    if (payload.get("body") or "").strip():
        add_comment(session, task, actor, "comment", payload["body"].strip())


DEFINITION_FIELDS = ("description", "acceptance_criteria")


def missing_definition(task: Task) -> list[str]:
    """Fields (`DEFINITION_FIELDS`) a task still lacks for publication as 'defined'."""
    return [f for f in DEFINITION_FIELDS if not (getattr(task, f) or "").strip()]


def require_definition(task: Task) -> None:
    """Publish as 'defined' only with a description and acceptance criteria so agents know the goal."""
    labels = {"description": _( 'Description' ), "acceptance_criteria": _( 'Acceptance criteria' )}
    missing = [labels[f] for f in missing_definition(task)]
    from app.services import runner_jobs

    if runner_jobs.missing_run_fields(task):
        raise Unprocessable(_("%(task_nr)s: a run task needs the script commit, the script path and the input dataset "
                              "before it is set to ‘Defined’.", task_nr=task.task_nr or "Task"),
                            code="run_fields_incomplete")
    if missing:
        raise Unprocessable(_("%(task_nr)s: %(fields)s missing – add them before setting the task to ‘Defined’.",
                              task_nr=task.task_nr or "Task", fields=_(" and ").join(missing)),
                            code="definition_incomplete")


def _publish(session, task, actor, payload):
    require_definition(task)
    if (payload.get("body") or "").strip():
        add_comment(session, task, actor, "comment", payload["body"].strip())


def _to_draft(session, task, actor, payload):
    if (payload.get("body") or "").strip():
        add_comment(session, task, actor, "comment", payload["body"].strip())


EFFECTS = {"claim": _claim, "ask": _ask, "answer": _answer, "release": _release, "lease_expired": _lease_expired,
           "cancel": _cancel, "submit": _submit, "approve": _approve, "reject": _reject,
           "reject_conflict": _reject_conflict, "abort": _abort,
           "reopen": _reopen, "publish": _publish, "to_draft": _to_draft,
           "gate_release": _gate_done, "gate_keep": _gate_done, "gate_reject": _gate_back, "gate_return": _gate_back}


def _profile_decision(session, task, actor, payload, status):
    from app.models import Dataset, DatasetProfile
    from app.services import datasets
    row = (payload or {}).get("_dataset_profile")
    if not isinstance(row, DatasetProfile) or row.task_id != task.id or row.status != status:
        return False
    ds = session.get(Dataset, row.dataset_id)
    return datasets.can_upload(session, _project(session, task), actor) and ds.project_id == task.project_id


def transition(session: Session, task: Task, action: str, actor: Actor | None, payload: dict | None = None) -> Task:
    """Run a domain action: validate, set fields, write comments/events, and update Gitea."""
    rule = RULES.get(action)
    if rule is None:
        raise AppError(_("Unknown action '%(action)s'", action=action))
    from app.services import reviews

    check_allowed(rule, task, actor, _project(session, task).owner_id,
                  rule.who == "reviewer" and (reviews.may_review(session, task, actor) or (
                      task.task_kind == "profile" and action in ("approve", "reject") and _profile_decision(
                          session, task, actor, payload, "released" if action == "approve" else "rejected"))))
    from app.services.workflows import engine as workflows

    workflows.lock_for_task(session, task)
    if action == "submit" and (payload or {}).get("usage"):
        from app.services import usage

        run, before = usage.record(session, actor, task, payload["usage"])
        usage.enforce(session, run, before)
    if action == "claim":
        workflows.require_claimable(session, task)
    if action == "approve":
        workflows.check_decision(session, task, actor)
    from_status = task.status
    after = EFFECTS[action](session, task, actor, payload or {})
    if rule.who == "reviewer":
        after = _reviewed(session, task, action, actor, after)
    if from_status == "for_review" and rule.target != "for_review":
        reviews.clear_claim(task)
    task.status = rule.target
    task.clear_waiting()
    task.row_version += 1
    add_event(session, task, actor, "lease_expired" if action == "lease_expired" else "status_change",
              from_status, rule.target, None if action in ("claim", "lease_expired") else action)
    session.flush()
    workflows.on_task_transition(session, task, action, actor, payload)
    from app.services import data_refs

    data_refs.on_transition(session, task, action, actor, payload)  # Follow-up drafts of a chain across zones
    if task.status == "cancelled":
        notify_dependents_cancelled(session, task, actor)
    if action == "approve" or task.status == "cancelled":
        from app.services import result_mail

        result_mail.on_task_status(session, task, actor, action)  # `on_approve` mails; cancel rejects pending ones
    if callable(after) and not (action == "submit" and task.status == "done"):
        after()
    if rule.target in END_STATUSES:
        notifications.sprint_done_check(session, task)
    return task


def transition_to(session: Session, task: Task, to_status: str, actor: Actor, comment: str | None = None,
                  override: bool = False) -> Task:
    """Generic status change (UI/Kanban/`POST /transition`). Without a matching rule, require
    admin `override` and record an event with note 'manual override'."""
    if to_status not in TASK_STATUSES:
        raise Unprocessable(_("Unknown status '%(status)s'", status=to_status))
    if to_status == task.status:
        raise InvalidTransition(_("Task already has status '%(status)s'", status=to_status))
    rule = find_generic_rule(task.status, to_status)
    if rule is not None and not override:
        return transition(session, task, rule.action, actor, {"body": comment})
    if not override:
        raise InvalidTransition(_("Transition %(from_status)s → %(to_status)s is not allowed", from_status=task.status, to_status=to_status))
    if not can_override(session, task, actor):
        raise Forbidden(_( 'Only human admins and the project owner can override status' ))
    if task.status == "draft" and to_status == "defined":
        require_definition(task)  # Also applies to overrides.
    from_status = task.status
    from app.services.workflows import engine as workflows

    workflows.lock_for_task(session, task)
    if to_status == "done":
        workflows.check_decision(session, task, actor)
    if comment and comment.strip():
        add_comment(session, task, actor, "comment", comment.strip())
    if from_status == "for_review":
        task.review_claimed_at = task.review_lease_expires_at = None
    task.status = to_status
    task.clear_waiting()
    if to_status != "in_progress":
        task.lease_expires_at = None
    elif task.assigned_to:
        _claim(session, task, session.get(Actor, task.assigned_to), {})
    if to_status == "done" and task.done_at is None:
        task.done_at = utcnow()
    task.row_version += 1
    add_event(session, task, actor, "status_change", from_status, to_status, "manual override")
    session.flush()
    workflows.on_task_transition(session, task, "override", actor, {"body": comment})
    if to_status == "cancelled":
        notify_dependents_cancelled(session, task, actor)
        from app.services import result_mail

        result_mail.on_task_status(session, task, actor, "override")
    if to_status in END_STATUSES:
        notifications.sprint_done_check(session, task)
    return task


def workflow_status(session: Session, task: Task, status: str, note: str, actor: Actor | None = None) -> None:
    """Internal workflow transition, under the run lock; never exposed as a public action.

    Runs may reopen a completed agent task for rework, and cancel their own tasks regardless of the human's
    task-manager role. Keep these exceptions here so leases, comments and status history stay consistent.
    """
    local_profile_done = status == "done" and task.task_kind == "profile" and task.status == "for_review"
    if not task.workflow_run_step_id or (status not in ("changes_requested", "cancelled", "defined")
                                         and not local_profile_done):
        raise InvalidTransition(_("Invalid internal workflow transition"))
    previous = task.status
    if local_profile_done:
        task.done_at = utcnow()
    if status == "changes_requested" and previous == "done":
        # Rework after a workflow approval is new work after a merged PR. Keeping pr_number would make
        # _submit comment on the already merged PR, and approve could recover that old merge SHA.
        task.pr_number = task.pr_url = None
        task.result_commit_sha = task.merge_commit_sha = task.result_summary = task.open_points = None
        task.submitted_at = task.done_at = None
    task.status, task.lease_expires_at = status, None
    task.review_claimed_at = task.review_lease_expires_at = None
    task.clear_waiting()
    task.row_version += 1
    add_event(session, task, actor, "status_change", previous, status, "workflow")
    add_comment(session, task, actor or session.get(Actor, task.created_by), "system", note)
    if status == "cancelled":
        notify_dependents_cancelled(session, task, actor)
    session.flush()


REASSIGN_RESETS = frozenset({"in_progress", "questions", "aborted", "for_review", "changes_requested"})


def reassign_resets_status(task: Task) -> bool:
    return task.status in REASSIGN_RESETS


def reset_for_reassignment(session: Session, task: Task, actor: Actor, new_assignee: str | None) -> None:
    """Reassign an active/aborted task through a human admin: reset to 'defined' and end the old
    agent's lease; preserve the branch, PR, and thread for the new agent."""
    from app.services.workflows import engine as workflows

    workflows.lock_for_task(session, task)
    from_status = task.status
    task.status = "defined"
    task.lease_expires_at = None
    task.clear_waiting()
    task.review_claimed_at = task.review_lease_expires_at = None
    add_event(session, task, actor, "status_change", from_status, "defined",
              _("reassigned to %(assignee)s", assignee=new_assignee or _("nobody")))
    workflows.on_task_transition(session, task, "reassign", actor)
