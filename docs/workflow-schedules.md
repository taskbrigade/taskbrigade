# Workflow schedules and calendars

A schedule starts the template's current published version as its owner, with `started_by = NULL`.
Existing and queued runs keep their version, original `scheduled_for` (UTC), resolved parameters and schedule
time zone. `{{run.period}}` means the previous month of that original slot in the schedule's time zone.

Enable **Admin → Settings → Workflows → Workflows enabled**, then open **Workflows** in the navigation.
Choose **Manage schedules** for a template. Human admins, project owners and template owners can save schedules;
project members can view them. Agents cannot create a permanent schedule. **Preview next runs** calculates five
upcoming dates with offsets without writing a schedule or creating runs. **Save schedule** resets the cursor to
now, so editing or enabling a schedule does not replay the new rule's past slots. Disable the global setting to
pause ticks while retaining cursors; re-enabling it applies each schedule's misfire policy to the backlog.

The template schedule page is `/workflows/PROJ/key/schedules`; `/workflows/schedules` lists visible templates and
schedules. Existing schedules can be disabled with their **Enabled** checkbox. Recent fires show each attempted
slot, including skips and failures. **Admin → Settings** and the editor show the last tick and warn after five
minutes without one. `UI_LANGUAGE=de` selects the German UI; the browser language switch still takes precedence.

## Rules

Cron uses five fields: minute, hour, day of month, month, weekday. Use `mon` through `sun` for clarity;
APScheduler's numeric weekdays start with Monday = 0. Example: `0 7 * * mon-fri`.
A selected calendar filters cron dates; **Holiday shift (cron)** chooses skip, next workday or previous workday.
Shifted slots landing at the same instant are one distinct slot.

For **Workday**, choose **Nth workday** (negative numbers count from the end) or **Day of month**,
then set the period, local time and date shift. Day-of-month mode requires the month period.
The stored rules have these forms:

```json
{"nth": 3, "of": "month", "time": "07:00"}
{"nth": -1, "of": "month", "time": "16:00"}
{"nth": -2, "of": "month", "time": "09:00"}
{"day": 15, "of": "month", "shift": "next_workday", "time": "08:00"}
{"nth": 1, "of": "week", "time": "06:30"}
{"nth": 5, "of": "quarter", "time": "07:00"}
```

Weeks start on Monday; quarters start in January, April, July and October. An absent day of month (such as
February 31) has no slot. Without a calendar, workdays are Monday–Friday. With a calendar, workdays exclude public
holidays for its country and subdivision, using the locked `holidays` dependency. Explicit `open` days override
holidays and weekends; explicit `closed` days override everything else. Shifts search at most one year for a
workday; impossible cron rules have no next occurrence within an eight-year search horizon.

For the third workday in DE-BW, the next example months are 5 October 2026, 4 November 2026 and 7 January 2027.
The January date is 6 January in DE-NW because Epiphany is a holiday in Baden-Württemberg.

The time zone defaults to `TZ_DISPLAY` (`Europe/Berlin`). Fixed local times in a DST gap run at the first valid
minute (02:30 becomes 03:00 on Berlin's spring switch). Repeated local times run once at their first occurrence
(`fold=0`). Hourly cron (`0 * * * *`, also `*/1` in the hour field) follows UTC intervals and has 23 or 25 slots
on switch days. All database timestamps are naive UTC; previews display local time and its UTC offset.

**Parameters (JSON)** contains named parameters or placeholder expressions, for example
`{"month":"{{run.period}}"}`. The same validation and defaults as manual starts apply; dataset IDs are checked
against the workflow project and template owner's data rights. The schedule time zone is stored on the run so an edit cannot change its period.

## Overlap and missed slots

An active run is queued, running, waiting **or paused**, across all schedules and manual starts of a template.
**Skip** records `skipped_overlap`. **Queue (at most one waiting run)** creates one queued run; later slots are
skipped until the queue clears. Once the predecessor is terminal, tick activates the queued run. A skip sends
at most one owner notice per template and UTC day, including when multiple schedules overlap.

Manual/API starts return `409 workflow_overlap` when a run is active. Retry with `--queue` (`queue: true` in the
API) to enqueue. A human admin can explicitly use `--force-parallel` (`force_parallel: true`); this is audit logged.
The scheduler never forces parallel execution.

A slot is missed when its delay is greater than **Misfire grace (minutes)** (default 360). Within grace it runs
normally. **Run the latest missed slot once** replays only the newest missed slot, preserving its original
scheduled time. Older missed slots get `skipped_misfire`. **Skip** skips all missed slots. Every slot has an audit
record, even when skipped. Very large backlogs are processed in batches of up to 10,000 slots per schedule per
tick and continue from the saved cursor on the next tick.

## Operator commands and tick

```sh
tbr-admin workflows calendar add DE-BW --country DE --subdivision BW
tbr-admin workflows calendar check DE-BW 2027-01
tbr-admin workflows calendar check 1 2027-01
tbr-admin workflows calendar day 1 2026-12-24 closed --note "Company closure"
tbr-admin workflows calendar day 1 2027-01-02 open --note "Inventory"
tbr-admin workflows calendar day 1 2027-01-02 remove
tbr-admin workflows schedules --next 5
tbr-admin workflows tick
tbr-admin workflows tick --now 2026-11-04T07:00:00+01:00
```

`calendar add` prints its database ID; `calendar day` uses that ID. `calendar check` accepts a stored ID (including
company exceptions) or a country-subdivision code (public holidays only). Exception edits recalculate affected
schedules from now, without replaying the changed calendar's history. An offset-free `--now` means UTC.

Tick requires a ready database at the current Alembic revision. Its independent MariaDB connection holds
`GET_LOCK('tbr_workflow_tick', 0)` until processing finishes, even across commits. A second trigger immediately
returns `{"skipped":"locked"}`. Both fires and runs have unique `(schedule_id, scheduled_for)` keys.
Each slot commits independently; a failing schedule does not block others and notifies its owner without exposing
parameter values. Queued activation and terminal-task progress reconciliation also commit independently.
The service entry point is `app.services.workflows.tick.tick(session, now)`. It owns the supplied session's
transactions; callers must use a clean session. The CLI prints JSON and exits nonzero if any operation failed.
`POST /api/v1/workflows/tick` requires both a loopback peer and an admin token.

`TBR_WORKFLOW_TICK=timer|app|off` controls the automatic trigger. Native installations default to `timer`;
the Docker image defaults to `app`, which calls the same service each minute. `off` disables the app trigger;
manual CLI/API invocations remain available. The native installation runs the tick through the systemd units
`taskbrigade-workflows.{service,timer}`; `deploy/update.sh` stops the timer around the schema upgrade.
Workflows stay disabled by default in this stage.
The same tick processes [working-time timeouts, retries and reminders](workflow-errors.md) and
[event triggers](workflow-events.md).
