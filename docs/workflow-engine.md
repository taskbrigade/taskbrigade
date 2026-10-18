# Workflow run engine

`app.services.workflows` provides `start`, `advance`, `pause`, `resume` and `cancel`. These are service functions;
HTTP, CLI, MCP and draft publication are documented in [Workflow API, CLI and MCP](workflows-api.md). The caller owns the
SQLAlchemy session and transaction. Commit the entire operation, or roll it back on an exception.

```python
from app.services import workflows

run = workflows.start(session, actor, template, params={"period": "2026-09"})
workflows.pause(session, actor, run)
workflows.resume(session, actor, run)
session.commit()
```

Starting locks the template to allocate its run sequence, pins its published current version and creates pending
run-step rows. Only the first step is activated. Each mutation locks the run; locking reads of attempts and
sequences also work when concurrent requests already hold older MariaDB repeatable-read snapshots. `advance`
is idempotent: an already active step keeps its task, and a completed run does nothing.

Task transitions call the engine inside the same transaction, including human status overrides. A task reaching
`done` completes its step and activates the successor. `aborted` schedules a configured retry, or pauses the run
when retries are unavailable/exhausted. `cancelled` pauses without retry.
Questions put the run in `waiting`; answering returns it to `running`. Explicit background waiting does not change
the run state and counts as working time. See [Workflow error handling](workflow-errors.md) for clocks, retries
and reminders.

## Assignment and step types

For reviewed scripts that can be reused across runs and repaired after schema changes, see
[Write once, run repeatedly](workflow-scripts.md).

An agent or approval step must specify exactly one fixed `assignee_id` or project `agent_role`. Candidates must
be active project members, visible to the template owner, allowed to handle the step's effective data class, and
suitable for its type. Agent steps exclude humans and runners; manual approvals require humans. Reviewer agents
may handle approval steps with agent review. Among role candidates the lowest rank wins, then the smallest number
of open assigned tasks/review jobs, then actor id. Agent capacity limits apply before selection. Failure pauses
with `no_assignee`; there is no fallback assignment outside the role.

- `runner` creates a sealed `run` or `profile` task assigned to a local Runner actor (`agent_kind=runner`,
  `model_location=none`). Data zones must be enabled. Set `config.task_kind` to `run` (default) or `profile`;
  `config.run` uses the normal runner fields: `dataset` (key or numeric dataset parameter), `commit_sha` and
  `script_path`. Profile jobs take only the dataset. Fields support workflow placeholders. Input bindings with
  purpose `input` supply the dataset; purpose `script` supplies a file's path and commit. Commits still pass
  the runner's reviewed/merged-commit check before execution. A step timeout can shorten the admin runner limit.
  Runner cancellation/restart/reassignment stops the sandbox at the next heartbeat; a manual run pause lets
  the job finish. Execution failures use configured retries with fresh tasks and jobs.
- `gate` creates no task and must directly follow a runner `run` step. It is mandatory and always manual.
  When a job passes the output checks, the runner attempt succeeds while its task remains `for_review`.
  The gate puts the run in `waiting` and watches that same result. Release through **Data releases** completes
  the gate and activates the consumer; the existing data rights, four-eyes checks and audit apply. Rejection or
  keeping this gated result sealed pauses with `gate_rejected`. Failed automatic checks pause with
  `gate_checks_failed`. Gates receive the normal daily waiting reminders. Decisions during a manual pause
  are recorded and successors wait for resume. No workflow setting, auto review or agent can approve a gate.
- Without a following gate, a successful `run` result is marked `kept_sealed` by the output-gate service and
  the task completes without release. Only task-number/status references may flow into a looser notification;
  consuming that result in a looser step still fails. Profile tasks wait for the separate human profile release,
  except when the following script step can reuse a reviewed pin against a locally matching fingerprint.

For example, a runner's `config` can be:

```json
{
  "task_kind": "run",
  "run": {
    "dataset": "{{params.dataset}}",
    "commit_sha": "{{steps.code.merge_commit}}",
    "script_path": "analysis/month.py"
  }
}
```

