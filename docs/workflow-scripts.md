# Write once, run repeatedly

A `script` step creates reviewed code for recurring analysis. It requires enabled data zones, a
`cloud_ok` class (explicit or inherited), an agent assignee or role, and mandatory manual review. Put it after a sealed
runner profile step and name that step with `profile_from`:

```json
{
  "path": "analysis/month.py",
  "profile_from": "profile",
  "max_repairs_per_run": 1
}
```

The subsequent sealed runner step binds the script with:

```json
{
  "inputs": [{"ref": "{{steps.code.script}}", "purpose": "script"}],
  "config": {"run": {"dataset": "{{params.dataset}}"}}
}
```

Use `{{steps.code.script}}`, the built-in file reference, rather than an agent-declared output. The engine sets
its path and **merge commit**, never the submitted branch commit. A manual gate must still follow the analysis
runner before a cloud step consumes `{{steps.execute.result}}`.

On the first run, the local profiler generates a schema and synthetic sample. The workflow waits for normal
human profile release, including the sealed profile's four-eyes requirement. The engine then creates the code
task with the released profile reference, inline schema/sample (or scoped API links for large profiles), target
path and the runner script contract in TASK.md. Workflow profile release creates no additional ad-hoc draft.
A human review and merge establishes a `workflow_script_pins` row and a `script_pinned` data audit entry.
Human status overrides without a merge SHA cannot create a pin.

On later runs, the profiler still runs. The server compares structural fingerprints locally. If the fingerprint
matches, the script step completes without an agent task, and the profile task completes **without releasing
the profile**. Its schema and sample remain inaccessible to cloud agents. Previously approved aliases can be
reused only when the exact original header shape and the aliased structural fingerprint still match. The
runner receives those approved aliases and checks the workflow's schema again when fetching its job.

Pins belong to the template and step key, and can survive publication of a new version with the same path.
Changing the path requires new reviewed code. **Recreate script** on the workflow page retires its pin for future
activations; it does not rewrite completed runs. Each script attempt stores the actual path and commit used,
so old runs remain reproducible after a new pin replaces the old one. The API equivalent is
`POST /api/v1/workflows/{project}/{key}/scripts/{step_key}/recreate` (human workflow editor only).

## Schema changes and repairs

A changed fingerprint waits for human release of the new profile before creating a repair task. The task
uses the script step's original assignment rule, follows the pinned code task and references both released
profiles. Its structural diff lists added/removed columns and changed types/precision, excluding cell values,
distributions and raw logs. Large diffs are available in the script attempt's `outputs.schema_diff` through the
linked run API, keeping TASK.md within its size limit. A repair merge retires the previous pin, creates a new pin and continues with the
same run parameters.

Exit code **3** means schema mismatch. Failed automatic output-gate checks also request repair. Both create
fresh attempts starting at the producing script step, then new runner jobs/results; historical attempts remain
visible. A generated profile that was kept local during reuse must be released before this repair reaches an
agent. The repair task receives only check numbers and statuses from failed checks, never quarantined output.

At most one repair task may remain open for a pin across concurrent runs (`script_repair_open`). The default
automatic repair limit is one per run (`script_repair_limit`); configure `max_repairs_per_run` from 0 to 254.
Further failures pause the workflow. Repeated ticks and stale task events never create duplicate repairs.
Other exit codes, memory failures and timeouts retain the normal configured retry policy and then pause.
An authorized human can choose **Create repair task** on a paused run, or call
`POST /api/v1/workflow-runs/{id}/repair`, to explicitly request repair after investigating the failure.
Manual repair can exceed the automatic per-run limit, but cannot create a second open repair for the same pin.
Manual pauses defer repair activation and all subsequent work until resume.

For the DATEV example, run one profiles `Kostenstelle`, releases the profile, merges `analysis/month.py`, runs
the pinned commit, releases its aggregate and writes the report. Run two with the same schema skips the code
task and profile release. When `Kostenstelle` becomes `KST`, the next run requests profile release and one repair
with `Kostenstelle` removed and `KST` added, then executes the new reviewed commit and waits at the output gate.
