"""Mini placeholder resolver. Regex and path lookup only – no Jinja, no expressions.

Syntax (research/konzept-workflows.md §3.5):

    {{ <path> [| <filter>]* }}
    \\{{  → a literal {{

An empty string is never filled in silently. `default("…")` is the explicit way to
substitute one. Anything the context cannot resolve raises `PlaceholderError` with
code `placeholder_unresolved` and the path. The caller pauses the run (`failure_effect`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SHORT_SHA = 7
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Filters that may follow a resolved value. `default` is always allowed and does not
# change this set: later filters must still match the declared type.
FILTERS_BY_TYPE = {
    "text": {"upper", "lower", "first_line", "truncate", "json"},
    "int": {"json"},
    "decimal": {"json"},
    "bool": {"json"},
    "date": {"date", "json"},
    "datetime": {"date", "json"},
    "month": {"date", "json"},
    "json": {"json"},
    "file": {"upper", "lower", "first_line", "truncate", "json"},
    "commit": {"upper", "lower", "first_line", "truncate", "json"},
    "dataset": {"json"},
    "profile": {"json"},
    "result": {"json"},
}

RUN_FIELDS = {
    "id": "int", "seq": "int", "key": "text", "url": "text",
    "started_at": "datetime", "scheduled_for": "datetime",
    "period": "month", "prev_month": "month", "this_month": "month", "year": "int",
}
BUILTIN_FIELDS = {
    "task_nr": "text", "status": "text", "summary": "text", "branch": "text",
    "pr": "int", "commit": "text", "merge_commit": "text", "open_points": "json",
    "result": "result", "profile": "profile", "script": "file",
}
TASK_BUILTINS = frozenset(BUILTIN_FIELDS) - {"result", "profile", "script"}
# Zone R values may not be pasted into a looser step. These references may, after a gate.
SEALED_REFERENCES = frozenset({"result", "profile", "task_nr", "status"})
STEP_TYPES_WITH_TASK = frozenset({"agent", "script", "runner", "approval"})
FINISHED = frozenset({"succeeded", "done"})


class PlaceholderError(Exception):
    """Activation must stop. `path` is the placeholder path (empty when the template itself is broken)."""

    def __init__(self, code: str, path: str, message: str):
        super().__init__(message)
        self.code = code
        self.path = path


def failure_effect(error: PlaceholderError) -> dict[str, str]:
    """What the run engine records when `render` raises. Does not touch the database."""
    code = error.code or "placeholder_unresolved"
    return {
        "run_step_status": "failed",
        "error_code": code,
        "run_status": "paused",
        "pause_reason": code,
        "error_detail": str(error)[:500],
    }


@dataclass
class Filter:
    name: str
    arg: str | None = None  # default literal, strftime pattern, or truncate length


@dataclass
class Placeholder:
    path: str
    filters: list[Filter]
    raw: str


@dataclass
class PathRef:
    """A parsed path. `kind` is param, run, step_builtin, step_output, workflow, project, event, or invalid."""

    kind: str
    name: str | None = None
    step: str | None = None
    builtin: str | None = None
    output: str | None = None
    fields: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class StepValue:
    """One finished step, shaped like `workflow_run_steps.output_json` plus the step's class."""

    step_type: str
    data_class: str | None = None
    status: str = "succeeded"
    task_nr: str | None = None
    summary: str | None = None
    branch: str | None = None
    pr: int | None = None
    commit: str | None = None
    merge_commit: str | None = None
    open_points: list | None = None
    output: dict | None = None
    result: Any = None
    profile: Any = None
    script: dict | None = None


@dataclass
class Context:
    params: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, StepValue] = field(default_factory=dict)
    run_id: int | None = None
    run_seq: int | None = None
    run_key: str | None = None
    run_url: str | None = None
    started_at: datetime | None = None
    scheduled_for: datetime | None = None
    timezone: str = "UTC"
    workflow_key: str | None = None
    workflow_name: str | None = None
    workflow_version: int | str | None = None
    project_key: str | None = None
    project_name: str | None = None
    event: dict[str, Any] | None = None
    allow_event: bool = False


@dataclass
class Resolution:
    text: str
    trace: list[dict]


