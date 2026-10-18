"""Static checks run before a workflow version is published (research/konzept-workflows.md §3.6).

The run engine and the publish API call `check_publish` with a definition dict, or
`definition_from_version` when the rows already exist. Nothing is written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from app.services.workflows.contracts import (
    BINDING_PURPOSES, DECIMAL_RE, MONTH_RE, ContractError, parse_output_contract,
)
from app.services.workflows.placeholders import (
    BUILTIN_FIELDS, IDENT, RUN_FIELDS, SEALED_REFERENCES, STEP_TYPES_WITH_TASK,
    TASK_BUILTINS, PathRef, Placeholder, PlaceholderError, classify_path, filters_compatible,
    parse_template,
)

STEP_TYPES = frozenset({"agent", "script", "runner", "approval", "gate", "notify"})
REVIEW_MODES = frozenset({"manual", "agent", "auto"})
NEEDS_ASSIGNEE = frozenset({"agent", "script", "approval", "runner"})
DATA_ZONE_STEPS = frozenset({"runner", "gate", "script"})
PARALLEL_TYPES = frozenset({"agent", "notify"})
CONDITION_TYPES = frozenset({"agent", "notify", "script", "runner"})
CLASS_RANK = {"sealed": 0, "local_only": 1, "cloud_ok": 2}
PARAM_TYPE_MAP = {
    "string": "text", "text": "text", "enum": "text", "task": "text",
    "int": "int", "decimal": "decimal", "bool": "bool", "date": "date", "month": "month",
    "dataset": "dataset",
}
SCHEMA_TYPE_MAP = {
    "string": "text", "integer": "int", "number": "decimal", "boolean": "bool",
    "object": "json", "array": "json", "null": "json",
}


@dataclass(frozen=True)
class PublishIssue:
    code: str
    message: str
    severity: str = "error"  # warning for role_unresolvable
    step_key: str | None = None
    path: str | None = None


def check_publish(definition: dict, *, role_candidates: dict[str, int] | None = None,
                  data_enabled: bool = False) -> list[PublishIssue]:
    """Return every problem. Warnings use severity `warning` and do not block on their own."""
    issues: list[PublishIssue] = []
    params = _params(definition.get("params") or [], issues)
    steps = _steps(definition, issues)
    allow_auto = definition.get("allow_auto_review", True)
    reads: list[tuple[dict, dict, PathRef]] = []
    for param in params.values():
        _check_default(param, params, steps, issues)
    for step in steps:
        _check_step_rules(step, steps, allow_auto, data_enabled, role_candidates, issues)
        for location, text in _texts(step):
            _walk(text, step, steps, params, issues, reads, allow_event=False, allow_steps=True,
                  location=location)
    for index, trigger in enumerate(definition.get("triggers") or []):
        template = trigger.get("template") if isinstance(trigger, dict) else None
        if isinstance(template, str):
            _walk(template, None, steps, params, issues, reads, allow_event=True, allow_steps=False,
                  location=f"trigger[{index}]")
    _check_conditions(steps, issues)
    _zone_checks(steps, reads, issues)
    return issues


def definition_from_version(version, template, project=None) -> dict:
    """Build the dict `check_publish` expects from stage-1 rows."""
    return {
        "key": template.workflow_key,
        "name": template.name,
        "version": version.version,
        "project_data_class": getattr(project, "privacy", None),
        "params": [_param_dict(param) for param in version.params],
        "steps": [_step_dict(step) for step in version.steps],
    }


def blocking(issues: list[PublishIssue]) -> list[PublishIssue]:
    return [issue for issue in issues if issue.severity != "warning"]


# --- definition -------------------------------------------------------------------------------------------


def _params(raw, issues: list[PublishIssue]) -> dict[str, dict]:
    params: dict[str, dict] = {}
    for index, param in enumerate(raw):
        if not isinstance(param, dict) or not param.get("name"):
            issues.append(PublishIssue("invalid_contract", f"params[{index}] needs a name"))
            continue
        name = str(param["name"])
        if not IDENT.fullmatch(name):
            issues.append(PublishIssue("invalid_contract", f"parameter {name} cannot be used in placeholders"))
        if name in params:
            issues.append(PublishIssue("invalid_contract", f"duplicate parameter {name}"))
        kind = param.get("type") or param.get("param_type")
        if kind not in PARAM_TYPE_MAP:
            issues.append(PublishIssue("param_default_invalid", f"parameter {name} has unknown type {kind!r}",
                                       path=name))
            kind = "string"
        values = param.get("values")
        if values is None and param.get("enum_values"):
            values = param.get("enum_values")
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except ValueError:
                issues.append(PublishIssue("invalid_contract", f"parameter {name} has unreadable enum values"))
                values = []
        params[name] = {
            "name": name, "type": kind, "default": param.get("default", param.get("default_expr")),
            "values": values or [],
        }
    return params


def _steps(definition: dict, issues: list[PublishIssue]) -> list[dict]:
    project_class = definition.get("project_data_class")
    steps: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(definition.get("steps") or []):
        if not isinstance(raw, dict):
            issues.append(PublishIssue("invalid_contract", f"steps[{index}] must be an object"))
            continue
        key = str(raw.get("key") or raw.get("step_key") or "")
        if not IDENT.fullmatch(key):
            issues.append(PublishIssue("invalid_contract", f"step key {key!r} cannot be used in placeholders"))
        if key in seen:
            issues.append(PublishIssue("invalid_contract", f"duplicate step {key}"))
        seen.add(key)
        kind = raw.get("type") or raw.get("step_type")
        if kind not in STEP_TYPES:
            issues.append(PublishIssue("invalid_contract", f"step {key} has unknown type {kind!r}", step_key=key))
        review = raw.get("review") or raw.get("review_mode") or "manual"
        if review not in REVIEW_MODES:
            issues.append(PublishIssue("invalid_contract", f"step {key} has unknown review mode {review!r}",
                                       step_key=key))
        data_class = raw.get("data_class") or project_class
        if data_class is not None and data_class not in CLASS_RANK:
            issues.append(PublishIssue("invalid_contract", f"step {key} has unknown data class {data_class!r}",
                                       step_key=key))
            data_class = None
        try:
            outputs = parse_output_contract(raw.get("outputs") if "outputs" in raw else raw.get("outputs_json"))
        except ContractError as exc:
            issues.append(PublishIssue("invalid_contract", str(exc), step_key=key))
            outputs = {}
        steps.append({
            "key": key,
            "position": int(raw.get("position") or index + 1),
            "type": kind,
            "title": raw.get("title") or raw.get("title_tpl") or "",
            "description": raw.get("description") if "description" in raw else raw.get("description_tpl"),
            "acceptance": raw.get("acceptance") if "acceptance" in raw else raw.get("acceptance_tpl"),
            "assignee_id": raw.get("assignee_id"),
            "role": raw.get("role") or raw.get("agent_role"),
            "reviewer": raw.get("reviewer", raw.get("reviewer_id")),
            "review": review,
            "data_class": data_class,
            "outputs": outputs,
            "inputs": raw.get("inputs") if "inputs" in raw else raw.get("inputs_json"),
            "config": raw.get("config") if "config" in raw else raw.get("config_json"),
            "optional": raw.get("optional", False),
            "parallel_group": raw.get("parallel_group") or None,
            "condition_tpl": raw.get("condition_tpl") or None,
        })
    return steps


def _param_dict(param) -> dict:
    values = None
    if param.enum_values:
        try:
            values = json.loads(param.enum_values)
        except ValueError:
            values = param.enum_values
    return {"name": param.name, "type": param.param_type, "required": param.required,
            "default": param.default_expr, "values": values, "label": param.label}


def _step_dict(step) -> dict:
    return {
        "key": step.step_key, "position": step.position, "type": step.step_type,
        "title": step.title_tpl, "description": step.description_tpl, "acceptance": step.acceptance_tpl,
        "assignee_id": step.assignee_id, "role": step.agent_role, "reviewer": step.reviewer_id,
        "review": step.review_mode, "data_class": step.data_class,
        "timeout_minutes": step.timeout_minutes, "wait_reminder_hours": step.wait_reminder_hours,
        "max_retries": step.max_retries, "retry_backoff_s": step.retry_backoff_s,
        "optional": step.optional, "parallel_group": step.parallel_group, "condition_tpl": step.condition_tpl,
        "outputs": _load(step.outputs_json), "inputs": _load(step.inputs_json), "config": _load(step.config_json),
    }


def _load(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# --- step rules -------------------------------------------------------------------------------------------


def _check_step_rules(step, steps, allow_auto, data_enabled, role_candidates, issues: list[PublishIssue]) -> None:
    key = step["key"]
    if step["type"] == "script":
        from app.services.runner_jobs import _script_path
        from app.errors import Unprocessable

        cfg = step.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = {}
        valid = isinstance(cfg, dict)
        cfg = cfg if valid else {}
        source = next((s for s in steps if s["key"] == cfg.get("profile_from")
                       and s["position"] < step["position"] and s["type"] == "runner"), None)
        source_cfg = source.get("config") or {} if source else {}
        if isinstance(source_cfg, str):
            try:
                source_cfg = json.loads(source_cfg)
            except ValueError:
                source_cfg = {}
        try:
            if not isinstance(cfg.get("path"), str):
                raise Unprocessable("Invalid path")
            _script_path(cfg.get("path"))
        except Unprocessable:
            valid = False
        limit = cfg.get("max_repairs_per_run", 1)
        if (not valid or source is None or not isinstance(source_cfg, dict)
                or source_cfg.get("task_kind") != "profile" or step["data_class"] != "cloud_ok"
                or step["review"] != "manual" or step["optional"]
                or isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 254):
            issues.append(PublishIssue("workflow_script_invalid",
                f"{key}: script needs a safe path, preceding profile_from, cloud_ok, mandatory manual review "
                "and max_repairs_per_run (0..254)", step_key=key))
    if step["type"] == "runner" and step["data_class"] != "sealed":
        issues.append(PublishIssue("workflow_runner_invalid", f"{key}: runner steps must be sealed", step_key=key))
    if step["type"] == "gate":
        previous = next((s for s in steps if s["position"] == step["position"] - 1), None)
        if (not previous or previous["type"] != "runner" or step["review"] != "manual"
                or step.get("optional")):
            issues.append(PublishIssue("workflow_gate_invalid",
                                       f"{key}: a mandatory manual gate must directly follow a runner", step_key=key))
    if step["type"] in NEEDS_ASSIGNEE and step["assignee_id"] and step["role"]:
        issues.append(PublishIssue("no_assignee_rule", f"{key}: choose a fixed assignee or a role, not both",
                                   step_key=key))
    if step["type"] in NEEDS_ASSIGNEE and not step["assignee_id"] and not step["role"]:
        # Approval in the content-pipeline example names the project owner as reviewer, not as assignee.
        if not (step["type"] == "approval" and step["reviewer"]):
            issues.append(PublishIssue(
                "no_assignee_rule", f"{key}: agent, script, runner and approval steps need an assignee or a role",
                step_key=key))
    if step["role"] and role_candidates is not None and role_candidates.get(step["role"], 0) < 1:
        issues.append(PublishIssue(
            "role_unresolvable", f"{key}: role {step['role']} has no candidate",
            severity="warning", step_key=key))
    if step["type"] in DATA_ZONE_STEPS and not data_enabled:
        issues.append(PublishIssue(
            "runner_without_data_zones",
            f"{key}: {step['type']} requires data zones to be enabled", step_key=key))
    if step["review"] == "auto":
        reasons = []
        if allow_auto is False:
            reasons.append("this template does not allow auto review")
        if step["type"] == "script":
            reasons.append("script steps are reviewed manually")
        if _output_feeds_runner(step, steps):
            reasons.append("a later runner executes this output")
        if _later_looser(step, steps):
            reasons.append("a later step has a looser data class")
        if reasons:
            issues.append(PublishIssue("auto_review_not_allowed", f"{key}: " + "; ".join(reasons), step_key=key))
    _check_inputs_shape(step, issues)


def _output_feeds_runner(step, steps) -> bool:
    for other in steps:
        if other["position"] <= step["position"] or other["type"] != "runner":
            continue
        for _location, text in _texts(other):
            if _mentions_step(text, step["key"]):
                return True
    return False


def _check_conditions(steps: list[dict], issues: list[PublishIssue]) -> None:
    """`when:` may only skip non-decision steps; parallel groups must be consecutive agent/notify runs."""
    for step in steps:
        if step.get("condition_tpl") and step["type"] not in CONDITION_TYPES:
            issues.append(PublishIssue(
                "workflow_condition_invalid",
                f"{step['key']}: `when:` is only allowed on agent, notify, script and runner steps",
                step_key=step["key"]))
    ordered = sorted(steps, key=lambda s: s["position"])
    i = 0
    while i < len(ordered):
        name = ordered[i].get("parallel_group")
        if not name:
            i += 1
            continue
        members = [ordered[i]]
        j = i + 1
        while j < len(ordered) and ordered[j].get("parallel_group") == name:
            members.append(ordered[j])
            j += 1
        if len(members) < 2:
            issues.append(PublishIssue(
                "workflow_parallel_invalid",
                f"{members[0]['key']}: a parallel group needs at least two consecutive steps",
                step_key=members[0]["key"]))
        for member in members:
            if member["type"] not in PARALLEL_TYPES:
                issues.append(PublishIssue(
                    "workflow_parallel_invalid",
                    f"{member['key']}: only agent and notify steps may run in parallel",
                    step_key=member["key"]))
        i = j


def _later_looser(step, steps) -> bool:
    rank = CLASS_RANK.get(step["data_class"])
    if rank is None:
        return False
    return any(CLASS_RANK.get(other["data_class"], -1) > rank and other["position"] > step["position"]
               for other in steps)


def _check_inputs_shape(step, issues: list[PublishIssue]) -> None:
    raw = step["inputs"]
    if raw in (None, "", []):
        return
    rows = raw
    if isinstance(raw, str):
        try:
            rows = json.loads(raw)
        except ValueError:
            issues.append(PublishIssue("invalid_contract", f"{step['key']}: inputs are not valid JSON",
                                       step_key=step["key"]))
            return
    if not isinstance(rows, list):
        issues.append(PublishIssue("invalid_contract", f"{step['key']}: inputs must be a list", step_key=step["key"]))
        return
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("ref"), str) or row.get("purpose") not in BINDING_PURPOSES:
            issues.append(PublishIssue(
                "invalid_contract", f"{step['key']}: inputs[{index}] needs ref and purpose input|script|output",
                step_key=step["key"]))
            continue
        ref = row["ref"].strip()
        try:
            parts = parse_template(ref)
        except PlaceholderError:
            continue  # The text walk reports the syntax error once.
        if len(parts) != 1 or not isinstance(parts[0], Placeholder):
            issues.append(PublishIssue(
                "unknown_placeholder", f"{step['key']}: binding ref must be exactly one placeholder",
                step_key=step["key"], path=ref))


def _mentions_step(text: str, key: str) -> bool:
    try:
        parts = parse_template(text or "")
    except PlaceholderError:
        return False
    return any(isinstance(part, Placeholder) and classify_path(part.path).step == key for part in parts)


# --- placeholders -----------------------------------------------------------------------------------------


def _texts(step: dict) -> list[tuple[str, str]]:
    found = [("title", step["title"] or ""), ("description", step["description"] or ""),
             ("acceptance", step["acceptance"] or ""), ("condition", step.get("condition_tpl") or "")]
    found.extend(_strings(step.get("config"), "config"))
    for index, row in enumerate(step.get("inputs") or [] if isinstance(step.get("inputs"), list) else []):
        if isinstance(row, dict) and isinstance(row.get("ref"), str):
            found.append((f"inputs[{index}]", row["ref"]))
    if isinstance(step.get("inputs"), str):
        found.append(("inputs", step["inputs"]))
    return [(where, text) for where, text in found if text]


def _strings(value, prefix: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            found.extend(_strings(item, f"{prefix}.{key}"))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_strings(item, f"{prefix}[{index}]"))
        return found
    return []


def _walk(text: str, step: dict | None, steps: list[dict], params: dict[str, dict],
          issues: list[PublishIssue], reads: list, *, allow_event: bool, allow_steps: bool,
          location: str) -> None:
    key = step["key"] if step else None
    try:
        parts = parse_template(text)
    except PlaceholderError as exc:
        issues.append(PublishIssue("placeholder_syntax", f"{location}: {exc}", step_key=key))
        return
    by_key = {item["key"]: item for item in steps}
    for part in parts:
        if not isinstance(part, Placeholder):
            continue
        ref = classify_path(part.path)
        if ref.kind == "invalid":
            issues.append(PublishIssue("unknown_placeholder", f"{part.path}: {ref.error}",
                                       step_key=key, path=part.path))
            continue
        if ref.kind == "event" and not allow_event:
            issues.append(PublishIssue(
                "unknown_placeholder", f"{part.path}: event placeholders are only valid in trigger parameters",
                step_key=key, path=part.path))
            continue
        value_type = _value_type(ref, part, step, by_key, params, issues, allow_steps)
        if value_type is None:
            continue
        if ref.kind in {"step_builtin", "step_output"} and step is not None:
            reads.append((step, by_key[ref.step], ref))
        mismatch = filters_compatible(value_type, part.filters)
        if mismatch:
            issues.append(PublishIssue("filter_mismatch", f"{part.path}: {mismatch}", step_key=key, path=part.path))


def _value_type(ref: PathRef, part: Placeholder, step, by_key, params, issues, allow_steps) -> str | None:
    key = step["key"] if step else None
    if ref.kind == "param":
        param = params.get(ref.name or "")
        if param is None:
            issues.append(PublishIssue("unknown_placeholder", f"{part.path}: unknown parameter",
                                       step_key=key, path=part.path))
            return None
        return PARAM_TYPE_MAP[param["type"]]
    if ref.kind == "run":
        return RUN_FIELDS[ref.name or ""]
    if ref.kind == "workflow":
        return "int" if ref.name == "version" else "text"
    if ref.kind in {"project", "event"}:
        return "text"
    if not allow_steps:
        issues.append(PublishIssue("unknown_placeholder", f"{part.path}: this template cannot reference steps",
                                   step_key=key, path=part.path))
        return None
    target = by_key.get(ref.step or "")
    if target is None or (step is not None and target["position"] >= step["position"]):
        issues.append(PublishIssue("unknown_placeholder", f"{part.path}: unknown step or not an earlier step",
                                   step_key=key, path=part.path))
        return None
    if ref.kind == "step_builtin":
        return _builtin_type(target, ref, part.path, key, issues)
    return _output_type(target, ref, part.path, key, issues)


def _builtin_type(target, ref: PathRef, path: str, key, issues) -> str | None:
    name = ref.builtin or ""
    if name in TASK_BUILTINS and target["type"] not in STEP_TYPES_WITH_TASK:
        issues.append(PublishIssue("unknown_placeholder", f"{path}: {target['key']} has no task",
                                   step_key=key, path=path))
        return None
    if name == "result" and target["type"] != "runner":
        issues.append(PublishIssue("unknown_placeholder", f"{path}: result is only built in on runner steps",
                                   step_key=key, path=path))
        return None
    if name == "profile" and target["type"] != "runner":
        issues.append(PublishIssue("unknown_placeholder", f"{path}: profile is only built in on runner steps",
                                   step_key=key, path=path))
        return None
    if name == "script" and target["type"] != "script":
        issues.append(PublishIssue("unknown_placeholder", f"{path}: script is only built in on script steps",
                                   step_key=key, path=path))
        return None
    return BUILTIN_FIELDS[name]


def _output_type(target, ref: PathRef, path: str, key, issues) -> str | None:
    spec = target["outputs"].get(ref.output or "")
    if spec is None:
        issues.append(PublishIssue(
            "undeclared_output", f"{path}: {target['key']} does not declare output {ref.output}",
            step_key=key, path=path))
        return None
    if not ref.fields:
        return spec.type
    if spec.type == "file":
        if ref.fields in {("path",), ("commit",)}:
            return "text"
        issues.append(PublishIssue("unknown_placeholder", f"{path}: file outputs only have path and commit",
                                   step_key=key, path=path))
        return None
    if spec.type != "json":
        issues.append(PublishIssue("unknown_placeholder", f"{path}: cannot read a field of {spec.type}",
                                   step_key=key, path=path))
        return None
    return _json_field_type(spec, ref.fields, path, key, issues)


def _json_field_type(spec, fields: tuple[str, ...], path: str, key, issues) -> str | None:
    schema = spec.schema
    if not schema:
        return "text"  # No schema: the field is accepted and filters treat it as text.
    current = schema
    value_type = "json"
    for name in fields:
        if not isinstance(current, dict) or current.get("type") not in {None, "object"}:
            issues.append(PublishIssue("unknown_placeholder", f"{path}: {name} is not an object field",
                                       step_key=key, path=path))
            return None
        props = current.get("properties") or {}
        if name not in props:
            issues.append(PublishIssue("unknown_placeholder", f"{path}: {name} is not in the schema",
                                       step_key=key, path=path))
            return None
        current = props[name]
        value_type = SCHEMA_TYPE_MAP.get(current.get("type") if isinstance(current, dict) else None, "json")
    return value_type


# --- zones ------------------------------------------------------------------------------------------------


def _zone_checks(steps: list[dict], reads: list[tuple[dict, dict, PathRef]], issues: list[PublishIssue]) -> None:
    ordered = sorted(steps, key=lambda item: item["position"])
    index = {step["key"]: pos for pos, step in enumerate(ordered)}
    for consumer, source, ref in reads:
        source_rank = CLASS_RANK.get(source["data_class"])
        consumer_rank = CLASS_RANK.get(consumer["data_class"])
        if source_rank is None or consumer_rank is None or consumer_rank <= source_rank:
            continue
        path = _path_of(ref)
        if source["data_class"] == "sealed":
            # Metadata references never carry data. Profiles use their own human release, not the output gate.
            if ref.kind == "step_builtin" and ref.builtin in {"task_nr", "status", "profile"}:
                continue
            if _sealed_value(ref):
                issues.append(PublishIssue(
                    "zone_boundary_without_gate",
                    f"{path}: sealed values cannot be inserted into a looser step; only result, profile, "
                    "task_nr and status are allowed, and only after a gate",
                    step_key=consumer["key"], path=path))
                continue
            if not _gate_between(ordered, index, source, consumer):
                issues.append(PublishIssue(
                    "zone_boundary_without_gate",
                    f"{path}: reading sealed output in a looser step needs a gate directly after the runner",
                    step_key=consumer["key"], path=path))
        elif source["data_class"] == "local_only" and consumer["data_class"] == "cloud_ok":
            if not _approval_between(ordered, source, consumer):
                issues.append(PublishIssue(
                    "zone_boundary_without_gate",
                    f"{path}: moving local_only output into cloud_ok needs a manual approval step in between",
                    step_key=consumer["key"], path=path))


def _sealed_value(ref: PathRef) -> bool:
    if ref.kind == "step_output":
        return True
    return ref.kind == "step_builtin" and ref.builtin not in SEALED_REFERENCES


def _gate_between(ordered, index, source, consumer) -> bool:
    pos = index.get(source["key"])
    if pos is None:
        return False
    nxt = ordered[pos + 1] if pos + 1 < len(ordered) else None
    if source["type"] == "runner":
        if not (nxt and nxt["type"] == "gate"):
            return False
        return nxt["key"] == consumer["key"] or nxt["position"] < consumer["position"]
    return any(step["type"] == "gate" and source["position"] < step["position"] < consumer["position"]
               for step in ordered)


def _approval_between(ordered, source, consumer) -> bool:
    if (consumer["type"] == "approval" and consumer["review"] == "manual"
            and consumer["position"] > source["position"]):
        return True
    return any(step["type"] == "approval" and step["review"] == "manual"
               and source["position"] < step["position"] < consumer["position"] for step in ordered)


def _path_of(ref: PathRef) -> str:
    if ref.kind == "step_output":
        tail = ".".join((ref.output or "", *ref.fields)).strip(".")
        return f"steps.{ref.step}.output.{tail}"
    return f"steps.{ref.step}.{ref.builtin}"


# --- parameter defaults -----------------------------------------------------------------------------------


def _check_default(param: dict, params, steps, issues: list[PublishIssue]) -> None:
    default = param.get("default")
    if default is None or (isinstance(default, str) and default.strip() == ""):
        return
    text = str(default).strip()
    try:
        parts = parse_template(text)
    except PlaceholderError as exc:
        issues.append(PublishIssue("param_default_invalid", f"{param['name']}: {exc}", path=param["name"]))
        return
    placeholders = [part for part in parts if isinstance(part, Placeholder)]
    if placeholders and not (len(parts) == 1 and isinstance(parts[0], Placeholder)):
        if param["type"] not in {"string", "text"}:
            issues.append(PublishIssue(
                "param_default_invalid", f"{param['name']}: a mixed default is only valid for text",
                path=param["name"]))
        _walk(text, None, steps, params, issues, [], allow_event=False, allow_steps=False, location=param["name"])
        return
    if len(placeholders) == 1:
        ref = classify_path(placeholders[0].path)
        value_type = _default_placeholder_type(ref, placeholders[0], param, params, issues)
        if value_type and not _type_accepts(param["type"], value_type):
            issues.append(PublishIssue(
                "param_default_invalid",
                f"{param['name']}: default type {value_type} does not match {param['type']}",
                path=param["name"]))
        mismatch = filters_compatible(value_type, placeholders[0].filters) if value_type else None
        if mismatch:
            issues.append(PublishIssue("filter_mismatch", f"{param['name']}: {mismatch}", path=param["name"]))
        return
    if not _literal_ok(param, text):
        issues.append(PublishIssue(
            "param_default_invalid", f"{param['name']}: default {text!r} does not match type {param['type']}",
            path=param["name"]))


def _default_placeholder_type(ref: PathRef, placeholder: Placeholder, param, params, issues) -> str | None:
    if ref.kind == "invalid":
        issues.append(PublishIssue("unknown_placeholder", f"{placeholder.path}: {ref.error}", path=param["name"]))
        return None
    if ref.kind in {"step_builtin", "step_output", "event"}:
        issues.append(PublishIssue(
            "unknown_placeholder", f"{placeholder.path}: parameter defaults cannot reference steps or events",
            path=param["name"]))
        return None
    if ref.kind == "param":
        issues.append(PublishIssue(
            "unknown_placeholder", f"{placeholder.path}: parameter defaults cannot reference other parameters",
            path=param["name"]))
        return None
    if ref.kind == "run":
        return RUN_FIELDS.get(ref.name or "")
    if ref.kind == "workflow":
        return "int" if ref.name == "version" else "text"
    if ref.kind == "project":
        return "text"
    return None


def _type_accepts(param_type: str, value_type: str) -> bool:
    if param_type in {"string", "text"}:
        return True
    if param_type == "decimal":
        return value_type in {"decimal", "int"}
    if param_type == "date":
        return value_type in {"date", "datetime"}
    if param_type == "task":
        return value_type in {"text", "task"}
    return param_type == value_type


def _literal_ok(param: dict, text: str) -> bool:
    kind = param["type"]
    if kind in {"string", "text", "dataset", "task"}:
        return True
    if kind == "int":
        return re.fullmatch(r"-?\d+", text) is not None
    if kind == "decimal":
        return DECIMAL_RE.fullmatch(text) is not None
    if kind == "bool":
        return text.lower() in {"true", "false"}
    if kind == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return False
        year, month, day = (int(part) for part in text.split("-"))
        try:
            date(year, month, day)
        except ValueError:
            return False
        return True
    if kind == "month":
        match = MONTH_RE.fullmatch(text)
        return bool(match and 1 <= int(match.group(2)) <= 12)
    if kind == "enum":
        return text in {str(value) for value in param.get("values") or []}
    return False
