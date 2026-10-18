# Workflows through the API, CLI and MCP

Templates live in a project and have a key such as `DEMO/content`. Each run pins a published version. Completing
tasks advances the run in the same transaction. See the [engine](workflow-engine.md) and
[output contracts](workflow-outputs.md) for task behavior; the web UI is described in
[workflows-ui.md](workflows-ui.md). All REST paths below are relative to `/api/v1` and
require `Authorization: Bearer <token>`. The server's `/docs` provides request schemas.

## Access

Admins, project owners and project members can read templates and history. Defaults, template text, run parameters,
errors and step outputs are masked when the caller cannot handle their data class. CLI and MCP use these same
responses and server checks.

Admins, orchestrators and the human project owner may edit/publish definitions. Human admins/orchestrators, the
human project/template owner, and project-member orchestrator agents may start runs. Other agents cannot start
runs, including previews. Human admins, the human project/template owner and human starter may control a run;
an orchestrator agent may control runs it started. Archived templates/projects cannot start runs; a paused template
still permits manual starts.

## REST

| Request | Result |
|---|---|
| `GET /workflows?project=DEMO` | Visible templates, parameters, version, steps and last run status |
| `GET /workflows/DEMO/content?version=1` | Template and selected version (default: current) |
| `GET /workflows/DEMO/content/draft` | Draft, for editors |
| `PUT /workflows/DEMO/content/draft` | Replace draft; create template on first save, owned by project owner |
| `POST /workflows/DEMO/content/publish` | Validate, hash and publish draft as current version |
| `GET /workflows/DEMO/content/export?version=1` | Canonical YAML of a version (default: current) as a download |
| `PUT /workflows/DEMO/content/import` | Store a canonical YAML document as an unpublished draft (never auto-publishes) |
| `POST /workflows/DEMO/content/runs` | Start or preview |
| `GET /workflow-runs?project=DEMO&workflow=DEMO/content&status=paused&limit=20` | Visible runs, newest first; limit 1–200 |
| `GET /workflow-runs/412` | Pinned version, attempts, tasks, outputs and budgets |
| `POST /workflow-runs/412/pause` | Pause with optional `reason`; active tasks continue |
| `POST /workflow-runs/412/resume` | Resume with optional `from_step`, `skip_failed`, `reason`, `budget_tokens`, `budget_cents` |
| `POST /workflow-runs/412/cancel` | Cancel with optional `reason`, including open tasks |
| `POST /workflows/tick` | Reserved scheduler endpoint; requires loopback and admin token |

Example draft:

```json
{
  "name": "Content pipeline",
  "params": [{"name": "topic", "param_type": "string", "label": "Topic", "required": true}],
  "steps": [{
    "step_key": "write", "step_type": "agent", "agent_role": "writer",
    "title_tpl": "Write about {{topic}}", "description_tpl": "Create a concise article.",
    "acceptance_tpl": "The article is ready for review.", "review_mode": "manual"
  }]
}
```

Draft steps use model field names (`title_tpl`, `description_tpl`, `acceptance_tpl`, `assignee_id`, `agent_role`,
`reviewer_id`, `data_class`, `model_tier` and timeout/retry/optional fields). `outputs`, `inputs` and `config` are
JSON objects/list, not serialized strings. Parameter fields are `name`, `param_type`, `required`, `default_expr`,
`enum_values`, `label`, `help`. Read responses use canonical fields `key`, `type`, `title`, `description`,
`acceptance`, `role`, `review`, `outputs`, `inputs`, `config`. Repeated saves replace the same draft. Published
definitions cannot be changed through these endpoints; the next save creates a new version. Runs retain their
original version. Failed publication leaves the draft intact.

## Export and import (YAML)

The canonical export of a version is stable, human-readable YAML – for diffs between versions, copying and tests,
not a backup format (the database is the source of truth). `GET /workflows/DEMO/content/export` writes the current
version; add `?version=1` (or `?version=draft` in the web UI) for another one. Only the fields that carry a value are
written, so a hand-written document may leave anything out:

```yaml
workflow: content-pipeline
version: 3
name: Content pipeline
params:
  - {name: thema, type: string, label: Thema}
  - {name: zielgruppe, type: enum, values: [kunden, partner], default: kunden}
steps:
  - key: recherche
    type: agent
    role: researcher
    title: "Recherche: {{thema}}"
    outputs: {quellen: {type: json, required: true}}
  - key: freigabe
    type: approval
    reviewer: admin
    title: "Freigabe {{thema}}"
```

`assignee`/`reviewer` accept an actor name (`admin`) or a numeric actor id (`1`). The canonical export writes that
name/id, so the round trip is lossless; import resolves only names and ids. The `human:owner` shorthand shown in
the design record (`research/konzept-workflows.md` §1.6) is *not* accepted on import – write the project owner's name
or id instead.
`workflow` and `version` are informational on import (the target key comes from the URL).

`PUT /workflows/DEMO/content/import` (body = the raw document, `Content-Type: application/yaml`) creates or replaces
the **unpublished** draft and never publishes. The document runs through the same checks as the editor; blocking
problems (unknown fields, duplicate keys, an agent without role/assignee, a runner without data zones, …) are refused
with `workflow_import_invalid`, so a broken document never becomes a draft. Publish the draft as usual with
`POST …/publish`. The round trip (export → import) is lossless: re-exporting the draft reproduces the document.

Run request:

```json
{"params": {"topic": "Repeatable workflows"}, "budget_tokens": 20000, "budget_cents": 300, "dry_run": true}
```