def classify_path(path: str) -> PathRef:
    parts = path.split(".")
    if not path or any(not IDENT.fullmatch(part) for part in parts):
        return PathRef("invalid", error="path is not a dotted name")
    head, rest = parts[0], parts[1:]
    if head == "params":
        if len(rest) != 1:
            return PathRef("invalid", error="params.<name> takes one segment")
        return PathRef("param", name=rest[0])
    if head == "run":
        if len(rest) != 1 or rest[0] not in RUN_FIELDS:
            return PathRef("invalid", error="unknown run field")
        return PathRef("run", name=rest[0])
    if head == "workflow":
        if len(rest) != 1 or rest[0] not in {"key", "name", "version"}:
            return PathRef("invalid", error="unknown workflow field")
        return PathRef("workflow", name=rest[0])
    if head == "project":
        if len(rest) != 1 or rest[0] not in {"key", "name"}:
            return PathRef("invalid", error="unknown project field")
        return PathRef("project", name=rest[0])
    if head == "event":
        if not rest:
            return PathRef("invalid", error="event.<path> needs a field")
        return PathRef("event", name=".".join(rest), fields=tuple(rest))
    if head == "steps":
        if len(rest) < 2:
            return PathRef("invalid", error="steps.<key> needs a field")
        step, kind, tail = rest[0], rest[1], rest[2:]
        if kind == "output":
            if not tail:
                return PathRef("invalid", error="steps.<key>.output.<name> needs an output name")
            return PathRef("step_output", step=step, output=tail[0], fields=tuple(tail[1:]))
        if tail or kind not in BUILTIN_FIELDS:
            return PathRef("invalid", error="unknown step field")
        return PathRef("step_builtin", step=step, builtin=kind)
    if rest:
        return PathRef("invalid", error="short form is only a parameter name")
    return PathRef("param", name=head)


def filters_compatible(value_type: str, filters: list[Filter]) -> str | None:
    """None when every non-default filter matches `value_type`, else a short reason."""
    allowed = FILTERS_BY_TYPE.get(value_type)
    if allowed is None:
        return f"no filters are defined for type {value_type}"
    for item in filters:
        if item.name == "default":
            continue
        if item.name not in allowed:
            return f"filter {item.name} does not apply to {value_type}"
    return None


def parse_template(template: str) -> list[str | Placeholder]:
    """Split a template into literals and placeholders. Raises `PlaceholderError` on syntax errors."""
    parts: list[str | Placeholder] = []
    buf: list[str] = []
    i, n = 0, len(template)
    while i < n:
        if template.startswith("\\{{", i):
            buf.append("{{")
            i += 3
            continue
        if template.startswith("{{", i):
            end = _closing_braces(template, i + 2)
            if end < 0:
                raise PlaceholderError("placeholder_syntax", "", "unclosed {{")
            raw = template[i + 2:end]
            parts.append("".join(buf))
            buf = []
            parts.append(_parse_placeholder(raw))
            i = end + 2
            continue
        buf.append(template[i])
        i += 1
    parts.append("".join(buf))
    return [part for part in parts if part != ""]


def render(template: str | None, ctx: Context) -> Resolution:
    """Resolve every placeholder. Missing and empty values raise; they are not inserted."""
    if template is None:
        return Resolution("", [])
    trace: list[dict] = []
    out: list[str] = []
    for part in parse_template(template):
        if isinstance(part, str):
            out.append(part)
            continue
        value, masked, defaulted = _lookup(part, ctx)
        text = _render_value(value, part)
        if text == "" and not _default_is_empty(part):
            raise PlaceholderError("placeholder_unresolved", part.path, f"{part.path}: resolved to an empty string")
        trace.append({"path": part.path, "expression": part.raw.strip(), "value": "***" if masked else value,
                       "masked": masked, "defaulted": defaulted})
        out.append(text)
    return Resolution("".join(out), trace)


def render_file(value: dict) -> str:
    """A file output without a filter renders as `path@` plus the first 7 hex characters of the commit."""
    path = str(value.get("path") or "")
    commit = str(value.get("commit") or "")
    if not path:
        raise PlaceholderError("placeholder_unresolved", "", "file output has no path")
    if not commit:
        return path
    return f"{path}@{commit[:SHORT_SHA]}"


# --- parsing ----------------------------------------------------------------------------------------------


def _closing_braces(template: str, start: int) -> int:
    quote = escape = False
    i = start
    while i < len(template):
        ch = template[i]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                quote = False
            i += 1
            continue
        if ch == '"':
            quote = True
        elif template.startswith("}}", i):
            return i
        i += 1
    return -1


