"""Best-effort usage extraction from agent-program output for the runner (research/konzept-workflows.md §7.4).

Agent programs that report their cost (e.g. Claude Code's final JSON result) are read by the runner and reported
to the server on submit. The extraction is deliberately lenient: usage is voluntary and unverifiable, so a missed
field only means the run shows "usage unknown".
"""
import json
from pathlib import Path

TOKEN_SOURCES = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                 ("input", "input_tokens"), ("output", "output_tokens"))
CENT_SOURCES = ("cost_cents", "total_cost_cents")
USD_SOURCES = ("cost_usd", "total_cost_usd")


def parse_usage_output(text: str) -> dict | None:
    """Token/cost usage from agent output, scanning one JSON object per line (last match wins).

    Recognises `{"usage": {"input_tokens": n, "output_tokens": n}}`, Claude Code's
    `{"type": "result", "total_cost_usd": 0.123, "usage": {...}}` and flat `cost_cents`/`cost_usd` fields.
    Returns `{"input_tokens", "output_tokens", "cost_cents", "model"}` with the fields found (None when nothing)."""
    if not text:
        return None
    found = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        result: dict = {}
        for src, dst in TOKEN_SOURCES:
            value = obj.get(src)
            if value is None:
                value = usage.get(src)
            if isinstance(value, int) and value >= 0:
                result[dst] = value
        cents = None
        for key in CENT_SOURCES:
            value = obj.get(key)
            if value is None:
                value = usage.get(key)
            if isinstance(value, int):
                cents = value
                break
        if cents is None:
            for key in USD_SOURCES:
                value = obj.get(key)
                if value is None:
                    value = usage.get(key)
                if isinstance(value, (int, float)):
                    cents = int(round(value * 100))
                    break
        model = obj.get("model")
        if isinstance(model, str) and model.strip():
            result["model"] = model.strip()[:100]
        if cents is not None:
            result["cost_cents"] = cents
        if result.get("input_tokens") or result.get("output_tokens") or "cost_cents" in result:
            found = result
    return found


def read_usage_file(worktree: Path) -> dict | None:
    """A `.tm-usage.json` in the worktree is the explicit contract for agents that report usage without JSON output."""
    path = worktree / ".tm-usage.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    result = {}
    for src, dst in TOKEN_SOURCES[:2]:
        if isinstance(data.get(src), int) and data[src] >= 0:
            result[dst] = data[src]
    for key in CENT_SOURCES:
        if isinstance(data.get(key), int):
            result["cost_cents"] = data[key]
            break
    model = data.get("model")
    if isinstance(model, str) and model.strip():
        result["model"] = model.strip()[:100]
    return result or None