Give the runner step its own `assignee_id` or `agent_role`, `data_class=sealed`, description and acceptance criteria.
Place a `gate` step immediately afterward, then let the cloud report step read `{{steps.execute.result}}`.
The consumer receives only released aggregates or an approved profile in its description, with API links for
large artifacts. Profile API access is scoped to the assigned active workflow consumer's recorded input snapshot.
Quarantine outputs and logs never enter these descriptions or the run's output JSON.

- `agent` creates a normal `work` task in `defined`, with rendered title, description and acceptance criteria,
  model tier and reviewer. It completes on `done`, after the existing review/merge path. Explicit `auto` review
  reuses that merge path after submission and records a system `auto_approved` status event. Auto-review is
  refused when a later step has a lower data class or executes code with a runner/script. Merge failures leave
  the task in review and pause the run for a human decision.
- `approval` creates an unassigned `approval` task in `for_review`, with the resolved actor as its reviewer.
  Approve completes it without requiring a pull request. Reject records the decision, cancels that approval task,
  and uses `config_json.on_reject`: `fail` pauses, while `rework:<step_key>` reopens an earlier agent task as
  `changes_requested` with the rejection comment. The default target is the nearest earlier agent step. Later
  steps get new pending attempts, so intermediate work repeats and the next approval has a new task. After
  `max_rework` rounds (default three), rejection pauses the run. Old attempts and task history remain intact.
  Reopening merged work clears its current PR/result metadata so resubmission creates a new PR; the prior PR
  remains in task events. A reviewer agent can decide through the existing review service/API. Runner preparation
  writes `REVIEW.md` for branchless approval jobs without requiring Git credentials or a checkout.
- `notify` creates no task. `config_json` accepts `to: ["owner", "starter"]` (default `owner`) and `text`, falling
  back to the step description/title. It completes after recording applicable notifications in the existing
  outbox transaction. Delivery, retries, quiet hours, event preferences and mutes use the existing notification
  services. Disabled Telegram, unlinked recipients and excluded events have the same no-delivery behavior as
  ordinary task notifications. Queue errors roll back completion rather than silently losing the notice.

