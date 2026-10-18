"""Server side of the data-zone runner (research/konzept-datenzonen.md §2/§5, guide docs/operations.md → Data-zone
runner).

The runner (`app/runner/`, service `taskbrigade-runner.service`) is an agent without a model (`model_location =
none`). It claims `run`/`profile` tasks like any agent, then asks this module for the job: the input dataset's
storage path and wrapped key (it decrypts on the host itself – the plaintext never crosses HTTP), the script at the
task's merged commit, and the resource limits. After the sandbox job it reports the encrypted outputs in
`TBR_DATA_DIR/quarantine`; the output gate (`output_gate`) checks them and the task goes to `for_review` (exit 0,
checks passed), `changes_requested` (a hard gate check failed) or `aborted` (anything else) with a message
**without** output contents. Only the output gate decides a run task; approving it like a code task is refused.
"""
import base64
import binascii
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app import data_crypto
from app.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.i18n import _
from app.models import Actor, Dataset, Project, RunResult, Task, TaskDependency
from app.services import data_audit, settings_store, zones
from app.services.gitea import GiteaError
from app.services.state_machine import add_event, transition

TASK_KINDS = ("work", "run", "profile")  # `approval` tasks come from workflows only
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SCRIPT_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_SCRIPT_BYTES = 1024 * 1024
UNAVAILABLE = ("receiving", "deleted")
# Input file name inside the job (`TBR_INPUT/data<suffix>`); other suffixes become `.bin`.
INPUT_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv", ".json", ".parquet", ".txt")
# Script contract: the input does not match the expected schema (research/konzept-workflows.md §4.1)
SCHEMA_MISMATCH = 3
LIMIT_DEFAULTS = {"runner_memory_mb": 4096, "runner_cpu_percent": 200, "runner_timeout_s": 900}
FAIL_CODES = ("precheck", "script_not_merged", "script_missing", "dataset_unavailable", "profile_unsupported",
              "sandbox_error", "decrypt_error", "output_error", "schema_mismatch")
FILE_KINDS = ("output", "log")


def result_context(job_id: str) -> str:
    """Context the runner binds a result's DEK to (`data_crypto.wrap_dek`)."""
    return f"run-result/{job_id}"


# --- Task fields -------------------------------------------------------------------------------------------------

def normalize_kind(kind: str | None) -> str:
    kind = (kind or "work").strip().lower()
    if kind not in TASK_KINDS:
        raise Unprocessable(_("Task kind must be one of %(kinds)s", kinds=", ".join(TASK_KINDS)),
                            code="invalid_task_kind")
    return kind


def _script_path(value: str | None) -> str:
    path = (value or "").strip()
    parts = PurePosixPath(path).parts
    if (not path or len(path) > 300 or path.startswith("/") or not path.endswith(".py")
            or any(p in (".", "..") or not SCRIPT_PART_RE.match(p) for p in parts)):
        raise Unprocessable(_("Script path must be a relative path of a .py file in the repository (letters, digits, "
                              "'_', '-', '.'; no '..')"), code="invalid_run_field")
    return path


def _dataset(session: Session, project: Project, key: str | None) -> Dataset:
    key = (key or "").strip().upper()
    ds = session.scalar(select(Dataset).where(Dataset.dataset_key == key)) if key else None
    if ds is None or ds.project_id != project.id:
        raise Unprocessable(_("Dataset '%(key)s' does not exist in project %(project)s", key=key or "-",
                              project=project.project_key), code="invalid_run_field")
    if ds.status in UNAVAILABLE:
        raise Unprocessable(_("Dataset %(key)s is not available ('%(status)s')", key=ds.dataset_key,
                              status=ds.status), code="invalid_run_field")
    return ds


