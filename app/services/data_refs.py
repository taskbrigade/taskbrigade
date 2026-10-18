"""Task chain across zones (research/konzept-datenzonen.md §1 "Task-Kette über Zonen", §8, DZ 9; guide
docs/data-store.md → Task chain across zones).

A task refers to data artifacts instead of carrying their content (`task_data_refs`): a `dataset` as `input` (only
`sealed` tasks), a `profile` as `schema`, a runner `result` as `output`. The order of the chain is the existing
`follow_up_of_id`; this module adds

- the references and rule 3 of §1 (a task with an input dataset is `sealed`; profile and result references in an
  LLM zone only while the artifact is `released`),
- the automatic follow-up drafts at each zone change (profile released → script task, script merged → run task,
  result released → interpretation task) – drafts, so a human confirms every crossing,
- the read rule for agents: an agent sees an artifact's content only through a task of its own that references it,
  and only released artifacts (`agent_may_read`, `artifacts` for TASK.md),
- the chain view on the task page (`chain`, rendered by the shared component `_workflow_chain.html`).

Workflow tasks are skipped: a workflow run models its chain itself (research/konzept-workflows.md §4). Callers own the
transaction.
"""
import json
import logging
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, Forbidden, NotFound, Unprocessable
from app.i18n import N_, _
from app.models import Actor, Dataset, DatasetProfile, Project, RunResult, Task, TaskDataRef
from app.services import data_audit, datasets, zones

log = logging.getLogger("taskbrigade.data_refs")

# One purpose per artifact type (concept §1: input = raw data, sealed only).
PURPOSES = {"dataset": "input", "profile": "schema", "result": "output"}
# Task statuses in which the assignee or reviewer may read the released artifacts its task references.
READ_STATUSES = ("defined", "in_progress", "questions", "for_review", "changes_requested")
CHAIN_LIMIT = 12
KIND_LABELS = {"profile": N_("Profile"), "run": N_("Run"), "work": N_("Work"), "approval": N_("Approval")}
PROFILE_STATUS_LABELS = {"generated": N_("Awaits release"), "released": N_("Released"), "rejected": N_("Rejected")}
# Task status → status of a chain cell (symbols and colours of `_workflow_chain.html`).
CELL_STATUS = {"draft": "pending", "defined": "pending", "in_progress": "active", "questions": "waiting",
               "for_review": "waiting", "changes_requested": "waiting", "aborted": "failed", "done": "succeeded",
               "cancelled": "cancelled"}
MAX_INLINE_BYTES = 200_000  # Larger artifacts are named with their API path in TASK.md instead of inlined


# --- Artifacts ---------------------------------------------------------------------------------------------------

def _artifact(session: Session, ref_type: str, ref_id: int) -> tuple[object, int, str, Dataset | None] | None:
    """(row, project id, status, dataset) of an artifact, or None."""
    if ref_type == "dataset":
        ds = session.get(Dataset, ref_id)
        return (ds, ds.project_id, ds.status, ds) if ds else None
    if ref_type == "profile":
        row = session.get(DatasetProfile, ref_id)
        if row is None:
            return None
        ds = session.get(Dataset, row.dataset_id)
        return row, ds.project_id, row.status, ds
    if ref_type == "result":
        row = session.get(RunResult, ref_id)
        if row is None:
            return None
        task = session.get(Task, row.task_id)
        return row, task.project_id, row.status, session.get(Dataset, row.dataset_id) if row.dataset_id else None
    return None


def label(ref_type: str, ref_id: int, dataset: Dataset | None = None) -> str:
    """`DS-0003`, `profile#7`, `result#12` (concept §1)."""
    if ref_type == "dataset":
        return dataset.dataset_key if dataset else f"dataset#{ref_id}"
    return f"{ref_type}#{ref_id}"


def status_label(ref_type: str, status: str) -> str:
    from app.services import output_gate

    table = {"dataset": datasets.STATUS_LABELS, "profile": PROFILE_STATUS_LABELS,
             "result": output_gate.STATUS_LABELS}[ref_type]
    return _(table[status]) if status in table else status


def refs(session: Session, task: Task) -> list[TaskDataRef]:
    if task.id is None:
        return []
    return list(session.scalars(select(TaskDataRef).where(TaskDataRef.task_id == task.id).order_by(TaskDataRef.id)))


