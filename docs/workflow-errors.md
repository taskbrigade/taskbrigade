# Workflow error handling

The minutely workflow tick processes timeouts, retries and reminders alongside schedules. Enable
**Admin → Settings → Workflows → Workflows enabled** and configure the normal workflow tick trigger
([scheduler operations](workflow-schedules.md)). Disabling workflows freezes automated recovery and reminders;
ordinary task actions still work. A failed step pauses its run when recovery is exhausted or impossible.

## Working-time timeouts

A step's **Timeout (minutes)** counts only the task's `in_progress` intervals. The default is 24 hours for
`agent` and 48 hours for `script`. Time in `defined`, `draft`, `questions`, `changes_requested` or `for_review`
does not count. Explicit background waiting while `in_progress` does count. Questions, release/reclaim,
review and reassignment update the clock through the normal task state hooks. Pausing the run leaves running
tasks working, so their work clock continues too.

On timeout, the engine writes a system comment, returns the task to `defined`, clears its lease and background
waiting, and fails the attempt with `timeout`. The task cannot be claimed while its attempt awaits recovery,
even though it is defined. The run either schedules a retry or pauses and notifies its owner and starter.
**Working-time deadline** appears on the run detail while work is in progress; it disappears during waits.

Clocks are stored durably with microsecond precision and survive service restarts. Migration 0027 starts clocks
for already active work at upgrade time; earlier work is not retroactively charged because pre-upgrade tasks
did not maintain working-time clocks. All new attempts measure from their first claim.

## Retries

Set **Retries** and **Retry backoff (seconds)** on each step in the template editor. The API fields are
`max_retries` and `retry_backoff_s`. Agent steps default to zero retries; the backoff defaults to 300 seconds.
Retry 1 waits 300 seconds, retry 2 waits 600 seconds, and subsequent delays double. `Next retry` is visible on
the failed attempt and exposed as `next_retry_at` in run detail responses. Repeated ticks do not create duplicate
attempts or notifications.

For `agent`/`script`, only `aborted` and a working-time timeout retry automatically, and only with an
`agent_role`. The next eligible candidate is chosen by rank, load and actor ID, excluding previously attempted
agents and the task's reviewer. Membership, activity, owner visibility, data class and available capacity are
checked again when the retry is due. No eligible candidate pauses with `no_assignee`. Fixed assignments pause
for a human decision rather than silently assigning outside their rule.

An automatic agent retry creates a new run-step attempt and reassigns the **same task**, preserving its branch,
PR and discussion through `reset_for_reassignment`. It clears the old lease and starts with a fresh work clock.
A concrete model that the new agent cannot use is dropped through the ordinary reassignment rule. Old attempts
remain failed and cannot advance the run. Cancelling the task while a retry is pending cancels that recovery.

Cancellation, assignment/placeholder errors, rejected approvals, schema errors and failed output checks never
retry automatically. Notify steps keep the existing notification queue's delivery retries.

Runner retry policy uses a new attempt and normal activation, which creates a fresh task/job rather than
reopening a job with existing results. Runner drafts default to two retries; explicit zero is preserved.
Runner and gate activation use the data-zone services; cancelled/superseded jobs stop at the next runner heartbeat.
Script pins and mapping exit code 3 to schema repair remain WF 11. That integration should call
`workflows.errors.fail` with an explicit schema/check error instead of `task_aborted` for non-retryable failures.
The runner's timeout remains the sandbox runtime limit, not the agent working-time clock.

## Human and claim reminders

Approval and gate steps have no timeout. An approval, gate, manual review or question gets its first reminder after
**Wait reminder (hours)** (`wait_reminder_hours`, default 24), then at most once every 24 hours while the same
wait continues. Resolving a wait resets the reminder interval. There is no automatic rejection or cancellation.
After **Workflow waiting warning (days)** (`workflow_paused_max_days`, default 30), run history and detail show a warning;
it still waits for a human decision.

A task left `defined` for **Workflow claim reminder (hours)** (`claim_reminder_hours`, default 4) sends one
owner notice per unclaimed interval. Add `"reassign_on_idle": true` to the step's settings JSON to try one
alternative role candidate at that point. Without an eligible alternative, the assignment is retained. The
attempt and task stay the same, and repeated ticks cannot cycle between candidates. Claiming and subsequently
releasing the task begins a new unclaimed interval.

## Notifications and run controls

**Workflow paused**, **Workflow waiting for a decision** and **Workflow task has not been claimed** appear in
the normal per-user notification settings, including Telegram/email channels, mute and quiet hours. Pause and
human-wait notices target the template owner and human starter (deduplicated); claim notices target the owner.
All include a link to `/workflow-runs/<id>`. Restricted projects/runs include only run number, event and link,
never template names, task text, parameters or outputs.

A final failure leaves the run paused. **Resume** defaults to its failed step with a new attempt and the pinned
version. Choosing **Resume from** an earlier step resets later attempts and cancels their open tasks. Optional
steps may be skipped; approvals and gates cannot. Manual pause stops new activations, retries and reminders;
already active tasks may finish, and their successors wait for resume. **Cancel run** clears pending retry
schedules and cancels open tasks with comments, preserving completed attempts. A corrected template is used by a
new run, not by changing the version of an existing run.

The run lock and the scheduler's MariaDB named lock protect recovery. Retry/timeout changes, outbox notices and
reminder timestamps commit in one transaction; a failure rolls them back together. Tests use a disposable
`*_test` MariaDB database: `uv run pytest -q tests/test_workflow_errors.py`.
