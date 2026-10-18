"""Output contracts, placeholders (WF 3) and the transactional workflow run engine (WF 2).

The run engine activates a step by calling `render`. A `PlaceholderError` means the
step failed and the run pauses (`failure_effect`). Publishing a version calls
`check_publish`. `submit` stores declared outputs on the run step; approval rewrites
file commits to the merge commit. Callers own commit/rollback; no background worker
is needed.

No Jinja: templates only interpolate `{{path | filter}}`.
"""

from app.services.workflows.contracts import (
    ContractError, apply_submit_outputs, build_output_json, finalize_approved_outputs,
    output_contract_view, resolve_bindings, validate_outputs,
)
from app.services.workflows.engine import (
    advance, cancel, on_task_transition, pause, resume, start,
)
from app.services.workflows.placeholders import (
    SHORT_SHA, Context, PlaceholderError, Resolution, StepValue, failure_effect, render,
)
from app.services.workflows.publish import PublishIssue, check_publish, definition_from_version

__all__ = [
    "SHORT_SHA", "Context", "ContractError", "PlaceholderError", "PublishIssue", "Resolution",
    "StepValue", "advance", "apply_submit_outputs", "build_output_json", "cancel", "check_publish",
    "definition_from_version", "failure_effect", "finalize_approved_outputs", "on_task_transition",
    "output_contract_view", "pause", "render", "resolve_bindings", "resume", "start",
    "validate_outputs",
]
