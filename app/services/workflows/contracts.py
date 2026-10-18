"""Declared outputs, input bindings, and the JSON stored on a run step.

Small values live in the run (text, number, JSON up to 64 KiB). Files, commits and
data-zone objects are references. `submit` rejects a broken contract before the task
leaves the agent; approval checks file paths again against the merge commit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.i18n import _
from app.services.workflows.placeholders import Context, Placeholder, PlaceholderError, parse_template

MAX_VALUE_BYTES = 64 * 1024
OUTPUT_TYPES = frozenset({
    "text", "int", "decimal", "bool", "date", "month", "json", "file", "commit",
    "dataset", "profile", "result",
})
DECL_KEYS = frozenset({"type", "required", "description", "max_length", "schema", "path_glob"})
SCHEMA_KEYS = frozenset({"type", "properties", "required", "items", "enum"})
SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
BINDING_PURPOSES = frozenset({"input", "script", "output"})
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$")


class ContractError(Exception):
    def __init__(self, problems: list[str], code: str = "outputs_invalid"):
        super().__init__("; ".join(problems))
        self.problems = problems
        self.code = code


@dataclass(frozen=True)
class OutputSpec:
    name: str
    type: str
    required: bool = True
    description: str | None = None
    max_length: int | None = None
    schema: dict | None = None
    path_glob: str | None = None


def parse_output_contract(raw: str | dict | None) -> dict[str, OutputSpec]:
    """Parse `outputs_json`. Raises `ContractError` when the declaration itself is broken."""
    if raw in (None, "", {}):
        return {}
    data = _json_object(raw, _("Workflow output contract is unreadable"))
    specs: dict[str, OutputSpec] = {}
    problems: list[str] = []
    for name, body in data.items():
        if not isinstance(name, str) or not name:
            problems.append("output name must be a string")
            continue
        spec, issue = _one_spec(name, body)
        if issue:
            problems.append(issue)
        elif spec:
            specs[name] = spec
    if problems:
        raise ContractError(problems, code="invalid_contract")
    return specs


def output_contract_view(raw: str | dict | None) -> list[dict]:
    """Rows for TASK.md: name, type, required, description, max_length, path_glob."""
    try:
        specs = parse_output_contract(raw)
    except ContractError:
        return []
    rows = []
    for spec in specs.values():
        rows.append({
            "name": spec.name, "type": spec.type, "required": spec.required,
            "description": spec.description, "max_length": spec.max_length, "path_glob": spec.path_glob,
        })
    return rows


def validate_outputs(contract: dict[str, OutputSpec], values: dict | None, *,
                     commit: str | None = None,
                     file_exists: Callable[[str, str], bool] | None = None,
                     commit_on_branch: Callable[[str], bool] | None = None,
                     reference_ok: Callable[[str, Any], bool] | None = None) -> dict:
    """Coerce declared outputs. Missing required values and bad types raise `ContractError`."""
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ContractError([_("Outputs must be a JSON object")])
    problems: list[str] = []
    cleaned: dict[str, Any] = {}
    for name in values:
        if name not in contract:
            problems.append(_("Unknown output %(name)s", name=name))
    for spec in contract.values():
        if spec.name not in values or _blank(values.get(spec.name), spec.type):
            if spec.required:
                problems.append(_("%(name)s (%(type)s) is missing", name=spec.name, type=spec.type))
            continue
        try:
            cleaned[spec.name] = _coerce(
                spec, values[spec.name], commit=commit, file_exists=file_exists,
                commit_on_branch=commit_on_branch, reference_ok=reference_ok)
        except ValueError as exc:
            problems.append(_("%(name)s (%(type)s) is invalid: %(detail)s",
                              name=spec.name, type=spec.type, detail=str(exc)))
    if problems:
        raise ContractError(problems)
    return cleaned


def retarget_file_outputs(declared: dict, contract: dict[str, OutputSpec], merge_commit: str,
                           file_exists: Callable[[str, str], bool] | None = None) -> dict:
    """Point file outputs at the merge commit and check the path still exists there."""
    updated = dict(declared)
    for spec in contract.values():
        if spec.type != "file" or spec.name not in updated:
            continue
        value = updated[spec.name]
        if not isinstance(value, dict) or not value.get("path"):
            raise ContractError([_("%(name)s (%(type)s) is invalid: %(detail)s",
                                  name=spec.name, type="file", detail="missing path")])
        retargeted = {"path": value["path"], "commit": merge_commit}
        if file_exists is not None and not file_exists(retargeted["path"], merge_commit):
            raise ContractError([_("%(name)s (file) is missing from the merge commit", name=spec.name)])
        updated[spec.name] = retargeted
    return updated


def build_output_json(*, task_nr: str, status: str, summary: str, branch: str | None,
                      commit: str | None, open_points: list[str] | None, declared: dict,
                      pr: int | None = None, merge_commit: str | None = None,
                      result: Any = None, profile: Any = None, script: dict | None = None) -> dict:
    """Built-ins at the top level, declared values under `output` (§3.7)."""
    data: dict[str, Any] = {
        "task_nr": task_nr, "status": status, "summary": summary, "branch": branch,
        "commit": commit, "open_points": list(open_points or []), "output": declared,
    }
    if pr is not None:
        data["pr"] = pr
    if merge_commit:
        data["merge_commit"] = merge_commit
    if result is not None:
        data["result"] = result
    if profile is not None:
        data["profile"] = profile
    if script is not None:
        data["script"] = script
    return data


def resolve_bindings(inputs: list | str | None, ctx: Context) -> list[dict]:
    """Resolve `inputs_json` to references. `script` bindings also expose path and commit.

    Rows are not written to `task_data_refs` here – that table belongs to data zones.
    The run engine inserts them when a step activates.
    """
    resolved = []
    for row in _binding_rows(inputs):
        placeholders = [part for part in parse_template(str(row["ref"]).strip()) if isinstance(part, Placeholder)]
        if len(placeholders) != 1 or not _only_placeholder(str(row["ref"])):
            raise PlaceholderError("placeholder_unresolved", str(row.get("ref")),
                                   f"{row.get('ref')}: a binding ref must be exactly one placeholder")
        found = _resolve_binding_value(placeholders[0], ctx)
        item = {"purpose": row["purpose"], "ref": str(row["ref"]).strip(), "value": found}
        if row["purpose"] == "script":
            path, commit = _script_pair(found)
            item["path"], item["commit"] = path, commit
        resolved.append(item)
    return resolved


def prepare_submit_outputs(session, task, outputs, *, commit: str) -> dict | None:
    """Validate outputs before a pull request is opened.

    None means the task is not a workflow task and no outputs were sent. An empty dict
    means a workflow task with no declared values. Raises before Gitea is touched.
    """
    from app.errors import Unprocessable
    from app.models import Project, WorkflowRunStep

    if not task.workflow_run_step_id:
        if outputs:
            raise Unprocessable(_("This task has no workflow output contract"), code="outputs_invalid")
        return None
    run_step = session.get(WorkflowRunStep, task.workflow_run_step_id)
    if run_step is None or run_step.step is None:
        raise Unprocessable(_("This task has no workflow output contract"), code="outputs_invalid")
    try:
        contract = parse_output_contract(run_step.step.outputs_json)
    except ContractError as exc:
        raise Unprocessable(str(exc), code="outputs_invalid") from exc
    project = session.get(Project, task.project_id)
    file_exists, on_branch = _repo_checkers(session, project)
    try:
        return validate_outputs(contract, outputs, commit=commit, file_exists=file_exists,
                                commit_on_branch=on_branch)
    except ContractError as exc:
        raise Unprocessable(str(exc), code=exc.code) from exc


def store_submit_outputs(session, task, declared: dict | None, *, open_points: list[str] | None,
                         status: str = "for_review") -> None:
    """Write the snapshot `prepare_submit_outputs` already accepted."""
    from app.models import WorkflowRunStep

    if declared is None or not task.workflow_run_step_id:
        return
    run_step = session.get(WorkflowRunStep, task.workflow_run_step_id)
    if run_step is None or run_step.step is None:
        return
    payload = build_output_json(
        task_nr=task.task_nr, status=status, summary=task.result_summary or "",
        branch=task.branch_name, commit=task.result_commit_sha, open_points=open_points,
        declared=declared, pr=task.pr_number, merge_commit=task.merge_commit_sha)
    # Engine-owned input snapshots and repair counters survive ordinary submissions/resubmissions.
    previous = json.loads(run_step.output_json or "{}")
    for name in ("input_profiles", "input_results", "repair_pin_id", "repair_reason", "failed_checks", "manual_repair",
                 "schema_diff"):
        if name in previous:
            payload[name] = previous[name]
    run_step.output_json = json.dumps(payload, ensure_ascii=False)
    run_step.output_class = run_step.step.data_class


def apply_submit_outputs(session, task, outputs, *, open_points: list[str] | None,
                         status: str = "for_review") -> None:
    """Validate and store. Prefer `prepare` before creating the pull request, then `store`."""
    declared = prepare_submit_outputs(session, task, outputs, commit=task.result_commit_sha or "")
    store_submit_outputs(session, task, declared, open_points=open_points, status=status)


def finalize_approved_outputs(session, task) -> None:
    """After merge: status `done`, `merge_commit`, and file outputs point at the merge commit."""
    from app.errors import Conflict
    from app.models import Project, WorkflowRunStep

    if not task.workflow_run_step_id:
        return
    run_step = session.get(WorkflowRunStep, task.workflow_run_step_id)
    if run_step is None or not run_step.output_json:
        return
    try:
        data = json.loads(run_step.output_json)
        contract = parse_output_contract(run_step.step.outputs_json if run_step.step else None)
    except (ValueError, ContractError) as exc:
        raise Conflict(_("Workflow output contract is unreadable"), code="outputs_invalid") from exc
    if not isinstance(data, dict):
        raise Conflict(_("Workflow output contract is unreadable"), code="outputs_invalid")
    project = session.get(Project, task.project_id)
    file_exists, _on_branch = _repo_checkers(session, project)
    declared = data.get("output") if isinstance(data.get("output"), dict) else {}
    try:
        declared = retarget_file_outputs(declared, contract, task.merge_commit_sha or "", file_exists)
    except ContractError as exc:
        raise Conflict(str(exc), code="outputs_invalid") from exc
    data["output"] = declared
    data["status"] = "done"
    if task.merge_commit_sha:
        data["merge_commit"] = task.merge_commit_sha
    run_step.output_json = json.dumps(data, ensure_ascii=False)


def glob_match(pattern: str, path: str) -> bool:
    """`*` and `?` stay inside one path segment. `**` crosses segments."""
    return re.fullmatch(_glob_regex(pattern), path) is not None


def safe_repo_path(path: str) -> bool:
    if not path or path.startswith(("/", "~")) or "\\" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def schema_problems(schema: Any, value: Any, path: str = "$") -> list[str]:
    """A small JSON Schema: only type, properties, required, items and enum."""
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    extra = set(schema) - SCHEMA_KEYS
    if extra:
        return [f"{path}: unsupported schema keywords {sorted(extra)}"]
    problems: list[str] = []
    if "type" in schema and schema["type"] not in SCHEMA_TYPES:
        problems.append(f"{path}: unsupported schema type {schema['type']!r}")
    if "enum" in schema and not isinstance(schema["enum"], list):
        problems.append(f"{path}: enum must be a list")
    if problems:
        return problems
    expected = schema.get("type")
    if expected and not _schema_type_ok(expected, value):
        problems.append(f"{path}: expected {expected}")
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        problems.append(f"{path}: not in enum")
    if expected == "object" and isinstance(value, dict):
        props = schema.get("properties") or {}
        if "properties" in schema and not isinstance(props, dict):
            return [f"{path}: properties must be an object"]
        required = schema.get("required") or []
        if "required" in schema and not isinstance(required, list):
            return [f"{path}: required must be a list"]
        for key in required:
            if key not in value:
                problems.append(f"{path}.{key}: required")
        if isinstance(props, dict):
            for key, sub in props.items():
                if key in value:
                    problems.extend(schema_problems(sub, value[key], f"{path}.{key}"))
    if expected == "array" and isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            problems.extend(schema_problems(schema["items"], item, f"{path}[{index}]"))
    return problems


def schema_well_formed(schema: Any, path: str = "schema") -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    extra = set(schema) - SCHEMA_KEYS
    if extra:
        return [f"{path}: unsupported schema keywords {sorted(extra)}"]
    problems: list[str] = []
    if "type" in schema and schema["type"] not in SCHEMA_TYPES:
        problems.append(f"{path}: unsupported schema type {schema['type']!r}")
    if "enum" in schema and not isinstance(schema["enum"], list):
        problems.append(f"{path}: enum must be a list")
    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            problems.append(f"{path}: properties must be an object")
        else:
            for key, sub in props.items():
                problems.extend(schema_well_formed(sub, f"{path}.{key}"))
    if "items" in schema:
        problems.extend(schema_well_formed(schema["items"], f"{path}.items"))
    if "required" in schema and (
            not isinstance(schema["required"], list) or not all(isinstance(item, str) for item in schema["required"])):
        problems.append(f"{path}: required must be a list of strings")
    return problems


# --- declaration and coercion -----------------------------------------------------------------------------


def _one_spec(name: str, body: Any) -> tuple[OutputSpec | None, str | None]:
    if not isinstance(body, dict):
        return None, f"{name}: output declaration must be an object"
    extra = set(body) - DECL_KEYS
    if extra:
        return None, f"{name}: unknown output keys {sorted(extra)}"
    kind = body.get("type")
    if kind not in OUTPUT_TYPES:
        return None, f"{name}: unknown output type {kind!r}"
    required = body.get("required", True)
    if not isinstance(required, bool):
        return None, f"{name}: required must be true or false"
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        return None, f"{name}: description must be a string"
    max_length = body.get("max_length")
    if max_length is not None and (kind != "text" or not isinstance(max_length, int) or max_length < 1):
        return None, f"{name}: max_length is only valid as a positive integer on text"
    path_glob = body.get("path_glob")
    if path_glob is not None and (kind != "file" or not isinstance(path_glob, str)):
        return None, f"{name}: path_glob is only valid on file"
    schema = body.get("schema")
    if schema is not None and kind != "json":
        return None, f"{name}: schema is only valid on json"
    if schema is not None:
        broken = schema_well_formed(schema, name)
        if broken:
            return None, broken[0]
    return OutputSpec(name, kind, required, description, max_length, schema, path_glob), None


def _json_object(raw: str | dict, unreadable: str) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError([unreadable], code="invalid_contract") from exc
    if not isinstance(data, dict):
        raise ContractError([unreadable], code="invalid_contract")
    return data


def _blank(value: Any, kind: str) -> bool:
    if value is None:
        return True
    if kind == "text" and isinstance(value, str) and value == "":
        return True
    return False


def _coerce(spec: OutputSpec, value: Any, *, commit, file_exists, commit_on_branch, reference_ok) -> Any:
    kind = spec.type
    if kind == "text":
        return _coerce_text(spec, value)
    if kind == "int":
        return _coerce_int(value)
    if kind == "decimal":
        return _coerce_decimal(value)
    if kind == "bool":
        return _coerce_bool(value)
    if kind == "date":
        return _coerce_date(value)
    if kind == "month":
        return _coerce_month(value)
    if kind == "json":
        return _coerce_json(spec, value)
    if kind == "file":
        return _coerce_file(spec, value, commit, file_exists)
    if kind == "commit":
        return _coerce_commit(value, commit_on_branch)
    if kind in {"dataset", "profile", "result"}:
        return _coerce_reference(kind, value, reference_ok)
    raise ValueError(f"unknown type {kind}")


def _coerce_text(spec: OutputSpec, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected text")
    if spec.max_length is not None and len(value) > spec.max_length:
        raise ValueError(f"longer than {spec.max_length} characters")
    _reject_huge(value)
    return value


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("expected an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise ValueError("expected an integer")


def _coerce_decimal(value: Any) -> str:
    """Stored as a string so trailing precision survives JSON."""
    if isinstance(value, bool):
        raise ValueError("expected a decimal")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = value if isinstance(value, str) else (str(value) if isinstance(value, float) else None)
    if text is None or not DECIMAL_RE.fullmatch(text.strip()):
        raise ValueError("expected a decimal")
    try:
        Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError("expected a decimal") from exc
    _reject_huge(text.strip())
    return text.strip()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    if value in {0, 1} and not isinstance(value, bool):
        return bool(value)
    raise ValueError("expected true or false")


def _coerce_date(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("expected YYYY-MM-DD")
    year, month, day = (int(part) for part in text.split("-"))
    try:
        from datetime import date
        date(year, month, day)
    except ValueError as exc:
        raise ValueError("expected YYYY-MM-DD") from exc
    return text


def _coerce_month(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    match = MONTH_RE.fullmatch(text)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ValueError("expected YYYY-MM")
    return text


def _coerce_json(spec: OutputSpec, value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "[{":
            raise ValueError("expected JSON")
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise ValueError("expected JSON") from exc
    _reject_huge(value)
    if spec.schema:
        problems = schema_problems(spec.schema, value)
        if problems:
            raise ValueError(problems[0])
    return value


def _coerce_file(spec: OutputSpec, value: Any, commit: str | None,
                 file_exists: Callable[[str, str], bool] | None) -> dict:
    if isinstance(value, str):
        body = {"path": value.strip(), "commit": commit}
    elif isinstance(value, dict) and isinstance(value.get("path"), str):
        body = {"path": value["path"].strip(), "commit": value.get("commit") or commit}
    else:
        raise ValueError("expected a repository path")
    if not safe_repo_path(body["path"]):
        raise ValueError("path must stay inside the repository")
    if spec.path_glob and not glob_match(spec.path_glob, body["path"]):
        raise ValueError(f"does not match {spec.path_glob}")
    if not body.get("commit"):
        raise ValueError("commit is missing")
    if file_exists is not None and not file_exists(body["path"], body["commit"]):
        raise ValueError("file not found at the commit")
    return {"path": body["path"], "commit": body["commit"]}


def _coerce_commit(value: Any, commit_on_branch: Callable[[str], bool] | None) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not SHA_RE.fullmatch(text or ""):
        raise ValueError("expected a 40-character SHA")
    if commit_on_branch is not None and not commit_on_branch(text):
        raise ValueError("commit is not on the default branch")
    return text


def _coerce_reference(kind: str, value: Any, reference_ok: Callable[[str, Any], bool] | None) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("expected a reference id")
    if isinstance(value, str) and not value.strip():
        raise ValueError("expected a reference id")
    if isinstance(value, int) and value < 1:
        raise ValueError("expected a reference id")
    ref = value.strip() if isinstance(value, str) else value
    if reference_ok is not None and not reference_ok(kind, ref):
        raise ValueError("reference was not found in this project")
    _reject_huge(ref)
    return ref


def _reject_huge(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_VALUE_BYTES:
        raise ValueError(f"larger than {MAX_VALUE_BYTES // 1024} KiB")


def _schema_type_ok(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _glob_regex(pattern: str) -> str:
    out = ["^"]
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        ch = pattern[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return "".join(out)


def _repo_checkers(session, project):
    from app.errors import Unprocessable
    from app.services import settings_store
    from app.services.gitea import GiteaError

    if project is None or not project.gitea_repo:
        def missing(_path, _commit):
            raise Unprocessable(_("Project has no Gitea repository"), code="no_repo")
        return missing, missing
    client = settings_store.gitea_client(session)
    owner, repo, branch = project.gitea_owner, project.gitea_repo, project.default_branch

    def file_exists(path: str, commit: str) -> bool:
        try:
            return client.file_exists(owner, repo, path, commit)
        except GiteaError as exc:
            raise Unprocessable(f"Gitea: {exc}", code="gitea_error") from exc

    def on_branch(sha: str) -> bool:
        try:
            return client.commit_on_branch(owner, repo, sha, branch)
        except GiteaError as exc:
            raise Unprocessable(f"Gitea: {exc}", code="gitea_error") from exc

    return file_exists, on_branch


def _binding_rows(inputs: list | str | None) -> list[dict]:
    if inputs in (None, "", []):
        return []
    if isinstance(inputs, str):
        try:
            inputs = json.loads(inputs)
        except ValueError as exc:
            raise ContractError([_("Input bindings are unreadable")], code="invalid_contract") from exc
    if not isinstance(inputs, list):
        raise ContractError([_("Input bindings must be a list")], code="invalid_contract")
    rows = []
    for index, row in enumerate(inputs):
        if not isinstance(row, dict) or not isinstance(row.get("ref"), str) or row.get("purpose") not in BINDING_PURPOSES:
            raise ContractError([f"inputs[{index}] needs ref and purpose input|script|output"], code="invalid_contract")
        rows.append({"ref": row["ref"], "purpose": row["purpose"]})
    return rows


def _only_placeholder(text: str) -> bool:
    parts = parse_template(text.strip())
    return len(parts) == 1 and isinstance(parts[0], Placeholder)


def _resolve_binding_value(placeholder: Placeholder, ctx: Context) -> Any:
    """Resolve a binding to the raw value, not the rendered prose."""
    from app.services.workflows.placeholders import _lookup
    value, _masked, _defaulted = _lookup(placeholder, ctx)
    return value


def _script_pair(value: Any) -> tuple[str, str]:
    if isinstance(value, dict) and value.get("path") and value.get("commit"):
        return str(value["path"]), str(value["commit"])
    raise PlaceholderError("placeholder_unresolved", "", "script binding did not resolve to {path, commit}")
