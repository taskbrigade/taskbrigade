# Workflow event triggers

Event triggers start the current published version of a template when something else has already happened.
The producer only writes a row to `workflow_events`. The same scheduler tick that runs schedules
(`workflows.tick`) delivers that outbox. A failure while starting a workflow never fails the dataset import
or the run that just finished.

Enable **Admin → Settings → Workflows → Workflows enabled**. Human admins, project owners and template owners
open **Workflows → Event triggers** or **Event triggers** on the template (`/workflows/PROJ/key/triggers`).
Agents cannot create a trigger. Project members can view the list. Parameters of a data class you may not
read are masked.

## Events

| Kind | When it is written | Placeholders |
|---|---|---|
| `dataset_ready` | A dataset status becomes `ready` (profile release, or a reviewed script pin that finishes profiling) | `event.dataset` (numeric id), `event.dataset_key`, `event.filename`, `event.filename_date`, `event.sftp_account`, `event.supersedes`, `event.project` |
| `workflow_succeeded` | A run reaches `succeeded` | `event.run.params.<name>`, `event.run.steps.<key>.output.<name>`, `event.run.workflow`, `event.run.key` |
| `workflow_failed` | A step failure pauses the run with no retry left (the run stays `paused`, so a human can still resume) | same run fields |

`event.filename_date` is the first date in the original filename (`Buchungen_2026-09.xlsx` → `2026-09`,
`export_20260915.csv` → `2026-09-15`). There is no value when the name has no date.

Manual pause does not write `workflow_failed`. Cancelling a run does not either. Each dataset and each
terminal outcome is written once (`dedupe_key`). Sealed and local-only step outputs are omitted from the
event, so they are not copied into another run's stored parameters.

A dataset event stays in the dataset's project. A workflow-end event can start a template in another project
only when the filter names that project or a `PROJ/key` workflow, and the template owner may read the source
project. The run's `trigger_kind` is `event` or `workflow`, `started_by` is empty, and `parent_run_id` points
at the finished run for workflow-end triggers.

## Filters and parameters

`filter_json` is an object. An empty object matches every event of that kind in the template's project.
Allowed keys:

- `project` — project key
- `sftp_account` — SFTP inbox username (web uploads do not match)
- `name_glob` — case-sensitive glob on the original filename (`Buchungen_*.xlsx`)
- `supersedes` — dataset key this upload replaces
- `workflow` — `key` or `PROJ/key` of the finished workflow

`params_json` maps those placeholders onto the template's parameters, for example
`{"datensatz":"{{event.dataset}}","month":"{{event.filename_date}}"}`. The same checks as a manual start apply.
A paused or archived template does not fire. If a run of the target template is already active, the delivery
is `skipped_overlap` (event triggers do not queue). The tick still marks the event processed.

## Exactly once

`workflow_trigger_fires` has a unique `(trigger_id, event_id)`. Processing an event again, including after
`processed_at` is cleared, does not start a second run. Each event is committed on its own. One broken
trigger does not stop the others; its delivery is `failed` and is not retried. Fixing the trigger does not
replay events that were already processed.

The tick holds `GET_LOCK('tbr_workflow_tick', 0)` for schedules and events together. At most 500 outbox rows
are delivered per tick; the rest wait for the next minute.
