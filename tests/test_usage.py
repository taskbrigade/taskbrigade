"""Usage extraction from agent output (cli/usage.py) and budget state (app/services/usage.py)."""
from types import SimpleNamespace

from cli.usage import parse_usage_output, read_usage_file
from app.services.usage import budget_state


def test_parse_usage_output_claude_result():
    text = '{"type":"result","total_cost_usd":0.12,"usage":{"input_tokens":1000,"output_tokens":500}}'
    assert parse_usage_output(text) == {"input_tokens": 1000, "output_tokens": 500, "cost_cents": 12}


def test_parse_usage_output_flat_and_model():
    text = '{"input_tokens":7,"output_tokens":3,"cost_cents":4,"model":"gpt-4"}'
    assert parse_usage_output(text) == {"input_tokens": 7, "output_tokens": 3, "cost_cents": 4, "model": "gpt-4"}


def test_parse_usage_output_last_match_wins():
    text = '{"usage":{"input_tokens":1}}\n{"usage":{"input_tokens":2,"output_tokens":3}}'
    assert parse_usage_output(text) == {"input_tokens": 2, "output_tokens": 3}


def test_parse_usage_output_ignores_non_json():
    assert parse_usage_output("hello\nworld") is None
    assert parse_usage_output("") is None
    assert parse_usage_output(None) is None


def test_read_usage_file(tmp_path):
    (tmp_path / ".tm-usage.json").write_text('{"input_tokens": 9, "output_tokens": 2, "cost_cents": 3, "model": "m"}')
    assert read_usage_file(tmp_path) == {"input_tokens": 9, "output_tokens": 2, "cost_cents": 3, "model": "m"}
    assert read_usage_file(tmp_path / "other") is None


def test_read_usage_file_invalid(tmp_path):
    (tmp_path / ".tm-usage.json").write_text("not json")
    assert read_usage_file(tmp_path) is None


def _run(tokens=None, cents=None, used_tokens=0, used_cents=0):
    return SimpleNamespace(budget_tokens=tokens, budget_cents=cents, used_tokens=used_tokens, used_cents=used_cents)


def test_budget_state():
    assert budget_state(_run(None, None)) == "ok"
    assert budget_state(_run(tokens=1000, used_tokens=10)) == "ok"
    assert budget_state(_run(tokens=1000, used_tokens=800)) == "warn"
    assert budget_state(_run(tokens=1000, used_tokens=1000)) == "exceeded"
    assert budget_state(_run(cents=300, used_cents=240)) == "warn"
    assert budget_state(_run(cents=300, used_cents=300)) == "exceeded"
    # A second, unset budget dimension never counts.
    assert budget_state(_run(tokens=1000, cents=None, used_tokens=500, used_cents=10_000)) == "ok"