def normalize_run(session: Session, project: Project, kind: str, run: dict | None, draft: bool = False) -> dict:
    """Task columns for the runner fields of a new task. `run` tasks need `commit_sha` (the merged commit of the
    script), `script_path` and `dataset`; `profile` tasks only `dataset`; work tasks none. A `draft` may still lack
    the script path (follow-up drafts of the task chain, `data_refs.propose_run`); publishing requires it."""
    run = {k: v for k, v in (run or {}).items() if v not in (None, "")}
    if kind not in zones.RUNNER_TASK_KINDS:
        if run:
            raise Unprocessable(_("Only runner tasks (kind 'run' or 'profile') take run fields"),
                                code="invalid_run_field")
        return {}
    unknown = set(run) - {"commit_sha", "script_path", "dataset"}
    if unknown:
        raise Unprocessable(_("Unknown run fields: %(fields)s", fields=", ".join(sorted(unknown))),
                            code="invalid_run_field")
    out = {"run_dataset_id": _dataset(session, project, run.get("dataset")).id,
           "run_commit_sha": None, "run_script_path": None}
    if kind == "run":
        sha = str(run.get("commit_sha") or "").strip().lower()
        if not SHA_RE.match(sha):
            raise Unprocessable(_("commit_sha must be the 40-character SHA of the merged script commit"),
                                code="invalid_run_field")
        path = run.get("script_path")
        out.update(run_commit_sha=sha, run_script_path=None if draft and not path else _script_path(path))
    elif run.get("commit_sha") or run.get("script_path"):
        raise Unprocessable(_("Profile tasks take only a dataset"), code="invalid_run_field")
    return out


def update_run(session: Session, actor: Actor, task: Task, project: Project, run: dict | None) -> None:
    """Change the runner fields of a task that has not started yet – or of a run task the output gate sent back
    (`changes_requested`, script commit cleared): the merged commit of the repaired script makes it run again."""
    if zones.started(task) and task.status != "changes_requested":
        raise Conflict(_("Work on %(task)s has started – its run fields cannot change any more", task=task.task_nr),
                       code="run_fields_locked")
    fields = normalize_run(session, project, task.task_kind, run, draft=task.status == "draft")
    if not fields:
        return
    for key, value in fields.items():
        setattr(task, key, value)
    add_event(session, task, actor, "run_fields", None, task.run_commit_sha[:10] if task.run_commit_sha else None)
    from app.services import data_refs

    data_refs.sync_run_input(session, task, actor)


def missing_run_fields(task: Task) -> bool:
    """A `run` task is published only with commit, script path and input dataset."""
    return getattr(task, "task_kind", None) == "run" and not (
        task.run_commit_sha and task.run_script_path and task.run_dataset_id)


def run_view(session: Session, task: Task) -> dict | None:
    """Runner fields for the task payload (dataset by key only)."""
    if task.task_kind not in zones.RUNNER_TASK_KINDS:
        return None
    ds = session.get(Dataset, task.run_dataset_id) if task.run_dataset_id else None
    return {"commit_sha": task.run_commit_sha, "script_path": task.run_script_path,
            "dataset": ds.dataset_key if ds else None}


def contract_applies(session: Session, task: Task) -> bool:
    """TASK.md shows the script contract for work tasks whose code may run in the sandbox: the project holds
    datasets, or a `run` task depends on this task."""
    if task.task_kind != "work" or task.id is None:
        return False
    has_data = session.scalar(select(exists().where(Dataset.project_id == task.project_id,
                                                    Dataset.status != "deleted")))
    if has_data:
        return True
    return bool(session.scalar(select(exists().where(
        TaskDependency.depends_on_task_id == task.id, TaskDependency.task_id == Task.id, Task.task_kind == "run"))))


# --- Settings ----------------------------------------------------------------------------------------------------

def limits(session: Session) -> dict:
    """Sandbox limits from *Admin → Settings* (`tbr-admin runner limits` writes them as a unit drop-in)."""
    out = {}
    for key, default in LIMIT_DEFAULTS.items():
        try:
            value = int(settings_store.get(session, key) or default)
        except ValueError:
            value = default
        out[key.removeprefix("runner_")] = value if value > 0 else default
    return out


# --- Jobs --------------------------------------------------------------------------------------------------------