# --- Rule 3 and references ---------------------------------------------------------------------------------------

def check_rule(project: Project, task: Task, ref_type: str, status: str, target: str | None = None) -> None:
    """Rule 3 of §1 for one reference under the class `target` (default: the task's effective class)."""
    cls = target or zones.effective_class(project, task)
    if ref_type == "dataset" and cls != "sealed":
        raise Unprocessable(_("A task with an input dataset must be 'sealed' (now '%(cls)s')", cls=cls),
                            code="data_class_ref")
    if ref_type in ("profile", "result") and cls != "sealed" and status != "released":
        raise Unprocessable(_("Only released profiles and results may be referenced by a task in zone C or L"),
                            code="data_ref_unreleased")


def check_class(session: Session, project: Project, task: Task, new: str | None) -> None:
    """Rule 3 when the task's own class changes (`tasks._set_class`): the references must fit the new class."""
    target = new or project.privacy
    for ref in refs(session, task):
        found = _artifact(session, ref.ref_type, ref.ref_id)
        check_rule(project, task, ref.ref_type, found[2] if found else "deleted", target)


def add(session: Session, task: Task, ref_type: str, ref_id: int, actor: Actor | None,
        purpose: str | None = None) -> TaskDataRef:
    """Add a reference (idempotent). The artifact must belong to the task's project and fit rule 3."""
    if PURPOSES.get(ref_type) is None or (purpose or PURPOSES[ref_type]) != PURPOSES[ref_type]:
        raise Unprocessable(_("Unknown data reference '%(type)s/%(purpose)s'", type=ref_type, purpose=purpose or "-"),
                            code="invalid_data_ref")
    found = _artifact(session, ref_type, ref_id)
    if found is None or found[1] != task.project_id:
        raise Unprocessable(_("%(ref)s does not exist in the task's project", ref=label(ref_type, ref_id)),
                            code="invalid_data_ref")
    _row, _project_id, status, ds = found
    check_rule(session.get(Project, task.project_id), task, ref_type, status)
    existing = session.scalar(select(TaskDataRef).where(TaskDataRef.task_id == task.id, TaskDataRef.ref_type == ref_type,
                                                        TaskDataRef.ref_id == ref_id))
    if existing is not None:
        return existing
    from app.services.state_machine import add_event

    row = TaskDataRef(task_id=task.id, ref_type=ref_type, ref_id=ref_id, purpose=PURPOSES[ref_type],
                      created_by=actor.id if actor else None)
    session.add(row)
    add_event(session, task, actor, "data_ref", None, label(ref_type, ref_id, ds), PURPOSES[ref_type])
    session.flush()
    return row


def sync_run_input(session: Session, task: Task, actor: Actor | None) -> None:
    """Runner tasks keep `run_dataset_id` (the runner's job reads it); the `dataset`/`input` reference follows it."""
    if task.task_kind not in zones.RUNNER_TASK_KINDS:
        return
    for ref in refs(session, task):
        if ref.ref_type == "dataset" and ref.ref_id != task.run_dataset_id:
            session.delete(ref)
    session.flush()
    if task.run_dataset_id:
        add(session, task, "dataset", task.run_dataset_id, actor)


def listing(session: Session, task: Task) -> list[dict]:
    """References for the task payload and the task page: type, id, label and status – never content."""
    out = []
    for ref in refs(session, task):
        found = _artifact(session, ref.ref_type, ref.ref_id)
        status = found[2] if found else "deleted"
        ds = found[3] if found else None
        out.append({"type": ref.ref_type, "id": ref.ref_id, "purpose": ref.purpose,
                    "label": label(ref.ref_type, ref.ref_id, ds), "status": status,
                    "status_label": status_label(ref.ref_type, status),
                    "dataset": ds.dataset_key if ds else None})
    return out


# --- Who reads what ----------------------------------------------------------------------------------------------

def _reading_tasks(session: Session, actor: Actor, ref_type: str, ref_id: int) -> list[Task]:
    return list(session.scalars(select(Task).join(TaskDataRef, TaskDataRef.task_id == Task.id).where(
        TaskDataRef.ref_type == ref_type, TaskDataRef.ref_id == ref_id, Task.status.in_(READ_STATUSES),
        (Task.assigned_to == actor.id) | (Task.reviewer_id == actor.id))))


