# Workflow output contracts and placeholders

Workflow steps pass values with a small resolver, not Jinja. Templates only interpolate
`{{path | filter}}`. There are no loops, expressions, or attribute access beyond the paths below.
The implementation lives in `app/services/workflows/` and does not start or advance a run; the run
engine calls it when a step is activated and when a version is published.

## Placeholder syntax

```text
{{ <path> [| <filter>]* }}
\{{                  literal {{
```

Paths:

| Path | Meaning |
|---|---|
| `{{name}}` or `{{params.name}}` | A workflow parameter. The short form is only a parameter. |
| `{{run.id}}` `seq` `key` `url` `started_at` `scheduled_for` | Run fields supplied by the caller. |
| `{{run.period}}` `prev_month` | `YYYY-MM` of the month before `scheduled_for`, or else `started_at`, in the schedule timezone. |
| `{{run.this_month}}` `year` | The month and year of that same instant. |
| `{{steps.<key>.task_nr}}` and `status` `summary` `branch` `pr` `commit` `merge_commit` `open_points` | Built-ins of an earlier step that has a task. |
| `{{steps.<key>.result}}` `profile` | Built-ins of an earlier `runner` step. |
| `{{steps.<key>.script}}` | `{path, commit}` of an earlier `script` step. |
| `{{steps.<key>.output.<name>}}` and `.<field>` | A declared output. `file` fields are `path` and `commit`. |
| `{{workflow.key}}` `name` `version` | The template. |
| `{{project.key}}` `name` | The project. |
| `{{event.<name>}}` | Trigger parameters only. |

Filters: `default("literal")`, `json`, `upper`, `lower`, `first_line`, `truncate(n)`, `date("strftime")`.
A filter must match the value's type. `default` is the only way to substitute a missing or empty value;
an empty string is never inserted silently. `0` and `false` are real values.

A file output with no filter renders as `path@` plus the first 7 characters of the commit SHA.
`\{{` is the only escape.

Resolution is strict. An unknown parameter, a later step, a step that is not `succeeded` or `done`,
or a missing output raises `PlaceholderError` with code `placeholder_unresolved` and that path.
`failure_effect` tells the run engine to mark the step `failed` and the run `paused`. The rendered
title, description and acceptance criteria are what the task stores; the trace lists `path → value`
and masks values that came from a `local_only` or `sealed` step.

## Output contract

`outputs_json` on a step:

```json
{
  "kernaussagen": {"type": "text", "required": true, "max_length": 4000, "description": "Short findings"},
  "quellen": {"type": "json", "required": true, "schema": {"type": "array", "items": {"type": "string"}}},
  "bericht": {"type": "file", "required": true, "path_glob": "reports/*.md"}
}
```

Types: `text`, `int`, `decimal`, `bool`, `date` (`YYYY-MM-DD`), `month` (`YYYY-MM`), `json`, `file`,
`commit`, `dataset`, `profile`, `result`. JSON schemas may use only `type`, `properties`, `required`,
`items` and `enum`. Value types are rejected above 64 KiB. `decimal` is stored as a JSON string so
precision is kept. A file path must stay inside the repository (`reports/a.md`, not `../` or an
absolute path). `*` in `path_glob` does not cross `/`.

`submit` checks the contract before it opens a pull request and returns `422 outputs_invalid`
with one message per problem, for example `datei (file) is missing`. Built-ins and the declared
object are then stored on `workflow_run_steps.output_json` and returned on the task as
`workflow_outputs`, so TASK.md can show them to a reviewer. Approval sets `status` to `done`, records `merge_commit`, and
rewrites file outputs onto that commit. A path that disappeared in the merge is `outputs_invalid`
and the task stays in review.

Built-ins available without a declaration, when the step has a task: `task_nr`, `status`, `summary`,
`branch`, `pr`, `commit`, `merge_commit`, `open_points`. `result` and `profile` are runner-only;
`script` is script-only.

## How an agent sets outputs

- CLI: `tbr submit DEMO-0142 --output kernaussagen=@kern.md --output anzahl=12 --output-json outputs.json`.
  `@file` reads the file (a trailing newline is stripped). `@@` is a literal `@`.
- MCP: `submit_task(..., outputs={...})`.
- `.tm-submit.md`: a `## Outputs` heading with one JSON object, optionally in a ` ```json ` fence.
  `## Open points` still works and either section may come first. Invalid JSON is not submitted; the
  runner gives the task back and leaves the file in place.

TASK.md of a workflow task has a **Required outputs** section with the name, type, description and
an example `tbr submit` command.

## Input bindings

`inputs_json` is a list of `{ref, purpose}`. `purpose` is `input`, `script` or `output`. `ref` must
be exactly one placeholder. `resolve_bindings` returns the raw value; a `script` binding also
returns `path` and `commit` for the run task's `run_script_path` / `run_commit_sha`. Rows are not
inserted into `task_data_refs` here — DZ 9 owns that integration. Runner activation currently populates the
existing dataset/commit/path fields and agent consumers receive only released artifacts in their descriptions.

## Publish checks

`check_publish(definition, role_candidates=..., data_enabled=...)` returns these issues (design record:
`research/konzept-workflows.md` §3.6):

| Code | Meaning |
|---|---|
| `unknown_placeholder` | The path does not exist, or it points at this step or a later one. |
| `undeclared_output` | `steps.<key>.output.<name>` and `<name>` was not declared. |
| `zone_boundary_without_gate` | A looser step reads a stricter one without the required gate or approval. |
| `auto_review_not_allowed` | `auto` on a script step, on output a runner executes, or when a later step is looser. |
| `no_assignee_rule` | An agent, script, runner or approval step has neither an assignee nor a role. |
| `role_unresolvable` | Warning only: the role currently has no candidate. |
| `runner_without_data_zones` | `runner`, `gate` or `script` while data zones are off. |
| `workflow_runner_invalid` | A runner step must be sealed. |
| `workflow_gate_invalid` | A mandatory manual gate must directly follow a runner. |
| `param_default_invalid` | The default is not the parameter's type. |

Two extra codes are used for contracts the concept does not name separately: `placeholder_syntax`
(broken braces or filters) and `invalid_contract` (a declaration that cannot be stored).
`filter_mismatch` means a filter does not apply to the declared type.

Data classes, strict to loose: `sealed`, `local_only`, `cloud_ok`. A missing class uses the
project privacy when the caller passes it. Reading `sealed` output from a looser step allows only
the references `result`, `profile`, `task_nr` and `status`, and only when a `gate` is the next step
after a runner (or between the steps otherwise). `local_only` into `cloud_ok` needs a manual
`approval` between the two steps. The approval step itself may read the value; other looser steps
may read it only after that approval.

`definition_from_version(version, template, project)` builds the dict from the stage-1 tables.
`allow_auto_review` defaults to true. `data_enabled` defaults to false because the project row has
no such column yet; pass true once data zones are on. Passing `role_candidates` (role → count)
enables the warning. Omit it to skip the warning.

## Example

`tests/test_workflow_outputs.py::test_content_pipeline_example` replays the content pipeline of the design record
(`research/konzept-workflows.md` §3.7): the resolved recherche title, the stored output object, the entwurf
description, `422` when `datei` is missing, the file reference after merge, and the notify text
`content/workflows.md@<7 chars>`.
