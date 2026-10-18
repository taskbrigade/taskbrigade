"""Output gate (research/konzept-datenzonen.md §6/§8, guide docs/data-store.md → Output gate).

A runner job's outputs wait encrypted in quarantine (`run_results`, status `quarantine`). When the runner reports
the job, `check` decrypts `result.json` on the server, runs the automatic checks 1–9 (`gate_checks`; 4 and 5 come
from the sandboxed probe, `app/runner/gate_probe.py`) and sets `checks_failed` or `pending_approval`. A human then
decides in the web UI only (never through Telegram or an agent's API token):

- **release** – `released`, run task `done`; `released_json` holds the released form (floats rounded, helper
  columns removed). Only now may the content leave zone R: `released_content` hands it to agents.
- **reject** (reason required) – `rejected`, run task `changes_requested` with its script commit cleared.
- **keep in zone R** – `kept_sealed`, run task `done` without a release (results without a zone change, e.g. a list
  for the accounting team that is read in the release view; research/konzept-workflows.md §11.2).

Who decides: project owner, admins and project members with the data right. Four-eyes (project option
`data_four_eyes`, default from *Admin → Settings*, on): the releaser must neither have created the run task nor
merged its script. Every check and decision goes to the hash-chained `data_audit`; `approvals_json` keeps who
decided what and when.
"""
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import data_crypto
from app.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.i18n import N_, _
from app.models import Actor, Dataset, Project, RunResult, Task, TaskEvent
from app.services import data_audit, datasets, gate_checks, gate_privacy, settings_store
from app.services.state_machine import transition

DECISIONS = ("release", "reject", "keep_sealed")
KEEPABLE = ("pending_approval", "checks_failed")
LOG_LIMIT = 64 * 1024
NOTE_MAX = 2000
SETTING_DEFAULTS = {"gate_min_group_size": 5, "gate_max_rows": 1000, "gate_dominance_pct": 85}

STATUS_LABELS = {"quarantine": N_("In quarantine"), "checks_failed": N_("Checks failed"),
                 "pending_approval": N_("Awaits release"), "released": N_("Released"),
                 "rejected": N_("Rejected"), "deleted": N_("Deleted"), "kept_sealed": N_("Kept in zone R")}
CHECK_LABELS = {1: N_("Format tbr-aggregate/1"), 2: N_("Size"), 3: N_("Minimum group size"),
                4: N_("No identifier values"), 5: N_("No raw rows"), 6: N_("Free text"), 7: N_("Dominance"),
                8: N_("Differencing"), 9: N_("Precision")}
CODE_TEXTS = {
    "unreadable": N_("result.json could not be decrypted"),
    "extra_files": N_("only result.json may be written to out/ (%(count)s other entries)"),
    "missing": N_("result.json is missing"),
    "too_large_to_parse": N_("result.json is far too large to be checked"),
    "not_json": N_("result.json is not valid JSON"),
    "wrong_format": N_("result.json does not declare the format tbr-aggregate/1"),
    "unknown_keys": N_("result.json has keys other than format, tables and metrics"),
    "no_tables": N_("tables is missing or not a list"),
    "bad_table": N_("a table is not an object with the allowed keys"),
    "bad_table_name": N_("a table name is missing, too long or not unique"),
    "bad_columns": N_("columns must be a list of unique, non-empty names"),
    "bad_rows": N_("rows must be a list"),
    "bad_row": N_("a row does not match the columns or holds a nested value"),
    "bad_count_column": N_("count_column does not name a column"),
    "bad_max_contribution": N_("max_contribution must map sum columns to distinct helper columns"),
    "bad_metrics": N_("metrics must map names to numbers"),
    "too_large": N_("%(bytes)s bytes, limit %(limit)s"),
    "too_many_tables": N_("%(tables)s tables, limit %(limit)s"),
    "too_many_rows": N_("a table has more than %(limit)s rows"),
    "no_count_column": N_("a table has no count_column"),
    "count_not_integer": N_("a count is not a whole number"),
    "small_group": N_("%(count)s group(s) smaller than k = %(k)s"),
    "probe_missing": N_("the gate probe did not report – the check cannot pass"),
    "probe_error": N_("the gate probe could not read the input (%(reason)s) – the check cannot pass"),
    "identifier_values": N_("%(hits)s value(s) equal identifier values of the input"),
    "raw_rows": N_("%(hits)s output row(s) reproduce input rows"),
    "free_text": N_("%(count)s long text(s) or long digit sequence(s)"),
    "not_declared": N_("no table declares max_contribution – not checkable"),
    "dominant": N_("%(count)s group(s) where one entry exceeds %(pct)s %% of the sum"),
    "close_to_released": N_("%(count)s group(s) differ by fewer than k = %(k)s from a released result"),
    "noisy_history": N_("%(count)s earlier release(s) contain noisy counts; differencing cannot be checked against them"),
}


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