def _job_task(session: Session, runner: Actor, task_nr: str, statuses=("in_progress",)) -> tuple[Task, Project]:
    if not zones.is_runner(runner):
        raise Forbidden(_("Only runner agents may fetch runner jobs"), code="not_a_runner")
    task = session.scalar(select(Task).where(Task.task_nr == task_nr.upper()).with_for_update())
    if task is None:
        raise NotFound(_("Task '%(task_nr)s' not found", task_nr=task_nr))
    if task.assigned_to != runner.id or task.task_kind not in zones.RUNNER_TASK_KINDS:
        raise Forbidden(_("%(task)s is not a runner job of %(name)s", task=task.task_nr, name=runner.name),
                        code="not_your_job")
    if task.status not in statuses:
        raise Conflict(_("%(task)s is '%(status)s', not in progress", task=task.task_nr, status=task.status),
                       code="invalid_transition")
    return task, session.get(Project, task.project_id)


def _input_suffix(ds: Dataset) -> str:
    suffix = PurePosixPath((ds.original_filename or "").lower()).suffix
    return suffix if suffix in INPUT_SUFFIXES else ".bin"


def job(session: Session, runner: Actor, task_nr: str) -> dict:
    """Everything the runner needs for the job of its in-progress task. Audited as `run_started`."""
    task, project = _job_task(session, runner, task_nr)
    ds = session.get(Dataset, task.run_dataset_id) if task.run_dataset_id else None
    if ds is None or ds.status in UNAVAILABLE or not ds.dek_wrapped or not ds.storage_path:
        raise Conflict(_("The input dataset of %(task)s is not available", task=task.task_nr),
                       code="dataset_unavailable")
    if task.task_kind == "profile" and ds.status != "archived":
        ds.status = "profiling"
    data_audit.append(session, "run_started", "dataset", ds.id, actor=runner, via="runner", task_id=task.id,
                      detail={"task": task.task_nr, "dataset": ds.dataset_key, "kind": task.task_kind,
                              "script": task.run_script_path, "commit": task.run_commit_sha})
    from app.models import DatasetProfile
    profile = session.scalar(select(DatasetProfile).where(DatasetProfile.dataset_id == ds.id,
                              DatasetProfile.status == "released").order_by(DatasetProfile.id.desc()).limit(1))
    if task.task_kind == "run" and task.workflow_run_step_id:
        from app.services.workflows.scripts import runner_profile

        profile = runner_profile(session, task) or profile
    job_limits = limits(session)
    if task.workflow_run_step_id:
        from app.models import WorkflowRunStep

        step = session.get(WorkflowRunStep, task.workflow_run_step_id).step
        if step.timeout_minutes:
            job_limits["timeout_s"] = min(job_limits["timeout_s"], step.timeout_minutes * 60)
    return {"task_nr": task.task_nr, "task_kind": task.task_kind, "project": project.project_key,
            "profile_aliases": json.loads(profile.aliases_json) if profile and profile.aliases_json else None,
            "dataset": {"key": ds.dataset_key, "storage_path": ds.storage_path, "sha256": ds.sha256,
                        "size_bytes": ds.size_bytes, "suffix": _input_suffix(ds),
                        "dek_wrapped": base64.b64encode(ds.dek_wrapped).decode()},
            "script": ({"path": task.run_script_path, "commit_sha": task.run_commit_sha}
                       if task.task_kind == "run" else None),
            "limits": job_limits, "min_group_size": ds.min_group_size or 5}


def merged(session: Session, project: Project, sha: str | None) -> bool:
    """`sha` is the merge commit of an approved task of the project (merged = reviewed, concept §5 step 2)."""
    return bool(sha) and bool(session.scalar(select(exists().where(
        Task.project_id == project.id, Task.status == "done", Task.merge_commit_sha == sha))))


def script(session: Session, runner: Actor, task_nr: str) -> bytes:
    """The script of a `run` task at its commit, fetched with the service token. Only merged commits run."""
    task, project = _job_task(session, runner, task_nr)
    if task.task_kind != "run":
        raise Conflict(_("%(task)s runs no script", task=task.task_nr), code="no_script")
    if not merged(session, project, task.run_commit_sha):
        raise Conflict(_("Commit %(sha)s is not the merge commit of an approved task in %(project)s – only reviewed "
                         "and merged scripts run", sha=(task.run_commit_sha or "-")[:10],
                         project=project.project_key), code="script_not_merged")
    if not project.gitea_repo:
        raise Unprocessable(_("Project has no Gitea repository"), code="no_repo")
    try:
        data = settings_store.gitea_client(session).raw_bytes(project.gitea_owner, project.gitea_repo,
                                                              task.run_script_path, task.run_commit_sha)
    except GiteaError as e:
        raise Conflict(f"Gitea: {e}", code="gitea_error") from e
    if data is None:
        raise NotFound(_("%(path)s does not exist at %(sha)s", path=task.run_script_path,
                         sha=task.run_commit_sha[:10]), code="script_missing")
    if len(data) > MAX_SCRIPT_BYTES:
        raise Unprocessable(_("The script is larger than 1 MiB"), code="script_too_large")
    return data