def _agent_holds(session: Session, actor: Actor | None, task: Task) -> bool:
    """The agent works on or reviews the open task and may handle its zone."""
    if actor is None or actor.actor_type != "agent" or not actor.is_active or actor.model_location == "none":
        return False
    if task.status not in READ_STATUSES or actor.id not in (task.assigned_to, task.reviewer_id):
        return False
    return zones.may_handle(session.get(Project, task.project_id), actor, task, session=session)


def agent_may_read(session: Session, actor: Actor | None, ref_type: str, ref_id: int) -> bool:
    """Concept §1: an agent reads a released artifact only through an open task of its own (assignee or reviewer)
    that references it. Runners (`model_location = none`) never read released content."""
    if actor is None or actor.actor_type != "agent":
        return False
    return any(_agent_holds(session, actor, t) for t in _reading_tasks(session, actor, ref_type, ref_id))


def artifacts(session: Session, task: Task, actor: Actor, via: str = "api") -> list[dict]:
    """Content of the released artifacts the task references, for TASK.md (`GET /tasks/{nr}/artifacts`). Agents:
    only for their own open task; humans: project members. Datasets are named only – their content never leaves
    zone R this way. Each inlined read is written to the data audit (`profile_read`/`result_read`)."""
    project = session.get(Project, task.project_id)
    if actor.actor_type == "agent":
        if not _agent_holds(session, actor, task):
            raise Forbidden(_("Only the assignee or reviewer of an open task reads its data artifacts"),
                            code="artifacts_forbidden")
    elif not datasets.is_member(session, project, actor):
        raise NotFound(_("Task '%(task_nr)s' not found", task_nr=task.task_nr))
    out = []
    for ref in refs(session, task):
        found = _artifact(session, ref.ref_type, ref.ref_id)
        if found is None:
            continue
        row, _project_id, status, ds = found
        item = {"type": ref.ref_type, "id": ref.ref_id, "purpose": ref.purpose,
                "label": label(ref.ref_type, ref.ref_id, ds), "status": status,
                "dataset": ds.dataset_key if ds else None, "content": None, "url": None}
        if ref.ref_type == "profile" and status == "released":
            item["url"] = f"/api/v1/data/profiles/{row.id}"
            item["content"] = {"schema": json.loads(row.schema_json), "sample_csv": row.sample_csv,
                               "schema_fingerprint": row.schema_fingerprint}
        elif ref.ref_type == "result" and status == "released" and row.released_json:
            item["url"] = f"/api/v1/data/results/{row.id}"
            item["content"] = json.loads(row.released_json)
        if item["content"] is not None and len(json.dumps(item["content"]).encode()) > MAX_INLINE_BYTES:
            item["content"], item["truncated"] = None, True
        elif item["content"] is not None:
            data_audit.append(session, f"{ref.ref_type}_read", ref.ref_type, row.id, actor=actor, via=via,
                              task_id=task.id, detail={"task": task.task_nr})
        out.append(item)
    return out


# --- Follow-up drafts at a zone change ---------------------------------------------------------------------------

def _draft(session: Session, origin: Task | None, project: Project, **kw) -> Task:
    """A draft created by the project owner: a human confirms each zone change before release (concept §1)."""
    from app.services import tasks

    owner = session.get(Actor, project.owner_id)
    return tasks.create(session, owner, project=project.project_key, status="draft",
                        follow_up_of=origin.task_nr if origin is not None else None, **kw)


def _has_follow_up(session: Session, origin: Task, ref_type: str | None = None, ref_id: int | None = None,
                   kind: str | None = None) -> bool:
    stmt = select(Task.id).where(Task.follow_up_of_id == origin.id, Task.status != "cancelled")
    if kind:
        stmt = stmt.where(Task.task_kind == kind)
    if ref_type:
        stmt = stmt.join(TaskDataRef, TaskDataRef.task_id == Task.id).where(TaskDataRef.ref_type == ref_type,
                                                                            TaskDataRef.ref_id == ref_id)
    return session.scalar(stmt.limit(1)) is not None


