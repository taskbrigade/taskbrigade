"""Workflow outputs on the agent side: `tbr submit --output`, `.tm-submit.md`, TASK.md.

The server validates the contract. This module only parses what the agent wrote.
"""

import json
import re
from pathlib import Path

from cli.i18n import _

OUTPUTS_HEADING = re.compile(r"^#{1,6}\s*(outputs|ausgaben)\s*:?\s*$", re.IGNORECASE)
OPEN_POINTS_HEADING = re.compile(r"^#{1,6}\s*(open points|offene punkte)\s*:?\s*$", re.IGNORECASE)
FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)


def parse_output_args(pairs: list[str] | None, json_file: Path | None) -> dict | None:
    """Merge `--output-json` with repeatable `--output NAME=VALUE`. `@file` reads a file.

    `--output` wins on duplicate names. None when the agent passed neither option.
    """
    if not pairs and json_file is None:
        return None
    merged: dict = {}
    if json_file is not None:
        try:
            loaded = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(_("--output-json %(path)s is not valid JSON", path=str(json_file))) from exc
        if not isinstance(loaded, dict):
            raise ValueError(_("--output-json must contain a JSON object"))
        merged.update(loaded)
    for raw in pairs or []:
        name, sep, value = raw.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ValueError(_("--output %(item)s: expected NAME=VALUE", item=raw))
        merged[name] = _output_value(value)
    return merged


def split_submit_sections(summary: str) -> tuple[str, list[str], dict | None]:
    """Split `## Open points` and `## Outputs` off a `.tm-submit.md` body.

    Return `(summary, points, outputs)`. `outputs` is None when that section is absent.
    Raises ValueError when the Outputs section is not a JSON object.
    """
    lines = summary.splitlines()
    kept: list[str] = []
    points: list[str] = []
    outputs: dict | None = None
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if OUTPUTS_HEADING.match(stripped):
            block, index = _section(lines, index + 1)
            outputs = _parse_outputs_block(block)
            continue
        if OPEN_POINTS_HEADING.match(stripped):
            block, index = _section(lines, index + 1)
            points.extend(_points(block))
            continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept).strip(), points, outputs


def submitted_outputs_lines(task: dict) -> list[str]:
    """Show the outputs stored at submit, so a reviewer can read them in TASK.md."""
    outputs = task.get("workflow_outputs")
    if not isinstance(outputs, dict) or not outputs:
        return []
    return ["", _("## Outputs"), "", "```json", json.dumps(outputs, ensure_ascii=False, indent=2), "```"]


def required_outputs_lines(task: dict) -> list[str]:
    """Markdown section for TASK.md. Empty when this task declares no outputs."""
    rows = task.get("required_outputs") or []
    if not rows:
        return []
    lines = ["", _("## Required outputs"), "",
             _("This workflow step declares outputs. `tbr submit` rejects the task when a required output is "
               "missing or invalid (`outputs_invalid`)."), ""]
    for row in rows:
        need = _("required") if row.get("required", True) else _("optional")
        detail = _output_detail(row)
        lines.append(_("- `%(name)s` (%(type)s, %(need)s)%(detail)s",
                       name=row["name"], type=row["type"], need=need, detail=detail))
    lines += ["", _("In `.tm-submit.md`, put the same object in a ```json block under `## Outputs`."), "",
              _("Example:"), "", "```", _example_command(task.get("task_nr") or "TASK", rows), "```"]
    return lines


def _output_value(value: str):
    if value.startswith("@@"):
        return value[1:]
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.is_file():
            raise ValueError(_("Output file %(path)s was not found", path=str(path)))
        text = path.read_text(encoding="utf-8")
        return text[:-1] if text.endswith("\n") else text
    return value


def _section(lines: list[str], start: int) -> tuple[list[str], int]:
    block = []
    index = start
    while index < len(lines):
        if OUTPUTS_HEADING.match(lines[index].strip()) or OPEN_POINTS_HEADING.match(lines[index].strip()):
            break
        block.append(lines[index])
        index += 1
    return block, index


def _points(lines: list[str]) -> list[str]:
    points: list[str] = []
    for item in lines:
        match = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*)$", item)
        if match and match.group(1).strip():
            points.append(match.group(1).strip())
        elif item.strip() and points and item.startswith((" ", "\t")):
            points[-1] += " " + item.strip()
    return points


def _parse_outputs_block(lines: list[str]) -> dict:
    text = "\n".join(lines).strip()
    fence = FENCE.search(text)
    raw = fence.group(1).strip() if fence else text
    if not raw:
        raise ValueError(_("Outputs section in .tm-submit.md is empty"))
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(_("Outputs section in .tm-submit.md is not valid JSON")) from exc
    if not isinstance(value, dict):
        raise ValueError(_("Outputs section in .tm-submit.md must be a JSON object"))
    return value


def _output_detail(row: dict) -> str:
    bits = []
    if row.get("description"):
        bits.append(str(row["description"]))
    if row.get("max_length"):
        bits.append(_("max %(num)s characters", num=row["max_length"]))
    if row.get("path_glob"):
        bits.append(_("path %(glob)s", glob=row["path_glob"]))
    return " – " + "; ".join(bits) if bits else ""


def _example_command(task_nr: str, rows: list[dict]) -> str:
    parts = [f"tbr submit {task_nr} --summary \"…\""]
    json_names = []
    for row in rows:
        kind = row["type"]
        name = row["name"]
        if kind == "json":
            json_names.append(name)
        elif kind == "file":
            parts.append(f"--output {name}=<path>")
        elif kind in {"int", "decimal", "bool", "date", "month"}:
            parts.append(f"--output {name}=<{kind}>")
        else:
            parts.append(f"--output {name}=@{name}.txt")
    if json_names:
        parts.append("--output-json outputs.json")
    return " ".join(parts)