For result delivery, set a notify step's `config_json.channel` to `email`. An earlier task is required: the request
appears on the latest preceding task in **Emails** and **Email approvals** in the inbox. The template owner requests
the mail, and the ordinary result-mail checks apply (see [operations](operations.md#sending-work-results-by-email)).
`to` and `cc` are lists of allowed project recipient addresses or confirmed human addresses; `owner` resolves to
the project owner's address, which must also be allowed/confirmed. `subject` defaults to the title; `body` defaults
to `text`, then description/title. These fields support the ordinary workflow placeholders.

```json
{
  "channel": "email",
  "to": ["recipient@example.org"],
  "subject": "Report {{run.seq}}",
  "body": "The approved report is attached.",
  "attachments": ["{{steps.write.output.report}}"]
}
```

Each attachment placeholder must be a whole, unfiltered reference to an earlier declared `file` or `result` output
(`{{steps.write.output.report}}`, `{{steps.analyse.output.aggregate}}`), or the runner's built-in
`{{steps.analyse.result}}`. File outputs are read from the producer's pinned merge commit. Literal result-mail
references (`attachment:`, `branch:`, `merge:`, `result:`) are also supported; branch/merge references belong to the
task carrying the mail. Missing outputs, unsafe attachments, disabled email and invalid recipients pause the run.
Restricted step contents cannot be interpolated into email text (task number/status are allowed).

Local-only project/task/notify mail requires at least one released `result:<id>` attachment and accepts no other
source. It always waits for a separate human mail approval, regardless of recipient mode or task approval. Result
attachments contain only the gate's released, transformed aggregates as Markdown with JSON; quarantine files and
logs never leave the server. Sealed mail is refused. The strictest project/task/notify class is saved with the
request and cannot be loosened by later edits. Delivery rechecks class, release status and attachment hashes.

The notify step succeeds when the send request is accepted, including requests waiting for approval; it creates
no task and records the request ID in `output_json.mail_id`. SMTP delivery/retry/failure remains visible on the mail
request. Advancing the same completed attempt again does not create another request; explicitly rerunning the step
creates a new request.

Tasks never receive a sprint; assigning one later returns `workflow_task_no_sprint`. Successor tasks link to the
nearest preceding task through `follow_up_of_id`. The bidirectional task/run-step foreign keys preserve history.

## Conditions and parallel groups

A step may carry a `when:` condition (`condition_tpl`) and a `parallel_group`. Conditions evaluate with the ordinary
placeholder resolver (parameters, run fields, earlier step outputs). The step is skipped when the rendered value is
empty or one of the falsy literals `false`, `0`, `no`, `off`, `none`, `null`; the skip is recorded with
`error_code=condition_false` and the condition as the reason, so the run detail explains it. An unresolved
placeholder fails closed (`placeholder_unresolved`, run paused). Conditions are only allowed on `agent`, `notify`,
`script` and `runner` steps – `approval` and `gate` are human/zone-boundary decisions and can never be skipped.

Consecutive steps that share a non-empty `parallel_group` activate together; the run advances past the group only
once every member is `succeeded` or `skipped`. Each member keeps its own task, assignee resolution, retries, clocks
and reminders, and a failed member pauses the run exactly like a single step (sibling tasks continue). Groups may
contain only `agent` and `notify` steps. Publication rejects a lone group name and any condition or group that
breaks these rules (`workflow_condition_invalid`, `workflow_parallel_invalid`).

## Run controls

Run controls allow human admins, the human project/template owner, the human starter, or an orchestrator for its own
run. Starting allows human admins, human project/template owners, human orchestrators and project-member orchestrator
agents. Archived templates/projects cannot start; a paused template still allows manual starts.

Pause leaves active tasks running. Completion is recorded during the pause, but creates no successor until
resume. Resume uses the pinned version and retries a failed step with a new attempt. `from_step="key"` allows
the same or an earlier step, resets all later steps, cancels their open tasks and preserves completed history.
`skip=True` requires an optional step and never permits skipping approvals or gates. Cancel marks the run and
all pending/active steps cancelled, and cancels every open task with an audited comment; completed tasks remain.

## Integration boundaries

`render_step(session, run, step)` shares `workflows.rendering` with dry-run previews: typed parameters/defaults,
the WF 3 placeholder resolver, previous attempt outputs and input-binding checks. Missing values pause activation
before a task is created. Draft publication validates and hashes definitions; published versions remain immutable
through the API. Dataset parameters are positive numeric IDs checked against the workflow project, dataset state
and the template owner's data rights. Bindings are not written to `task_data_refs`: the run records its inputs
itself, and the ad-hoc follow-up drafts of the [task chain](data-store.md#task-chain-across-zones) skip workflow tasks.

Publication and activation inspect actual reads in titles, descriptions, acceptance criteria, configuration and
input bindings. Crossing from sealed to a looser class requires a gate directly after the runner and a released
result; profiles use their separate human release. Task-number/status references carry no contents and need no
gate. Arbitrary sealed text (including summaries and declared values) cannot cross, even after a gate. Activation
uses the strictest current and recorded producer class, so later project-class changes cannot bypass checks.
A local-to-cloud boundary requires
a preceding manual approval, whose decision must come from a human project owner/admin, including when an agent
admin attempts approval or a human uses a status override. Notify and pause notices omit content and names when
any pinned step is restricted. This conservative masking also covers inherited project privacy changes.

Activation stores the effective step class on the task (`tasks.data_class`). The data class rules apply to the
template owner: a step class looser than the project's privacy pauses the run with `data_class_forbidden` unless the
template owner is the project owner or an admin. A template-wide auto-review switch is not exposed; review modes are
set per step.

Run `uv run pytest -q`. To include the engine's sequential, rejection, cancellation, rollback and concurrency
tests, set `TBR_TEST_DATABASE_URL` to a disposable MariaDB database ending in `_test`.