def propose_script(session: Session, profile: DatasetProfile, ds: Dataset, actor: Actor) -> Task:
    """Profile released → draft "Write analysis script" (zone C) with the schema reference."""
    project = session.get(Project, ds.project_id)
    origin = session.get(Task, profile.task_id) if profile.task_id else None
    heading = _("Released profile #%(id)s; schema fingerprint %(fingerprint)s.",
                id=profile.id, fingerprint=profile.schema_fingerprint)
    description = heading + (f"\n\n```json\n{profile.schema_json}\n```\n\n"
                             f"Synthetic sample:\n```csv\n{profile.sample_csv}\n```")
    if len(description.encode("utf-8")) > 60000:  # tasks.description is TEXT, not MEDIUMTEXT.
        description = heading + "\n\n" + _(
            "Read the released schema and synthetic sample at %(url)s after claiming this task.",
            url=f"/api/v1/data/profiles/{profile.id}")
    draft = _draft(session, origin, project, title=_("Write analysis script for %(key)s", key=ds.dataset_key),
                   data_class="cloud_ok", description=description)
    add(session, draft, "profile", profile.id, actor)
    return draft


def _script_path(session: Session, project: Project, task: Task) -> str | None:
    """The one Python file the merged pull request added or changed outside the tests, if there is exactly one."""
    from app.services import runner_jobs, settings_store
    from app.services.gitea import GiteaError

    if not (task.pr_number and project.gitea_repo):
        return None
    try:
        names = settings_store.gitea_client(session).pull_files(project.gitea_owner, project.gitea_repo,
                                                                task.pr_number)
    except (GiteaError, OSError) as e:  # A missing path only leaves the field for the human to fill in.
        log.warning("pull files of %s: %s", task.task_nr, e)
        return None
    candidates = []
    for name in names:
        path = PurePosixPath(name)
        if path.suffix != ".py" or path.parts[0] in ("tests", "test") or path.name.startswith("test_"):
            continue
        try:
            candidates.append(runner_jobs._script_path(name))
        except Unprocessable:
            continue
    return candidates[0] if len(candidates) == 1 else None


def propose_run(session: Session, code: Task, actor: Actor | None) -> Task | None:
    """Script merged (code task with a schema reference approved) → draft "Run script" (zone R) with the input
    dataset and `run_commit_sha` = merge commit. The script path is taken from the pull request when it changed
    exactly one Python file outside the tests; otherwise the human fills it in before release."""
    if not code.merge_commit_sha or _has_follow_up(session, code, kind="run"):
        return None
    profile_ids = [r.ref_id for r in refs(session, code) if r.ref_type == "profile"]
    profile = session.get(DatasetProfile, profile_ids[-1]) if profile_ids else None
    ds = session.get(Dataset, profile.dataset_id) if profile else None
    if ds is None or ds.status in ("receiving", "deleted"):
        return None
    project = session.get(Project, code.project_id)
    path = _script_path(session, project, code)
    description = _("Run the script merged in %(task)s (commit %(sha)s) on %(key)s in the runner sandbox. The result "
                    "goes through the output gate before anything leaves zone R.", task=code.task_nr,
                    sha=code.merge_commit_sha[:10], key=ds.dataset_key)
    criteria = _("The output gate checks pass and a human releases the result.")
    draft = _draft(session, code, project, title=_("Run script on %(key)s", key=ds.dataset_key),
                   description=description, acceptance_criteria=criteria, task_kind="run",
                   run={"commit_sha": code.merge_commit_sha, "script_path": path, "dataset": ds.dataset_key})
    return draft


def propose_interpretation(session: Session, run: Task, result: RunResult, actor: Actor | None) -> Task | None:
    """Result released → draft "Interpret result" (zone C) with the output reference."""
    if result.status != "released" or _has_follow_up(session, run, "result", result.id):
        return None
    project = session.get(Project, run.project_id)
    ds = session.get(Dataset, result.dataset_id) if result.dataset_id else None
    description = _("Interpret the released result #%(id)s of %(task)s. TASK.md lists the released aggregate under "
                    "\"Approved data artifacts\"; it is also readable at %(url)s while you work on this task.",
                    id=result.id, task=run.task_nr, url=f"/api/v1/data/results/{result.id}")
    draft = _draft(session, run, project, title=_("Interpret result #%(id)s%(on)s", id=result.id,
                                                  on=_(" on %(key)s", key=ds.dataset_key) if ds else ""),
                   data_class="cloud_ok", description=description)
    add(session, draft, "result", result.id, actor)
    return draft