# --- Settings and rights -----------------------------------------------------------------------------------------

def _int_setting(session: Session, key: str) -> int:
    default = SETTING_DEFAULTS[key]
    try:
        value = int(settings_store.get(session, key) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def config(session: Session, dataset: Dataset | None) -> gate_checks.Config:
    """k comes from the dataset when it sets one (§3 `min_group_size`), else from *Admin → Settings*."""
    k = dataset.min_group_size if dataset is not None and dataset.min_group_size else None
    return gate_checks.Config(k=k or _int_setting(session, "gate_min_group_size"),
                              max_rows=_int_setting(session, "gate_max_rows"),
                              dominance_pct=min(_int_setting(session, "gate_dominance_pct"), 100),
                              dominance_hard=settings_store.get_bool(session, "gate_dominance_hard"))


def four_eyes(session: Session, project: Project) -> bool:
    if project.data_four_eyes is not None:
        return bool(project.data_four_eyes)
    return settings_store.get_bool(session, "data_four_eyes")


def may_decide(session: Session, project: Project, actor: Actor | None) -> bool:
    """Humans only: project owner, admins, and project members with the data right."""
    if not datasets.is_member(session, project, actor):
        return False
    return actor.role == "admin" or project.owner_id == actor.id or bool(actor.data_access)


may_view = may_decide  # The release view shows the full output and the logs: same circle of people


def script_task(session: Session, result: RunResult) -> Task | None:
    """The approved code task whose merge commit is the result's script."""
    if not result.script_commit_sha:
        return None
    task = session.get(Task, result.task_id)
    return session.scalars(select(Task).where(Task.project_id == task.project_id, Task.status == "done",
                                              Task.merge_commit_sha == result.script_commit_sha)
                           .order_by(Task.id.desc()).limit(1)).first()


def involved(session: Session, result: RunResult) -> set[int]:
    """Four-eyes rule: who created the run task and who merged its script."""
    task = session.get(Task, result.task_id)
    ids = {task.created_by}
    code = script_task(session, result)
    if code is not None:
        ids |= set(session.scalars(select(TaskEvent.actor_id).where(TaskEvent.task_id == code.id,
                                                                     TaskEvent.event_type == "merged")))
    return {i for i in ids if i is not None}


# --- Encrypted files ---------------------------------------------------------------------------------------------

def files(result: RunResult) -> dict[str, dict]:
    return {f["name"]: f for f in json.loads(result.files_json or "[]")}


def _context(result: RunResult) -> str:
    from app.services.runner_jobs import result_context

    return result_context(result.job_id)


def read_file(result: RunResult, name: str, limit: int) -> tuple[bytes, bool] | None:
    """Plaintext of one quarantined file, at most `limit` bytes; (data, truncated). None if there is no such file.
    Raises `data_crypto.DecryptError`/`DataKeyError` when it cannot be decrypted."""
    entry = files(result).get(name)
    if entry is None or not result.dek_wrapped:
        return None
    dek = data_crypto.unwrap_dek(datasets.kek(), result.dek_wrapped, _context(result))
    out, size = [], 0
    with open(datasets.resolve(entry["path"]), "rb") as f:
        for chunk in data_crypto.decrypt_chunks(f, dek):
            out.append(chunk)
            size += len(chunk)
            if size > limit:
                return b"".join(out)[:limit], True
    return b"".join(out), False


def _result_json(result: RunResult) -> bytes | None:
    """`result.json` checked against the SHA-256 the runner reported; None if missing, unreadable or changed."""
    try:
        got = read_file(result, gate_checks.RESULT_FILE, gate_checks.PARSE_LIMIT)
    except (data_crypto.DecryptError, data_crypto.DataKeyError, OSError, Unprocessable):
        return None
    if got is None:
        return None
    data, truncated = got
    if not truncated and result.output_sha256 and hashlib.sha256(data).hexdigest() != result.output_sha256:
        return None
    return data


# --- Automatic checks --------------------------------------------------------------------------------------------

def _released_docs(session: Session, result: RunResult) -> tuple[list[dict], int]:
    if result.dataset_id is None:
        return [], 0
    rows = session.execute(select(RunResult.released_json, RunResult.release_policy_json).where(
        RunResult.dataset_id == result.dataset_id, RunResult.status == "released", RunResult.id != result.id))
    out, noisy = [], 0
    for text, policy_text in rows:
        try:
            if json.loads(policy_text or "{}").get("epsilon") is not None:
                noisy += 1
                continue
            out.append(json.loads(text or "{}"))
        except ValueError:
            continue
    return out, noisy


def check(session: Session, result: RunResult, probe: dict | None, refused: int = 0) -> gate_checks.Report:
    """Run checks 1–9 on a finished job and set `checks_failed` or `pending_approval`. Audited as `gate_checked`
    (check numbers only)."""
    dataset = session.get(Dataset, result.dataset_id) if result.dataset_id else None
    cfg = config(session, dataset)
    outputs = [name for name, f in files(result).items() if f["kind"] == "output"]
    data = _result_json(result) if gate_checks.RESULT_FILE in outputs else b""
    if data is None:
        report = gate_checks.failed_report("unreadable")
    else:
        released, noisy = _released_docs(session, result)
        report = gate_checks.evaluate(data, outputs, probe, cfg, released, refused)
        for entry in report.checks:
            if entry["n"] == 8 and entry["status"] == "ok" and noisy:
                entry.update(status="warning", code="noisy_history", params={"count": noisy})
    result.checks_json = report.as_json(cfg.k)
    result.status = "checks_failed" if report.failed else "pending_approval"
    task = session.get(Task, result.task_id)
    data_audit.append(session, "gate_checked", "result", result.id, via="runner", task_id=task.id,
                      detail={"task": task.task_nr, "status": result.status, "failed": report.failed,
                              "warnings": report.warnings, "k": cfg.k})
    return report


def checks(result: RunResult) -> dict:
    try:
        return json.loads(result.checks_json or "{}")
    except ValueError:
        return {}


def describe(entry: dict) -> str:
    """Text of one check entry in the current language (no data values: codes, counts, limits only)."""
    code, params = entry.get("code"), dict(entry.get("params") or {})
    params.setdefault("count", entry.get("count", 0))
    if code in CODE_TEXTS:
        try:
            return _(CODE_TEXTS[code], **params)
        except (KeyError, TypeError, ValueError):  # A report written by another version: show the code
            return code
    if entry["status"] == "not_run":
        return _("not run – an earlier hard check failed")
    if entry["n"] == 1 and entry["status"] == "ok":
        return _("%(tables)s table(s)", tables=params.get("tables", 0))
    if entry["n"] == 2 and entry["status"] == "ok":
        return _("%(tables)s table(s) / %(rows)s row(s)", tables=params.get("tables", 0), rows=params.get("rows", 0))
    if entry["n"] == 3 and entry["status"] == "ok":
        return _("every group ≥ %(k)s", k=params.get("k", "?"))
    if entry["n"] == 4 and entry["status"] == "ok":
        return _("%(columns)s identifier column(s) in %(rows)s input row(s) – no match",
                 columns=params.get("identifier_columns", 0), rows=params.get("input_rows", 0))
    if entry["n"] == 5 and entry["status"] == "ok":
        return _("%(rows)s input row(s) compared – no match", rows=params.get("input_rows", 0))
    if entry["n"] == 8 and entry["status"] == "ok":
        return _("compared with %(n)s released result(s)", n=params.get("compared", 0))
    if entry["n"] == 9:
        return _("%(n)s value(s) rounded to %(d)s decimals", n=params.get("rounded", 0), d=params.get("decimals", 2))
    return ""


def check_rows(result: RunResult) -> list[dict]:
    return [{**c, "label": _(CHECK_LABELS.get(c["n"], "?")), "text": describe(c)} for c in checks(result).get("checks", [])]


def summary(result: RunResult) -> str:
    """One line for the task thread: which checks failed or how many warnings – never contents."""
    data = checks(result)
    if data.get("failed"):
        names = ", ".join(f"{n} ({_(CHECK_LABELS.get(n, '?'))})" for n in data["failed"])
        return _("Output gate: check %(checks)s failed.", checks=names)
    return _("Output gate: all hard checks passed, %(n)s warning(s). Result #%(id)s waits for release.",
             n=len(data.get("warnings") or []), id=result.id)


def send_back(session: Session, task: Task, result: RunResult, runner: Actor) -> None:
    """Hard check failed: the run task goes to `changes_requested` (script commit cleared, event `gate_failed`)."""
    transition(session, task, "gate_return", runner, {
        "_gate": result, "_author": runner, "_event": "gate_failed",
        "body": _("The output gate refused result #%(id)s. Repair the script in a code task, then set its merge "
                  "commit as the run fields of %(task)s to run it again – or keep the result in zone R in the "
                  "release view.", id=result.id, task=task.task_nr)})


# --- Decisions ---------------------------------------------------------------------------------------------------

def get(session: Session, result_id: int, lock: bool = False) -> RunResult:
    stmt = select(RunResult).where(RunResult.id == result_id)
    result = session.scalars(stmt.with_for_update() if lock else stmt).first()
    if result is None:
        raise NotFound(_("Result #%(id)s not found", id=result_id))
    return result


def get_visible(session: Session, result_id: int, actor: Actor | None) -> tuple[RunResult, Task, Project]:
    """The result if the actor may open the release view; otherwise "not found"."""
    result = get(session, result_id)
    task = session.get(Task, result.task_id)
    project = session.get(Project, task.project_id)
    if not may_view(session, project, actor):
        raise NotFound(_("Result #%(id)s not found", id=result_id))
    return result, task, project


def latest_id(session: Session, task: Task) -> int | None:
    return session.scalar(select(func.max(RunResult.id)).where(RunResult.task_id == task.id))


def options(session: Session, result: RunResult, task: Task, project: Project, actor: Actor | None) -> dict:
    """Which decisions the actor can take now, and why release is blocked (four-eyes)."""
    allowed = may_decide(session, project, actor) and result.id == latest_id(session, task)
    pending = allowed and result.status == "pending_approval" and task.status == "for_review"
    blocked = pending and four_eyes(session, project) and actor.id in involved(session, result)
    budget = gate_privacy.view(session.get(Dataset, result.dataset_id) if result.dataset_id else None)
    keep = (allowed and result.status in KEEPABLE and task.status in ("for_review", "changes_requested"))
    return {"release": pending and not blocked and not budget["exhausted"], "reject": pending,
            "keep_sealed": keep, "four_eyes_blocked": blocked, "budget_blocked": budget["exhausted"],
            "four_eyes": four_eyes(session, project)}


def decide(session: Session, result_id: int, actor: Actor, decision: str, note: str | None = None,
           noise_reviewed: bool = False, policy_token: str | None = None) -> RunResult:
    """Release, reject or keep a result (web UI only). Raises 403 `four_eyes` when the releaser is involved."""
    if decision not in DECISIONS:
        raise Unprocessable(_("Unknown decision"), code="invalid_decision")
    result = get(session, result_id, lock=True)
    task = session.scalars(select(Task).where(Task.id == result.task_id).with_for_update()).first()
    project = session.get(Project, task.project_id)
    if not may_decide(session, project, actor):
        raise Forbidden(_("Only the project owner, admins and project members with the data right decide on "
                          "results"), code="gate_forbidden")
    note = (note or "").strip()[:NOTE_MAX]
    can = options(session, result, task, project, actor)
    if not can[decision]:
        if decision == "release" and can["budget_blocked"]:
            raise Conflict(_("The dataset's output query budget is exhausted"), code="gate_budget_exhausted")
        if decision == "release" and can["four_eyes_blocked"]:
            raise Forbidden(_("Four-eyes rule: a second person must release this result – you created the run task "
                              "or merged its script."), code="four_eyes")
        raise Conflict(_("Result #%(id)s cannot be decided this way now (status '%(status)s', task '%(task)s')",
                         id=result.id, status=result.status, task=task.status), code="gate_not_pending")
    if decision == "reject" and not note:
        raise Unprocessable(_("A reason is required to reject a result"), code="missing_field")
    if decision == "release":
        data = _result_json(result)
        try:
            doc = gate_checks.check_format(data or b"", [gate_checks.RESULT_FILE])
        except gate_checks.Failure as e:
            raise Conflict(_("The quarantined result can no longer be read – it cannot be released"),
                           code="gate_unreadable") from e
        result.released_json = gate_privacy.release(session, result, doc, noise_reviewed, policy_token)
        result.released_at = now()
        result.status, rule, action = "released", "gate_release", "result_released"
        body = _("Result #%(id)s released in the output gate by %(name)s – it may now leave zone R.", id=result.id,
                 name=actor.name)
    elif decision == "reject":
        result.status, rule, action = "rejected", "gate_reject", "result_rejected"
        body = _("Result #%(id)s rejected in the output gate by %(name)s. Repair the script in a code task, then set "
                 "its merge commit as the run fields of %(task)s to run it again.", id=result.id, name=actor.name,
                 task=task.task_nr)
    else:
        result.status, rule, action = "kept_sealed", "gate_keep", "result_kept_sealed"
        body = _("Result #%(id)s stays in zone R (decided by %(name)s); it is not released.", id=result.id,
                 name=actor.name)
    entry = {"actor_id": actor.id, "actor": actor.name, "at": now().isoformat(), "decision": decision, "note": note}
    result.approvals_json = json.dumps(approvals(result) + [entry], ensure_ascii=False)
    data_audit.append(session, action, "result", result.id, actor=actor, via="web", task_id=task.id,
                      detail={"task": task.task_nr, "decision": decision, "note": note[:500],
                              "four_eyes": can["four_eyes"],
                              "release_policy": json.loads(result.release_policy_json or "null")})
    transition(session, task, rule, actor, {"_gate": result, "_author": actor, "body": body})
    return result


def keep_sealed_by_system(session: Session, result: RunResult, reason: str) -> RunResult:
    """Workflows without a gate after the runner step (research/konzept-workflows.md §11.2): the result stays in zone
    R and the run task is done without a release. Hard check failures still stop here."""
    task = session.scalars(select(Task).where(Task.id == result.task_id).with_for_update()).first()
    if result.status != "pending_approval" or task.status != "for_review":
        raise Conflict(_("Result #%(id)s cannot be decided this way now (status '%(status)s', task '%(task)s')",
                         id=result.id, status=result.status, task=task.status), code="gate_not_pending")
    runner = session.get(Actor, result.runner_id) if result.runner_id else None
    if runner is None:
        raise Conflict(_("Result #%(id)s has no runner", id=result.id), code="gate_not_pending")
    result.status = "kept_sealed"
    entry = {"actor_id": None, "actor": None, "at": now().isoformat(), "decision": "keep_sealed", "note": reason}
    result.approvals_json = json.dumps(approvals(result) + [entry], ensure_ascii=False)
    data_audit.append(session, "result_kept_sealed", "result", result.id, via="scheduler", task_id=task.id,
                      detail={"task": task.task_nr, "decision": "keep_sealed", "note": reason[:500]})
    transition(session, task, "gate_keep", None, {"_gate": result, "_author": runner, "body": _(
        "Result #%(id)s stays in zone R (no release step); it is not released.", id=result.id)})
    return result


def approvals(result: RunResult) -> list[dict]:
    try:
        value = json.loads(result.approvals_json or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


# --- Reading -----------------------------------------------------------------------------------------------------

def released_content(session: Session, result_id: int, actor: Actor, via: str = "api") -> dict:
    """The released form of a result – the only way runner output reaches agents (zone C). Anything not released
    is "not found" for everyone. An agent must hold an open task that references the result (`task_data_refs`,
    DZ 9: assignee or reviewer); humans must be project members. Audited as `result_read`."""
    result = session.get(RunResult, result_id)
    if result is None or result.status != "released" or not result.released_json:
        raise NotFound(_("Result #%(id)s not found", id=result_id))
    task = session.get(Task, result.task_id)
    project = session.get(Project, task.project_id)
    if actor.actor_type == "human":
        allowed = datasets.is_member(session, project, actor)
    else:
        from app.services import data_refs

        allowed = data_refs.agent_may_read(session, actor, "result", result.id)
    if not allowed:
        raise NotFound(_("Result #%(id)s not found", id=result_id))
    data_audit.append(session, "result_read", "result", result.id, actor=actor, via=via, task_id=task.id,
                      detail={"task": task.task_nr})
    ds = session.get(Dataset, result.dataset_id) if result.dataset_id else None
    return {"id": result.id, "task": task.task_nr, "dataset": ds.dataset_key if ds else None,
            "released_at": result.released_at.isoformat() if result.released_at else None,
            "content": json.loads(result.released_json)}


def view(session: Session, result: RunResult, task: Task, project: Project, actor: Actor) -> dict:
    """Everything the release view shows: checks, the full output, the logs (decrypted for this request only) and
    the script diff link. Opening it is audited (`result_viewed`)."""
    doc, error = None, None
    if result.status == "released" and result.released_json:
        doc = json.loads(result.released_json)
    elif result.status != "deleted":
        data = _result_json(result)
        if data is None:
            error = _("result.json could not be decrypted")
        else:
            try:
                doc = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                error = _("result.json is not valid JSON")
    logs = {}
    for name in ("stdout.log", "stderr.log"):
        try:
            got = read_file(result, name, LOG_LIMIT) if result.status != "deleted" else None
        except (data_crypto.DecryptError, data_crypto.DataKeyError, OSError, Unprocessable):
            got = (_("(cannot be decrypted)").encode(), False)
        if got is not None:
            logs[name] = {"text": got[0].decode("utf-8", "replace"), "truncated": got[1]}
    data_audit.append(session, "result_viewed", "result", result.id, actor=actor, via="web", task_id=task.id,
                      detail={"task": task.task_nr, "status": result.status})
    ds = session.get(Dataset, result.dataset_id) if result.dataset_id else None
    code = script_task(session, result)
    previous = session.scalars(select(RunResult).where(
        RunResult.dataset_id == result.dataset_id, RunResult.status == "released", RunResult.id < result.id,
        RunResult.script_path == result.script_path).order_by(RunResult.id.desc()).limit(1)).first()
    diff = None
    if (previous and previous.script_commit_sha and result.script_commit_sha and project.gitea_url
            and previous.script_commit_sha != result.script_commit_sha):
        diff = f"{project.gitea_url.rstrip('/')}/compare/{previous.script_commit_sha}...{result.script_commit_sha}"
    merger = None
    if code is not None:
        merger_id = session.scalar(select(TaskEvent.actor_id).where(TaskEvent.task_id == code.id,
                                                                     TaskEvent.event_type == "merged").limit(1))
        merger = session.get(Actor, merger_id).name if merger_id else None
    runtime = (int((result.finished_at - result.started_at).total_seconds())
               if result.started_at and result.finished_at else None)
    helper = sorted({m for t in (doc or {}).get("tables") or [] if isinstance(t, dict)
                     for m in (t.get("max_contribution") or {}).values()}) if isinstance(doc, dict) else []
    rows = check_rows(result)
    hits = {w for c in rows if c["status"] in ("failed", "warning") for w in c.get("where") or []}
    return {"result": result, "task": task, "project": project, "dataset": ds, "doc": doc, "doc_error": error,
            "privacy": gate_privacy.view(ds), "release_policy": json.loads(result.release_policy_json or "null"),
            "hits": hits,
            "logs": logs, "checks": rows, "summary": checks(result), "approvals": approvals(result),
            "code_task": code, "merger": merger, "diff": diff, "previous": previous, "runtime": runtime,
            "helper_columns": helper, "options": options(session, result, task, project, actor),
            "status_label": _(STATUS_LABELS.get(result.status, result.status))}


def pending_for(session: Session, actor: Actor | None) -> list[dict]:
    """Inbox category "Data releases": results waiting for a decision in projects the human may decide on."""
    if actor is None or actor.actor_type != "human":
        return []
    rows = session.execute(select(RunResult, Task, Project).join(Task, Task.id == RunResult.task_id)
                           .join(Project, Project.id == Task.project_id)
                           .where(RunResult.status == "pending_approval", Task.status == "for_review")
                           .order_by(RunResult.id)).all()
    out = []
    for result, task, project in rows:
        if not may_decide(session, project, actor):
            continue
        ds = session.get(Dataset, result.dataset_id) if result.dataset_id else None
        data = checks(result)
        out.append({"id": result.id, "task_nr": task.task_nr, "title": task.title, "project": project.project_key,
                    "dataset": ds.dataset_key if ds else None, "finished_at": result.finished_at,
                    "warnings": len(data.get("warnings") or []),
                    "blocked": four_eyes(session, project) and actor.id in involved(session, result)})
    return out


def for_task(session: Session, task: Task) -> list[dict]:
    """Results of a run task, newest first (task page box "Data")."""
    rows = session.scalars(select(RunResult).where(RunResult.task_id == task.id).order_by(RunResult.id.desc()))
    return [{"id": r.id, "status": r.status, "label": _(STATUS_LABELS.get(r.status, r.status)),
             "finished_at": r.finished_at} for r in rows]


def for_dataset(session: Session, dataset: Dataset) -> list[dict]:
    """Runs on a dataset, newest first (dataset page)."""
    rows = session.execute(select(RunResult, Task.task_nr).join(Task, Task.id == RunResult.task_id)
                           .where(RunResult.dataset_id == dataset.id).order_by(RunResult.id.desc())).all()
    return [{"id": r.id, "task_nr": nr, "status": r.status, "label": _(STATUS_LABELS.get(r.status, r.status)),
             "finished_at": r.finished_at, "script": r.script_path,
             "commit": (r.script_commit_sha or "")[:10]} for r, nr in rows]
