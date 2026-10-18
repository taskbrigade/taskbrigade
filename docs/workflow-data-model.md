# Workflow data model, stage 1

The workflow foundation stores definitions and execution history in seven tables:

| Table | Purpose |
|---|---|
| `workflow_templates` | Project-scoped key, owner, status, current version and budget defaults |
| `workflow_template_versions` | Numbered definitions, publication metadata and definition hash |
| `workflow_steps` | Ordered task templates, assignment, review, data class and output contracts |
| `workflow_params` | Typed parameters and default expressions for a version |
| `workflow_role_bindings` | Ranked actors for a role in a project |
| `workflow_runs` | Execution pinned to a version, resolved parameters, status and budget counters |
| `workflow_run_steps` | Step attempts, generated task, output and timing/error metadata |

SQLAlchemy models live in `app/models.py`; only Alembic creates the schema. Keys use unsigned integers,
timestamps use naive UTC, and tables use InnoDB with `utf8mb4_unicode_ci`. Unique constraints protect template
keys within a project, version numbers within a template, step keys and positions within a version, parameter
names within a version, role memberships, run sequence numbers and step attempts.

Runs reference a specific version rather than the template's mutable current-version pointer. Foreign keys
prevent deleting versions used by runs and steps used by run history. Deleting an unused version cascades to
its steps and parameters. The template/current-version and task/run-step cycles have named foreign keys;
the migration adds those after creating the target tables and removes them before dropping tables.

Tasks have three new columns:

- `workflow_run_step_id`: nullable, indexed reference to the workflow step attempt.
- `task_kind`: `work`, `profile`, `run` or `approval`, with `work` as the default for existing and new tasks.
- `merge_commit_sha`: nullable SHA of the commit created by merging the task's pull request.

Approval reads `merge_commit_sha` from the merged PR in Gitea before recording the task as done. It keeps
`result_commit_sha` as the submitted commit and includes both values in task API responses. Repository changes
that apply brain/changelog fragments happen afterwards, so they do not change which commit is recorded.
If reading the merged PR fails, approval remains in review; retrying can recover the SHA from the already
merged PR. Existing completed tasks keep a null merge SHA; historical backfill is outside this stage.

These tables hold persistence only. The [run engine](workflow-engine.md) adds ordered activation, role resolution,
run controls and workflow task rules; publishing validation (one draft per template, immutable published
definitions) is described in [workflow-outputs.md → Publish checks](workflow-outputs.md#publish-checks). Overview of
all workflow features: [workflows.md](workflows.md).

Later migrations add the rest of the model: `workflow_schedules`, `workflow_schedule_fires`, `workflow_calendars`
and `workflow_calendar_days` (`0026`, with `workflow_runs.schedule_id` and `UNIQUE (schedule_id, scheduled_for)`),
the error-handling state (`0027`, below), the runner fields of tasks (`0028`), `task_usage` (`0031`),
`workflow_script_pins` (`0035`), `workflow_events`, `workflow_triggers` and `workflow_trigger_fires` (`0036`, with
`workflow_runs.trigger_id`) and step conditions and parallel groups (`0037`). Role bindings have no UI, API or CLI
yet; rows are inserted directly (`project_id`, `role`, `actor_id`, `rank`, lowest rank first).

Migration `0027` adds durable error-handling state to each run-step attempt: `work_us` and a microsecond
`work_started_at`, wait/idle start times, reminder timestamps, and the attempt's `assignee_id`. The existing
`deadline_at` and `next_retry_at` fields hold the projected working deadline and scheduled recovery time.
See [Workflow error handling](workflow-errors.md) for timing rules, upgrade behavior and retry ownership.

Run `uv run pytest -q` for tests. Set `TBR_TEST_DATABASE_URL` to a disposable MariaDB database whose name ends
in `_test` to include schema, relationship, constraint and migration round-trip tests. The DB fixture deletes
all workflow data between tests.