def on_transition(session: Session, task: Task, action: str, actor: Actor | None, payload: dict | None) -> None:
    """Hook of `state_machine.transition`: drafts for the next zone of an ad-hoc chain. A draft that cannot be
    created (e.g. archived project) is skipped in a savepoint – the merge or release already happened."""
    if task.workflow_run_step_id:
        return
    propose = None
    if action == "approve" and task.task_kind == "work":
        if any(r.ref_type == "profile" for r in refs(session, task)):
            def propose():
                return propose_run(session, task, actor)
    elif action == "gate_release":
        result = (payload or {}).get("_gate")
        if isinstance(result, RunResult):
            def propose():
                return propose_interpretation(session, task, result, actor)
    if propose is None:
        return
    try:
        with session.begin_nested():
            propose()
    except AppError as e:
        log.warning("no follow-up draft for %s: %s", task.task_nr, e.message)


# --- Chain view --------------------------------------------------------------------------------------------------

def _chain_tasks(session: Session, task: Task) -> list[Task]:
    root, seen = task, {task.id}
    while root.follow_up_of_id and len(seen) < CHAIN_LIMIT:
        parent = session.get(Task, root.follow_up_of_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        root = parent
    out, queue = [], [root]
    while queue and len(out) < CHAIN_LIMIT:
        current = queue.pop(0)
        out.append(current)
        queue.extend(session.scalars(select(Task).where(Task.follow_up_of_id == current.id,
                                                        Task.status != "cancelled").order_by(Task.id)))
    if task not in out:  # Chains longer than the limit: keep the task itself visible.
        out[-1] = task
    return out


def _handover(session: Session, t: Task) -> str | None:
    """What a chain step hands to the next: profile#7 released, commit a1b2c3d, result#12 pending."""
    if t.task_kind == "profile":
        row = session.scalar(select(DatasetProfile).where(DatasetProfile.task_id == t.id)
                             .order_by(DatasetProfile.id.desc()).limit(1))
        return f"profile#{row.id} · {status_label('profile', row.status)}" if row else None
    if t.task_kind == "run":
        row = session.scalar(select(RunResult).where(RunResult.task_id == t.id).order_by(RunResult.id.desc()).limit(1))
        return f"result#{row.id} · {status_label('result', row.status)}" if row else None
    if t.merge_commit_sha:
        return _("commit %(sha)s", sha=t.merge_commit_sha[:7])
    return None


def chain(session: Session, task: Task) -> list[dict] | None:
    """Cells of the chain view above the description (concept §8), or None when the task is not part of a chain
    across zones (no follow-up link, or neither a runner step nor a data reference in it)."""
    if task.workflow_run_step_id or task.id is None:
        return None
    if not task.follow_up_of_id and session.scalar(select(Task.id).where(Task.follow_up_of_id == task.id)
                                                   .limit(1)) is None:
        return None
    members = _chain_tasks(session, task)
    ids = [t.id for t in members]
    with_refs = set(session.scalars(select(TaskDataRef.task_id).where(TaskDataRef.task_id.in_(ids))))
    if len(members) < 2 or not (with_refs or any(t.task_kind in zones.RUNNER_TASK_KINDS for t in members)):
        return None
    names = dict(session.execute(select(Actor.id, Actor.name).where(
        Actor.id.in_([t.assigned_to for t in members if t.assigned_to]))).all())
    projects = {p.id: p for p in session.scalars(select(Project).where(Project.id.in_({t.project_id
                                                                                         for t in members})))}
    from app.services.workflows.ui import STEP_SYMBOLS

    cells = []
    for t in members:
        status = CELL_STATUS.get(t.status, "pending")
        cells.append({"key": t.title[:40], "type": t.task_kind, "label": _(KIND_LABELS.get(t.task_kind, t.task_kind)),
                      "zone_chain": True, "task_status": t.status, "position": len(cells) + 1, "attempt": 0,
                      "status": status, "symbol": STEP_SYMBOLS.get(status, "?"), "task_nr": t.task_nr,
                      "data_class": zones.effective_class(projects[t.project_id], t), "current": False,
                      "who": names.get(t.assigned_to), "handover": _handover(session, t), "duration": None,
                      "error_code": None, "repaired": False, "pr_number": t.pr_number})
    return cells