Parameters are validated by declared type. CLI strings convert to integers, exact decimal strings, and booleans
(`true`/`false`). Dates/months use ISO format. Unknown or missing required parameters, invalid enum values,
non-finite decimals and unresolved defaults return 422. Published defaults use literals and run calendar fields
such as `{{run.prev_month}}`. Budgets are nonnegative unsigned integers; zero is preserved.

`dry_run` shares execution's parameter, text and role resolvers. It creates no run, task, attempt, notification or
sequence reservation. It returns first-step text, input bindings and placeholder traces, plus all steps' roles,
currently eligible assignees, review modes, classes and validation warnings. Later placeholders are `deferred`
because their outputs do not exist yet. `<new-run>` stands in for a future run ID/URL; sequence and role choices
are provisional. Restricted text and traces are masked. Authentication still updates usual last-seen metadata.

Resume retries failed steps. `from_step` restarts the current or an earlier step and resets later attempts.
`skip_failed=true` only skips optional steps, never approvals or gates. Reasons are stored in the run's error
detail field, subject to masking. Generated tasks have no sprint; API task responses include `task_kind`.

## Usage and budgets

Agents report token/cost usage; each report is a row in `task_usage` and counts towards the workflow run's
`used_tokens`/`used_cents`. Reports are voluntary and unverifiable – the budget is a brake against endless loops,
not billing. A run without any report shows "usage unknown" instead of zero.

- CLI: `tbr submit 412 --usage input=1200,output=800,cost_cents=15,model=gpt-4`, or the same `--usage` on
  `tbr heartbeat`.
- MCP: `submit_task(..., usage={"input_tokens": 1200, "output_tokens": 800, "cost_cents": 15, "model": "…"})` and
  `report_waiting(..., usage=…)`.
- The runner (`tbr run`) reads usage where the agent program reports it (a JSON result line with `usage`/cost
  fields, or a `.tm-usage.json` in the worktree) and reports it on submit.

At 80 % of a set budget the run owner is notified; at 100 % the run pauses with `budget_exceeded`. No further
`agent`/`script` step activates; `approval`, `notify`, `runner` and `gate` do not consume the agent budget. The heartbeat response
carries `budget_exceeded`, so a running agent can stop after its next checkpoint. Resuming a run that exceeded its
budget requires a higher budget (`budget_tokens`/`budget_cents`), otherwise the run stays paused.

## CLI

```text
tbr workflows --project DEMO
tbr workflow show DEMO/content --version 1
tbr workflow start DEMO/content --param topic="Repeatable workflows" --budget-cents 300 --dry-run
tbr workflow start DEMO/content --param topic="Repeatable workflows"
tbr workflow runs DEMO/content --status paused --limit 20
tbr workflow run 412
tbr workflow pause 412 --reason "Check inputs"
tbr workflow resume 412 --from-step write
tbr workflow resume 412 --skip
tbr workflow cancel 412 --reason "No longer needed"
tbr workflow export DEMO/content --output content.yaml
tbr workflow export DEMO/content --version 1
tbr workflow import DEMO/content content.yaml
tbr workflow import DEMO/content - < content.yaml
```

All commands support `--json`. Repeat `--param name=value` for different parameters. Duplicate names and malformed
workflow paths fail before an API request. Server errors use the standard CLI error/exit behavior.

## MCP

The existing `tbr mcp` server exposes English descriptions/replies and a German catalog:

| Tool | Parameters |
|---|---|
| `list_workflows` | `project=None` |
| `show_workflow` | `workflow`, `version=None` |
| `start_workflow_run` | `workflow`, `params`, `budget_tokens=None`, `budget_cents=None`, `dry_run=False` |
| `list_workflow_runs` | `workflow=None`, `project=None`, `status=None`, `limit=20` |
| `show_workflow_run` | `run_id` |
| `pause_workflow_run` | `run_id`, `reason` |
| `resume_workflow_run` | `run_id`, `from_step=None`, `skip_failed=False` |
| `cancel_workflow_run` | `run_id`, `reason` |
| `draft_workflow` | `workflow`, `yaml_document` |

Workflow arguments use `PROJ/key`. Preview before starting with `dry_run=True`. Real starts return the run ID,
first task and step states; lists show sequence, status, progress and current task status; details include masked
outputs and budgets. There are no MCP template-editing tools, with one exception: `draft_workflow` drafts a version
from a canonical YAML document (an orchestrator agent drafts, a human publishes). Scheduling tools do not exist.
Declared task outputs still use
`submit_task(outputs=...)`; usage reports go through `submit_task(usage=...)` or `report_waiting(usage=...)`.

Reviewer agents claim approval jobs with `start_task` or `tbr prepare`. Preparation writes `REVIEW.md` without
requiring a repository, branch or Git credentials. `tbr session --review` uses an approval-specific prompt and
the normal review lease lifecycle. Decide with `review_task` or `tbr review`; approval tasks do not use ordinary
submission or PR creation.

## Stage boundaries

Workflow schedules, calendars, budget accounting/enforcement and [event triggers](workflow-events.md) are
available. `runner` and manual `gate` steps use the data-zone services (see [Workflow engine](workflow-engine.md));
`script` steps support [reviewed pins and bounded schema repair](workflow-scripts.md).
Dataset parameters are project-scoped numeric IDs checked
against dataset state and the template owner's data rights.

Schedule operations are described in [Workflow schedules and calendars](workflow-schedules.md).
Template responses now include schedules, next-fire dates and local previews with UTC offsets.
Schedule parameters obey the same data-class masking as template defaults. Manual run starts reject overlaps;
`queue: true` allows one waiting run, and `force_parallel: true` is restricted to human admins.