def _parse_placeholder(raw: str) -> Placeholder:
    try:
        path, filters = _split_filters(raw)
    except ValueError as exc:
        raise PlaceholderError("placeholder_syntax", raw.strip(), str(exc)) from exc
    if not path:
        raise PlaceholderError("placeholder_syntax", "", "placeholder has no path")
    parsed: list[Filter] = []
    for piece in filters:
        parsed.append(_parse_filter(piece))
    return Placeholder(path=path, filters=parsed, raw=raw)


def _split_filters(raw: str) -> tuple[str, list[str]]:
    parts: list[str] = []
    buf: list[str] = []
    quote = escape = False
    for ch in raw:
        if quote:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                quote = False
            continue
        if ch == '"':
            quote = True
            buf.append(ch)
            continue
        if ch == "|":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if quote:
        raise ValueError("unclosed quote in placeholder")
    parts.append("".join(buf))
    cleaned = [part.strip() for part in parts]
    if any(part == "" for part in cleaned):
        raise ValueError("empty filter")
    return cleaned[0], cleaned[1:]


def _parse_filter(piece: str) -> Filter:
    name, sep, arg = piece.partition("(")
    name = name.strip()
    if name not in {"default", "json", "upper", "lower", "date", "first_line", "truncate"}:
        raise PlaceholderError("placeholder_syntax", "", f"unknown filter {name!r}")
    if name in {"json", "upper", "lower", "first_line"}:
        if sep or arg:
            raise PlaceholderError("placeholder_syntax", "", f"filter {name} takes no arguments")
        return Filter(name)
    if not sep or not arg.endswith(")"):
        raise PlaceholderError("placeholder_syntax", "", f"filter {name} needs parentheses")
    inner = arg[:-1].strip()
    if name == "truncate":
        if not inner.isdigit() or int(inner) < 1:
            raise PlaceholderError("placeholder_syntax", "", "truncate needs a positive integer")
        return Filter(name, inner)
    if name == "default":
        return Filter(name, _unquote(inner, "default"))
    if not inner:
        raise PlaceholderError("placeholder_syntax", "", "date needs a strftime pattern")
    pattern = _unquote(inner, "date")
    if not pattern or "{{" in pattern or "}}" in pattern:
        raise PlaceholderError("placeholder_syntax", "", "date pattern is empty or contains braces")
    return Filter(name, pattern)


def _unquote(inner: str, label: str) -> str:
    if len(inner) < 2 or inner[0] != '"' or inner[-1] != '"':
        raise PlaceholderError("placeholder_syntax", "", f'{label} argument must be a "quoted" string')
    out: list[str] = []
    escape = False
    for ch in inner[1:-1]:
        if escape:
            out.append(ch)
            escape = False
        elif ch == "\\":
            escape = True
        else:
            out.append(ch)
    if escape:
        raise PlaceholderError("placeholder_syntax", "", f"trailing backslash in {label}")
    return "".join(out)


def _default_is_empty(placeholder: Placeholder) -> bool:
    return any(item.name == "default" and item.arg == "" for item in placeholder.filters)


# --- lookup and rendering ---------------------------------------------------------------------------------


def _lookup(placeholder: Placeholder, ctx: Context) -> tuple[Any, bool, bool]:
    ref = classify_path(placeholder.path)
    if ref.kind == "invalid":
        raise PlaceholderError("placeholder_syntax", placeholder.path, f"{placeholder.path}: {ref.error}")
    found, masked = _raw(ref, ctx)
    default = next((item.arg or "" for item in placeholder.filters if item.name == "default"), None)
    defaulted = False
    if _missing(found):
        if default is None:
            raise PlaceholderError("placeholder_unresolved", placeholder.path,
                                   f"{placeholder.path}: {_why_missing(ref, ctx)}")
        found, defaulted, masked = default, True, False
    value: Any = found
    for item in placeholder.filters:
        if item.name == "default":
            continue
        value = _apply_filter(value, item, ref)
    return value, masked, defaulted


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _why_missing(ref: PathRef, ctx: Context) -> str:
    if ref.kind in {"step_builtin", "step_output"}:
        step = ctx.steps.get(ref.step or "")
        if step is None:
            return "unknown step"
        if step.status not in FINISHED:
            return "step is not finished"
        if ref.kind == "step_output":
            return "output is missing"
        return "built-in output is missing"
    if ref.kind == "param":
        return "unknown parameter"
    return "not resolved"