def _time(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError as e:
        raise Unprocessable(_("Invalid time: %(value)s", value=value), code="invalid_result") from e
    return when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when


def _files(body: dict, job_id: str) -> list[dict]:
    files = body.get("files") or []
    if not isinstance(files, list) or len(files) > 20:
        raise Unprocessable(_("files must be a list of at most 20 entries"), code="invalid_result")
    out, prefix = [], f"quarantine/{job_id}/"
    for f in files:
        if not isinstance(f, dict):
            raise Unprocessable(_("Invalid file entry"), code="invalid_result")
        name, path = str(f.get("name") or ""), str(f.get("path") or "")
        if (f.get("kind") not in FILE_KINDS or not SCRIPT_PART_RE.match(name) or not path.startswith(prefix)
                or ".." in path.split("/") or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256") or ""))):
            raise Unprocessable(_("Invalid file entry"), code="invalid_result")
        out.append({"name": name, "kind": f["kind"], "path": path, "size": int(f.get("size") or 0),
                    "sha256": f["sha256"]})
    return out


def _seconds(started: datetime | None, finished: datetime | None) -> int | None:
    return int((finished - started).total_seconds()) if started and finished else None


def record_result(session: Session, runner: Actor, task_nr: str, body: dict) -> RunResult:
    """Store the outcome of a sandbox job and move the task on: exit 0 → output gate checks → `for_review` (result
    waits for release) or `changes_requested` (hard check failed), exit 3 → `aborted` (schema mismatch), anything
    else → `aborted`. Thread texts carry exit code, runtime and check numbers only, never output contents."""
    task, project = _job_task(session, runner, task_nr)
    job_id = str(body.get("job_id") or "")
    if not JOB_ID_RE.match(job_id):
        raise Unprocessable(_("Invalid job id"), code="invalid_result")
    if session.scalar(select(RunResult.id).where(RunResult.job_id == job_id)):
        raise Conflict(_("Job %(job)s was already reported", job=job_id), code="duplicate_job")
    try:
        wrapped = base64.b64decode(body.get("dek_wrapped") or "", validate=True) or None
    except (binascii.Error, ValueError) as e:
        raise Unprocessable(_("Invalid wrapped key"), code="invalid_result") from e
    files = _files(body, job_id)
    exit_code = body.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        raise Unprocessable(_("exit_code must be an integer"), code="invalid_result")
    service_result = str(body.get("service_result") or "")[:40] or None
    output = next((f for f in files if f["kind"] == "output" and f["name"] == "result.json"), None)
    peak = body.get("peak_memory_mb")
    result = RunResult(task_id=task.id, dataset_id=task.run_dataset_id, runner_id=runner.id, job_id=job_id,
                       task_kind=task.task_kind, script_commit_sha=task.run_commit_sha,
                       script_path=task.run_script_path, started_at=_time(body.get("started_at")),
                       finished_at=_time(body.get("finished_at")), exit_code=exit_code,
                       service_result=service_result, peak_memory_mb=peak if isinstance(peak, int) else None,
                       status="quarantine", output_path=output["path"] if output else None,
                       output_sha256=output["sha256"] if output else None,
                       files_json=json.dumps(files) if files else None, dek_wrapped=wrapped if files else None)
    session.add(result)
    session.flush()
    runtime = _seconds(result.started_at, result.finished_at)
    data_audit.append(session, "run_finished", "result", result.id, actor=runner, via="runner", task_id=task.id,
                      detail={"task": task.task_nr, "job": job_id, "exit_code": exit_code,
                              "service_result": service_result, "runtime_s": runtime, "files": len(files)})
    stats = _("exit code %(code)s, runtime %(runtime)s s", code="-" if exit_code is None else exit_code,
              runtime="-" if runtime is None else runtime)
    if exit_code == 0 and service_result in (None, "success") and output is not None:
        if task.task_kind == "profile":
            from app.services import dataset_profiles
            try:
                profile = dataset_profiles.ingest(session, result, runner)
            except (ValueError, KeyError, TypeError, OSError, data_crypto.DecryptError, data_crypto.DataKeyError):
                _profile_failed(session, task)
                transition(session, task, "abort", runner, {"body": _("The profiler output could not be validated.")})
                return result
            transition(session, task, "submit", runner, {"_run_result": result, "summary": _(
                "Profile #%(id)s generated (%(stats)s). A human must release it on the dataset page.",
                id=profile.id, stats=stats)})
            return result
        from app.services import output_gate

        refused = body.get("refused")
        output_gate.check(session, result, body.get("probe"), refused if isinstance(refused, int) else 0)
        transition(session, task, "submit", runner, {"_run_result": result, "summary": _(
            "Runner job finished (%(stats)s). %(gate)s", stats=stats, gate=output_gate.summary(result))})
        if result.status == "checks_failed":
            output_gate.send_back(session, task, result, runner)
            if task.workflow_run_step_id:
                from app.services.workflows import engine
                from app.models import WorkflowRunStep

                row = session.get(WorkflowRunStep, task.workflow_run_step_id)
                if engine._latest(session, row.run)[row.position].id != row.id:
                    engine._internal_status(session, task, "cancelled", _("Workflow script repair requested"))
    else:
        _profile_failed(session, task)
        transition(session, task, "abort", runner, {"body": failure_text(exit_code, service_result, output, stats)})
    return result


def _profile_failed(session, task):
    if task.task_kind == "profile":
        ds = session.get(Dataset, task.run_dataset_id)
        if ds and ds.status == "profiling":
            ds.status = "quarantine"


def failure_text(exit_code, service_result: str | None, output, stats: str) -> str:
    if service_result == "timeout":
        return _("Runner job stopped at the time limit (%(stats)s).", stats=stats)
    if service_result in ("oom-kill", "signal") and exit_code != SCHEMA_MISMATCH:
        return _("Runner job was killed (%(reason)s, %(stats)s) – memory or task limit?", reason=service_result,
                 stats=stats)
    if exit_code == SCHEMA_MISMATCH:
        return _("The input does not match the schema the script expects (exit code 3). The script needs a repair "
                 "for the current data (%(stats)s).", stats=stats)
    if exit_code == 0 and output is None:
        return _("The script wrote no result.json (%(stats)s).", stats=stats)
    return _("Runner job failed (%(stats)s). Logs are kept encrypted in quarantine.", stats=stats)


def fail(session: Session, runner: Actor, task_nr: str, code: str, detail: str | None = None) -> Task:
    """The job could not start (pre-check hit, unmerged script, dataset gone, …): abort the task with a reason.
    `detail` comes from the runner and names code findings only (never data)."""
    task, _project = _job_task(session, runner, task_nr)
    _profile_failed(session, task)
    if code not in FAIL_CODES:
        raise Unprocessable(_("Unknown failure code"), code="invalid_result")
    detail = " ".join((detail or "").split())[:1000]
    data_audit.append(session, "run_failed", "task", task.id, actor=runner, via="runner", task_id=task.id,
                      detail={"task": task.task_nr, "code": code})
    reasons = {
        "precheck": _("The static pre-check of the script found forbidden constructs"),
        "script_not_merged": _("The script commit is not merged – only reviewed scripts run"),
        "script_missing": _("The script does not exist at the given commit"),
        "dataset_unavailable": _("The input dataset is not available"),
        "profile_unsupported": _("Profile jobs need the schema profiler, which this version does not have yet"),
        "sandbox_error": _("The sandbox job could not be started"),
        "decrypt_error": _("The input dataset could not be decrypted"),
        "output_error": _("The job outputs could not be stored in quarantine"),
        "schema_mismatch": _("Workflow script schema changed before execution"),
    }
    text = reasons[code] + (f": {detail}" if detail else "")
    return transition(session, task, "abort", runner, {"body": text, "_workflow_failure_code": code})
