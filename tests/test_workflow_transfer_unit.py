"""WF 14: unit tests of the YAML transport (`app/services/workflows/transfer.py`) that need no test DB."""
from types import SimpleNamespace

import pytest

from app.api.workflows import Draft
from app.services.workflows import transfer
from app.services.workflows.transfer import ImportError_


def _step(**over):
    base = dict(step_key="run", step_type="runner", title_tpl="Run", agent_role="runner",
                review_mode="manual", description_tpl=None, acceptance_tpl=None, assignee_id=None,
                data_class=None, reviewer_id=None, model_tier=None, timeout_minutes=None,
                wait_reminder_hours=None, max_retries=0, retry_backoff_s=300, optional=False,
                outputs_json=None, inputs_json=None, config_json=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_retry_backoff_zero_survives_the_round_trip():
    """A step with retry_backoff_s=0 (allowed) must not be exported field-less and re-imported as 300."""
    doc = transfer._step_document(_step(retry_backoff_s=0))
    assert doc["retry_backoff_s"] == 0
    assert transfer.parse_document(None, {"steps": [doc]})["steps"][0]["retry_backoff_s"] == 0


def test_retry_backoff_non_default_survives_the_round_trip():
    doc = transfer._step_document(_step(retry_backoff_s=45))
    assert doc["retry_backoff_s"] == 45
    assert transfer.parse_document(None, {"steps": [doc]})["steps"][0]["retry_backoff_s"] == 45


def test_default_retry_backoff_is_omitted_from_the_document():
    assert "retry_backoff_s" not in transfer._step_document(_step(retry_backoff_s=300))


def test_max_retries_round_trip_for_a_runner_step():
    """A runner's non-default max_retries (0) is exported; the default (2) is omitted and comes back as 2."""
    doc = transfer._step_document(_step(max_retries=0))
    assert doc["max_retries"] == 0
    assert transfer.parse_document(None, {"steps": [doc]})["steps"][0]["max_retries"] == 0
    doc = transfer._step_document(_step(max_retries=2))
    assert "max_retries" not in doc
    assert transfer.parse_document(None, {"steps": [doc]})["steps"][0]["max_retries"] == 2


def test_runner_step_without_max_retries_uses_the_api_default():
    """`StepDraft` defaults runner steps to 2; a hand-written runner step must not be forced to 0."""
    body = transfer.parse_document(None, {"steps": [
        {"key": "run", "type": "runner", "title": "Run", "role": "runner"}]})
    assert Draft(**body).steps[0].max_retries == 2


def test_explicit_max_retries_zero_is_kept():
    body = transfer.parse_document(None, {"steps": [
        {"key": "run", "type": "runner", "title": "Run", "role": "runner", "max_retries": 0}]})
    assert Draft(**body).steps[0].max_retries == 0


def test_boolean_fields_refuse_wrong_types():
    """`optional`/`required` are read as booleans, not by truthiness – 'no' must not become True."""
    with pytest.raises(ImportError_):
        transfer.parse_document(None, {"steps": [
            {"key": "n", "type": "notify", "title": "T", "optional": "no"}]})
    with pytest.raises(ImportError_):
        transfer.parse_document(None, {
            "params": [{"name": "p", "type": "string", "required": "no"}],
            "steps": [{"key": "n", "type": "notify", "title": "T"}]})


def test_boolean_fields_accept_real_booleans_and_default_required():
    parsed = transfer.parse_document(None, {
        "params": [{"name": "p", "type": "string", "required": False}],
        "steps": [{"key": "n", "type": "notify", "title": "T", "optional": True}]})
    assert parsed["params"][0]["required"] is False
    assert parsed["steps"][0]["optional"] is True
    # `required` defaults to true when omitted.
    parsed = transfer.parse_document(None, {
        "params": [{"name": "p", "type": "string"}],
        "steps": [{"key": "n", "type": "notify", "title": "T"}]})
    assert parsed["params"][0]["required"] is True