def _raw(ref: PathRef, ctx: Context) -> tuple[Any, bool]:
    if ref.kind == "param":
        if ref.name not in ctx.params:
            return None, False
        return ctx.params[ref.name], False
    if ref.kind == "run":
        return _run_value(ref.name or "", ctx), False
    if ref.kind == "workflow":
        return {"key": ctx.workflow_key, "name": ctx.workflow_name, "version": ctx.workflow_version}.get(ref.name or ""), False
    if ref.kind == "project":
        return {"key": ctx.project_key, "name": ctx.project_name}.get(ref.name or ""), False
    if ref.kind == "event":
        path = ref.name or ""
        if not ctx.allow_event:
            raise PlaceholderError("placeholder_unresolved", f"event.{path}",
                                   f"event.{path}: only valid in trigger parameters")
        value = ctx.event or {}
        for part in ref.fields:
            if not isinstance(value, dict) or part not in value:
                return None, False
            value = value[part]
        return value, False
    step = ctx.steps.get(ref.step or "")
    if step is None or step.status not in FINISHED:
        return None, False
    masked = step.data_class in {"local_only", "sealed"}
    if ref.kind == "step_builtin":
        return getattr(step, ref.builtin or "", None), masked
    output = step.output or {}
    if ref.output not in output:
        return None, masked
    value = output[ref.output]
    for name in ref.fields:
        if not isinstance(value, dict) or name not in value:
            return None, masked
        value = value[name]
    return value, masked


def _run_value(name: str, ctx: Context) -> Any:
    direct = {"id": ctx.run_id, "seq": ctx.run_seq, "key": ctx.run_key, "url": ctx.run_url,
              "started_at": ctx.started_at, "scheduled_for": ctx.scheduled_for}
    if name in direct:
        return direct[name]
    moment = _in_zone(ctx.scheduled_for or ctx.started_at, ctx.timezone, name)
    if moment is None:
        return None
    if name == "year":
        return moment.year
    if name == "this_month":
        return f"{moment.year:04d}-{moment.month:02d}"
    if name in {"period", "prev_month"}:
        year, month = _shift_month(moment.year, moment.month, -1)
        return f"{year:04d}-{month:02d}"
    return None


def _in_zone(moment: datetime | None, timezone: str, path: str) -> datetime | None:
    if moment is None:
        return None
    try:
        zone = ZoneInfo(timezone or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise PlaceholderError("placeholder_unresolved", f"run.{path}",
                               f"run.{path}: unknown timezone {timezone!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _apply_filter(value: Any, item: Filter, ref: PathRef) -> Any:
    if item.name == "json":
        return json.dumps(value, ensure_ascii=False)
    if item.name == "date":
        return _format_date(value, item.arg or "", ref)
    text = _as_text(value)
    if text is None:
        raise PlaceholderError("placeholder_unresolved", ref_path(ref),
                               f"{ref_path(ref)}: filter {item.name} needs text")
    if item.name == "upper":
        return text.upper()
    if item.name == "lower":
        return text.lower()
    if item.name == "first_line":
        return text.splitlines()[0] if text.splitlines() else ""
    if item.name == "truncate":
        return text[:int(item.arg or "0")]
    raise PlaceholderError("placeholder_syntax", ref_path(ref), f"unknown filter {item.name}")


def ref_path(ref: PathRef) -> str:
    if ref.kind == "step_output":
        tail = ".".join((ref.output or "", *ref.fields)).strip(".")
        return f"steps.{ref.step}.output.{tail}"
    if ref.kind == "step_builtin":
        return f"steps.{ref.step}.{ref.builtin}"
    if ref.kind == "param":
        return ref.name or ""
    return f"{ref.kind}.{ref.name}"


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "path" in value:
        try:
            return render_file(value)
        except PlaceholderError:
            return None
    return None


def _format_date(value: Any, pattern: str, ref: PathRef) -> str:
    parsed = _coerce_date(value)
    if parsed is None:
        raise PlaceholderError("placeholder_unresolved", ref_path(ref),
                               f"{ref_path(ref)}: filter date does not apply")
    try:
        return parsed.strftime(pattern)
    except ValueError as exc:
        raise PlaceholderError("placeholder_syntax", ref_path(ref),
                               f"{ref_path(ref)}: invalid date pattern") from exc


def _coerce_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        year, month = int(text[:4]), int(text[5:7])
        if 1 <= month <= 12:
            return datetime(year, month, 1)
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _render_value(value: Any, placeholder: Placeholder) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict) and "path" in value and set(value) <= {"path", "commit"}:
        return render_file(value)
    if value is None:
        raise PlaceholderError("placeholder_unresolved", placeholder.path, f"{placeholder.path}: not resolved")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
