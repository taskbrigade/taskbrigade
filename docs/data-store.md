# Data store

The data store keeps confidential tabular files (for example accounting exports) **outside Gitea**, encrypted at rest.
Cloud agents never see a dataset's name or contents; tasks refer to a dataset only by its key (`DS-0003`). The
schema profiler runs in the local runner sandbox; a human releases its schema and synthetic sample before cloud use
([overview of the data zones](data-zones.md)); runner results leave zone R only through the
[output gate](#output-gate). UI labels below are the
English ones; with `UI_LANGUAGE=de` the menu item is called *Daten*.

## Switching it on

1. Create the data directory (only ciphertext is ever written there) and the key:

   ```sh
   sudo install -d -o taskbrigade -g taskbrigade -m 0750 /srv/taskbrigade-data
   sudo -u taskbrigade tbr-admin init-data-key        # or as root, then chgrp taskbrigade + chmod 0640
   ```

   Paths come from `TBR_DATA_DIR` (default `/srv/taskbrigade-data`) and `TBR_DATA_KEY_FILE`
   (default `/etc/taskbrigade/data.key`); set them with `tbr-admin config set`. With `ProtectSystem=strict` the
   service also needs `ReadWritePaths=/srv/taskbrigade-data` (a drop-in for `taskbrigade.service`) until the
   installation routines take care of it.
2. *Admin → Settings*: tick **Data store enabled (menu Data: encrypted dataset upload)**. The same section holds the
   maximum upload size (`data_max_upload_mb`, default 200) and the default retention (`data_retention_days`,
   default 90).
3. *Admin → Users & agents → \<human\>*: grant the **Data right** to project members who may upload datasets and work
   on `sealed` tasks.

Step 1 is what `deploy/install.sh`/`deploy/update.sh` do for you when `TBR_DATA_ZONES=1` is passed: they create the
directory, the key and the `ReadWritePaths`/`SupplementaryGroups` drop-in on `taskbrigade.service`
([operations.md](operations.md#data-zones-installation-and-migration)). Do the manual step only on an installation
that leaves the data-zone routines off, or to repair a missing key.

Docker installations: the compose profile `data` creates the volumes, the key and the runner
([docker.md → Data zones](docker.md#data-zones-profiles-data-and-data-sftp)).

## Key handling

- **KEK** (key encryption key): 32 random bytes in `TBR_DATA_KEY_FILE`, written by `tbr-admin init-data-key` as one
  base64 line with mode `0640`. It is separate from `master.key` (Gitea and SFTP secrets), so a leak of one does not
  open the other. The server refuses a key file that other users can read. The command never overwrites an existing
  key: a new key makes every stored dataset unreadable.
- **DEK** (data encryption key): 32 random bytes per dataset. Only the DEK wrapped with the KEK is stored
  (`datasets.dek_wrapped`, AES-256-GCM, bound to the dataset key and tagged with a fingerprint of the KEK). A dataset
  encrypted under another KEK is reported as such instead of producing garbage.
- **Files**: AES-256-GCM in records of 1 MiB. The associated data of each record holds the file header, the record
  number and a final-record flag, so modified, reordered, dropped, truncated or appended records fail to decrypt.
  The header carries a format version and a random file id; the binding to the dataset is the wrapped DEK.
- **Backups**: back the KEK up **separately** from the database and from `TBR_DATA_DIR` – never in the same backup.
  Without the KEK, neither the database nor the store files reveal any data. Rotating the KEK is not implemented
  yet (re-wrapping all DEKs).
- Recommended in addition: put `TBR_DATA_DIR` on a LUKS volume. An NAS mount works too, because only ciphertext
  is stored there.

## Upload

*Data → + Upload* (owner, admins, project members with the data right). The browser streams the file; the server
encrypts each chunk as it arrives and writes only ciphertext to `TBR_DATA_DIR/incoming` – no plaintext temporary
file, also not in `/tmp`. When the upload is complete, the dataset row is created, the DEK is wrapped and the file is
moved to `store/DS-<id>.enc`. CSV/TSV (UTF-8, optional BOM) and XLSX uploads automatically create a sealed `profile` task and enter
*Profiling*. The runner must be a member of the project to claim the unassigned task. Other formats stay
*Stored – awaits profile* (`quarantine`); no parser runs in the web process.
A file with the same SHA-256 as another dataset of the project is stored and reported as a duplicate. *Replaces*
links a new version and archives the old dataset.

The form must keep the file input as its last field: the server checks the CSRF token and the upload right before
the first byte of the file is written.

## Schema profiles and release

Web uploads and SFTP imports use the same automatic profiling path. The runner decrypts the input into its job
tmpfs and runs the bundled trusted profiler through `taskbrigade-job@.service`, with the same network, filesystem,
memory and time limits as analysis scripts. It never fetches profiler code from a project repository. XLSX parsing
requires `defusedxml` and caps expanded ZIP data at 512 MiB, archive entries at 10,000, sheets at 50, rows per sheet
at 200,000 and columns at 500. Invalid, ambiguous or unsupported tables fail closed. Parser failures leave encrypted
logs in quarantine and abort the task without putting source values in its comments.

The profiler emits `schema.json` (`tbr-schema/1`), `sample.csv` (20 rows per sheet, in schema order) and `result.json`
(profiler version and SHA-256 schema fingerprint). Numeric ranges are rounded outwards by order of magnitude;
dates show months. Categories require cardinality ≤20 and at least `min_group_size` occurrences (default 5);
infrequent categories become `(rare)`. Suspected identifiers/PII expose no values. High-cardinality strings are
identifiers; numeric/date measures retain rounded ranges. Samples use deterministic synthetic placeholders rather
than copying even approved category values. Every nonempty source data cell is excluded from synthetic rows, including equivalent numeric
and date representations. When a small numeric/date range is exhausted, synthetic values can extend beyond it to
preserve this guarantee. There is no Faker dependency.

`dataset_profiles` stores generated profiles locally. The fingerprint covers sheet/column order, names, types and
numeric precision; changing row counts, values, null rates, cardinalities or inferred PII/category roles alone does
not change it. Aliases do change it, since scripts use the released names.

Open *Data → dataset → Schema profile*. Only the human owner, human admins and project members with the data right
see unreleased profiles or their controls. Review the schema statistics and synthetic sample, especially **column
and sheet names**, which can themselves be confidential. Set aliases, then **Release profile and propose code
task**, or **Reject profile**. The form identifies the exact reviewed profile, so a stale page cannot release a newer
one. Decisions lock the dataset/profile and are written to the hash-chained data audit. For `sealed` datasets
the uploader cannot release their own profile while the project's four-eyes rule is on (the same option as for the
[output gate](#output-gate), default on). Local-only profiles may be released by their uploader.
Release sets the profile to `released`, the dataset to *Ready*, completes the profile task and creates a **draft**
cloud code task containing only the approved schema/sample and fingerprint – the first follow-up draft of the
[task chain across zones](#task-chain-across-zones). Rejection sets `rejected`, returns the dataset to quarantine and the
profile task to `changes_requested` for a new run. There is no agent-token release endpoint.

`GET /api/v1/data/profiles/<id>` exposes a generated profile only to authorized humans; released profiles are also
readable by human project members. An agent can read a released profile only through an open task of its own that
references it (`task_data_refs`, see [task chain across zones](#task-chain-across-zones));
`dataset_profiles.code_task_id` still names the proposed code task. The approved artifacts are also inline in the
draft's description when they fit its 60,000-byte handoff budget; larger profiles use the scoped API link instead. Each ingested
profile artifact is limited to 4 MiB. Alias mappings use MEDIUMTEXT to support wide workbooks. Originals and alias
mappings are never included in the profile response or draft. On subsequent analysis runs,
the runner applies aliases in the sandbox to a private temporary copy of the CSV/XLSX input; the encrypted dataset
and read-only source mount stay intact. A header mismatch exits with code 3.

## Rights

| Action | Who |
|---|---|
| See the list and metadata (name, size, status; no contents) | project members, owner, admins |
| Upload | owner, admins, project members with the data right |
| Delete | owner, admins |
| Download raw data (emergency) | admins with the data right; a reason is required and audited |
| Read the contents (agents) | a local-model agent (`model_location=local`) that is a member of the dataset's project, and only for datasets of class `local_only` |

The menu item *Data* appears when the data store is enabled and the human has the data right, owns a project or is
an admin. Tasks with the class `sealed` may be assigned only to runners and to humans with the data right.

## Agent read access

A local-model agent may read the contents of a `local_only` dataset over `GET /api/v1/data/datasets/<key>/content`
(the file streams decrypted, `Content-Disposition: attachment`). The right is checked with the zone helper
(`zones.may_read_dataset`): only agents whose model runs locally (`model_location=local`, and `runtime=local` once
`privacy_enforce_model_location` is on) may read `local_only`; cloud-model agents are refused, and `sealed` datasets
are never readable by an LLM agent. The agent must be a member of the dataset's project. Every read is written to
the data audit (`dataset_read`) before the first decrypted byte leaves.

## SFTP inbox (native)

The optional SFTP inbox lets specialised programs (accounting exports, ERP dumps) deliver large files directly
into the encrypted store, without a browser. It is part of the data zones and needs `TBR_DATA_ZONES=1`; switch it
on with `TBR_DATA_SFTP=1` and re-run the install/update script ([operations.md](operations.md#data-zones-installation-and-migration)).
It is a **different** facility from the *SFTP storage for agent results* (`sftp_*` settings, `tbr sftp`), which
only stores agent output on a foreign SFTP server.

**How it works.** OpenSSH `internal-sftp` with a per-user chroot serves the inbox; the drop-in
`deploy/ssh/60-taskbrigade-sftp.conf` forces SFTP, forbids shells and forwarding and allows keys only. Every
deliverer is a system user (`--system --shell /usr/sbin/nologin`) in the group `tbr-sftp`, chrooted to
`<TBR_SFTP_DIR>/<user>` (root-owned, mode `0755`) with a single writable `upload/` folder (`<user>:tbr-data`,
mode `0730`). A systemd path unit (`taskbrigade-data-import.path`) watches the upload folders and starts
`tbr-admin data sftp-user import`, which encrypts every finished file into the store, writes the audit entry
(`dataset_uploaded`, `via=sftp`), removes the plaintext and notes the delivery on the account.

**Create a deliverer** (on the app server, as root):

```sh
sudo tbr-admin data sftp-user add demo-export --project DEMO --key ~/id_ed25519.pub \
     --name "Accounting export"
tbr-admin data sftp-user list                 # users, project, keys
sudo tbr-admin data sftp-user list --pending  # files waiting in the upload folders
sudo tbr-admin data sftp-user key add demo-export ~/id_ed25519_2.pub
tbr-admin data sftp-user key list demo-export
sudo tbr-admin data sftp-user remove demo-export --purge   # also deletes the system user and the chroot
```

The command creates the system user, the chroot folder and `authorized_keys`. **Remove** without `--purge` only
drops the database row; the account and folder stay until `--purge` (or a manual cleanup) removes them. `key
add`/`key remove` rewrite `authorized_keys` at once; `tbr-admin data sftp-user provision` re-creates a missing
system user, folder or key file from the database (for example after an OS reinstall).

The project key is the mapping `user → project`; a deliverer therefore belongs to exactly one project and a
second project needs a second account.

**What the deliverer may do.** Log in with its key and upload into `upload/` – nothing else. There is no shell,
no port forwarding, no X11, and it cannot read another deliverer's folder or the store. The inbox is only as safe
as the network it is reachable from, so keep it in the LAN or behind WireGuard/Tailscale and use keys only
(password login is off in the drop-in).

**Import.** A file counts as finished when it has not changed for 60 s or its name ends in `.done`/`.ready`
(the suffix is stripped from the dataset name). The import runs under the database lock `tbr_data_import`, so a
path-unit trigger and a timer never overlap. It refuses to run while the data store is off (`data_enabled`), and
it never touches a file it could not import (empty file, too large, an import error) – the next run retries. The
imported dataset lands in the store exactly like a web upload: class `sealed`, status *Stored – awaits profile*,
retention from `data_retention_days`, a duplicate detected by SHA-256, and the project's recipients get the
usual `dataset_uploaded` notification. Running the import by hand:

```sh
sudo tbr-admin data sftp-user import
```

A systemd timer is a fallback if the host has no path-unit support; the path unit is the normal trigger.

**Deviations from the concept:** the design record (`research/konzept-datenzonen.md`
§4) lists the same drop-in and folder layout; TaskBrigade adds a
`last_import_at` timestamp per account and an extra `import` sub-command so the path unit and a manual/timer run
share one code path. `restrict,command="…/sftp-server"` is written into each `authorized_keys` line in addition
to `ForceCommand internal-sftp`, so a key alone cannot open anything but SFTP even if the drop-in is missing.

## Output gate

A run task's outputs ([data-zone runner](operations.md#data-zone-runner)) wait encrypted in
`TBR_DATA_DIR/quarantine`. Before anyone decides, automatic checks run in this order; the first failed hard check
stops, warnings go to the person who decides:

| # | Check | Rule | Kind |
|---|---|---|---|
| 1 | Format | `result.json` in `tbr-aggregate/1`, nothing else in `out/`; cells are plain values (no nested lists/objects), `metrics` maps names to numbers, unknown keys are refused | hard |
| 2 | Size | ≤ 1 MiB, ≤ 20 tables, ≤ `gate_max_rows` rows per table (default 1000) | hard |
| 3 | Group size | every table names a `count_column`; every count is a whole number ≥ k (`gate_min_group_size`, default 5; the dataset's *Min. group size* wins) | hard |
| 4 | No identifier values | no string cell, column name, table name or metric key equals a value of an identifier column of the input | hard |
| 5 | No raw rows | no output row reproduces an input row | hard |
| 6 | Free text | strings longer than 64 characters or with 8+ digits in a row | warning |
| 7 | Dominance | for sums declared with `max_contribution`: the largest single entry > `gate_dominance_pct` (default 85 %) of the group's sum | warning (hard with `gate_dominance_hard`) |
| 8 | Differencing | a group differs from the same group of a released result of the same dataset by fewer than k | warning |
| 9 | Precision | numbers with decimals are rounded to two places on release | transformation |

Checks 4 and 5 need the input, so they run in a **separate sandbox job** right after the script's job (the probe
`app/runner/gate_probe.py`, TaskBrigade's own code): the spreadsheet is parsed without network and without access to
the key or the store, never in the runner process that holds the key. Identifier columns are columns whose name
looks personal (name, customer, client, patient, email, IBAN, phone, …), whose values mostly look like an IBAN,
email address, phone number or tax id, or text columns whose distinct values exceed half the rows; dates and numbers
are never identifiers by cardinality. Comparisons ignore case, spacing and separators and also look at runs of up to
four words inside longer strings; rows are compared as sets of normalised cells (reordered columns match, `12.50`
equals `12.5`; for rows of six or more cells one missing cell still counts). The probe reports locations only
(`t0.r3.c1` = table 0, row 3, column 1), never values; if it fails or cannot read the input, checks 4 and 5 fail. CSV,
TSV, TXT, JSON, XLSX/XLSM (openpyxl) and Parquet (pyarrow) are read; other formats fail closed.

For check 7 a table declares, per sum column, a helper column with the largest single contribution:
`"max_contribution": {"total": "top_entry"}`. The helper column is checked and **removed** before release, so the
largest single value never leaves zone R.

**Statuses** (`run_results.status`): `quarantine` → `checks_failed` (a hard check failed: the run task goes to
*Changes requested* and its script commit is cleared, so the runner does not repeat it) or `pending_approval` →
`released`, `rejected` or `kept_sealed` (the result stays in zone R: the run task is done without a release, for
example a list the accounting team reads in the release view).

**Release view** (*Inbox → Data releases*, the task's *Output gate* box, the dataset's *Runs*; `/data/results/<id>`):
the people who may decide – project owner, admins and project members with the data right – see the check results,
the full output with flagged cells, the decrypted stdout/stderr (first 64 KiB), the script with its merge task and a
diff link against the last released run. Opening it is audited (`result_viewed`). Decisions:

- **Release – data leaves zone R**: the run task is done; `released_json` holds the released form (rounded, helper
  columns removed).
- **Reject** (reason required): the run task goes to *Changes requested*. Repair the script in a code task, then set
  the merged commit as the run task's run fields (`PATCH /api/v1/tasks/<nr>` with `run`) to run it again.
- **Keep in zone R**: also possible for a result whose checks failed.

Decisions are web-only: never through Telegram and never with an agent's API token. Telegram and email only say
"Result waits for release (output gate)" or "Output gate refused a result" with the task number and link.

**Four-eyes rule** (default on, *Admin → Settings → Output gate: four-eyes rule by default*; per project in the
project settings, changes audited as `four_eyes_changed`): the person who releases must neither have created the run
task nor merged its script. They can still reject or keep the result in zone R.

**Reading released results.** `GET /api/v1/data/results/<id>` returns the released form – only for `released`
results; every other status answers 404 for everyone. Humans must be project members; agents must hold an open task
(assignee or reviewer) that references the result (see [task chain across zones](#task-chain-across-zones)). Each
read is audited (`result_read`).

### Noise and query budgets

Noise is **off by default**, and both budgets default to unlimited. The human project owner or an admin can
open *Data → dataset → Configure output policy*. This web form requires CSRF protection; data rights alone do
not permit policy changes. Every change is recorded as `gate_policy_changed`, with the old and new settings.

| Setting | Meaning |
|---|---|
| Query limit | Maximum successful output releases for this dataset, including exact releases. Blank = unlimited; 0 blocks release. Integer, at most 2,147,483,647. |
| Epsilon per release | Positive ε charged for **one entire result**, across all tables and metrics. Blank together with sensitivity disables noise. |
| Total L1 sensitivity | Positive bound Δ on the sum of absolute numeric changes when adding/removing **one person's entire contribution**, for the whole output after two-decimal rounding and helper-column removal. Set together with ε. |
| Epsilon limit | Maximum cumulative ε for this dataset. Requires noise enabled. Blank = unlimited; 0 blocks release. |

Decimal settings accept up to six decimal places and values up to 10¹²; the smallest positive value is 0.000001.
The ratio sensitivity/epsilon must be at least 0.01 so noise remains meaningful at cent precision.
Use a small ε chosen for the actual privacy requirement, rather than treating the accepted upper bound as a
recommendation. For example, ε = 0.1, sensitivity = 2 and an epsilon limit of 0.3 allow three releases at Laplace
scale 20. A query limit of 2 would stop after the second release instead. Limits are independent and both apply.

**Reviewed-query contract.** A person must verify the sensitivity bound against the **merged script**, including
per-person contribution limits and clipping of unbounded amounts. A count of people has sensitivity 1 only when
each person can affect one released count by at most one; counts of rows or many overlapping groups usually need
a larger bound. For sums, bound amounts and the number of contributions **inside the script**, before aggregation.
The bound must cover the whole numeric vector, including redundant totals, every table and metrics, after the
gate's rounding. It is not a per-cell bound. The gate does not infer sensitivity from observed data, which would
itself disclose information, and it does not modify the script's contribution limits.

All labels, string values, table/metric/column names, row order, row count, numeric positions and null positions
must be fixed public information, independent of the confidential input. Include public empty groups when needed;
do not use data-dependent group discovery, top-k selection or a list of identifiers. The release view requires
an explicit confirmation of this contract under the currently displayed policy. If the policy changes while the
page is open, reload and review it again. Human review and all existing hard checks/four-eyes rules still apply.

**Mechanism.** After helper-column removal and rounding, independent Laplace noise with scale Δ/ε is added to
**every numeric cell, including counts, numeric category codes and metrics**. Use public strings for category
labels if their values should remain readable. Boolean cells are treated as 0/1 and noised as well. Noise uses
OS-backed `SystemRandom` and a four-uniform sampler
following Holohan & Braghin (2021), also described in the
[IBM diffprivlib Laplace mechanism](https://diffprivlib.readthedocs.io/en/latest/modules/mechanisms.html#diffprivlib.mechanisms.Laplace).
No seeds, noise samples or exact numeric outputs are recorded in the release metadata/audit. Output is rounded
again to two decimals. Counts can be fractional or negative; the k-check applies to the original counts before
noise. Numeric inputs above magnitude 10¹² and non-finite noisy outputs are refused.

The approval view clearly labels the pending output as **exact quarantine content** and shows ε, sensitivity,
release count, epsilon spent and both limits. Noise is sampled only when the human releases the result.
`released_json` stores that one noisy artifact; API reads, workflow handoffs, task drafts and result-mail exports
reuse it, with no fresh sampling. `release_policy_json` and the `result_released` audit entry record the applied
parameters and cumulative usage. The release view also shows the historical policy for an already released result.
Differencing against earlier exact releases still works; earlier noisy counts cannot be compared reliably, so
check 8 warns when those are the only otherwise unflagged history.

**Accounting.** The dataset row is locked and refreshed during release. Charging both budgets, saving the artifact,
the audit entry and the task transition share one transaction. Concurrent releases cannot spend the final allowance
twice, and rollback refunds it. Rejected, failed or kept-in-zone-R results and repeated reads spend nothing. A retry
of an already decided result spends nothing. Usage survives policy edits and artifact deletion; lowering a limit
below usage blocks further releases. Raising/disabling a limit requires an audited owner/admin change. Upgrade
backfills the release count from existing results (including retained release timestamps on deleted artifacts);
historical exact releases have no ε charge. Already removed historical rows cannot be reconstructed.

These controls account **per dataset ID**, not per person, across projects or across copies/versions of a dataset.
Re-uploading the same records creates another budget; operators must coordinate privacy spending across such
copies. Profiles, emergency downloads, local-model raw reads and exact releases outside the noise-enabled period
are outside epsilon accounting. Enabling noise cannot retroactively protect earlier disclosures. A cumulative ε
value describes the configured noise-enabled queries under the reviewed-query contract, **not a certified
end-to-end differential-privacy guarantee for arbitrary scripts or the whole gate**: gate accept/reject outcomes,
group suppression, metadata, human decisions and finite-precision implementation can also disclose information.
Evaluate the complete disclosure process using
[NIST SP 800-226](https://csrc.nist.gov/pubs/sp/800/226/final) before relying on a formal guarantee.

## Task chain across zones

A typical analysis crosses the zones several times ([walk-through](data-zones.md#a-walk-through)):

```text
DEMO-0101  profile  sealed    runner       → profile#7 (released by a human)
DEMO-0102  work     cloud_ok  cloud agent  refs: profile#7 (schema)   → PR with analysis/x.py
DEMO-0103  run      sealed    runner       refs: DS-0003 (input)      run_commit_sha = merge commit of 0102
                                                                      → result#12 (output gate → released)
DEMO-0104  work     cloud_ok  cloud agent  refs: result#12 (output)   interpretation
```

The order is the existing follow-up link (`follow_up_of`); the handover is a **reference** in `task_data_refs`
(task, `ref_type` `dataset`/`profile`/`result`, `ref_id`, `purpose` `input`/`schema`/`output`), never the content.
The task payload lists them as `data_refs` (type, id, label such as `DS-0003` or `result#12`, status).

**Rule 3 (class follows the references).** A task with an input dataset must be `sealed`; runner tasks get that
reference from their run fields (`run.dataset`, kept in sync with `tasks.run_dataset_id`, which the runner reads). A
`profile` or `result` reference in a task that an LLM may work on (`cloud_ok` or `local_only`) needs the artifact to
be `released` – stricter than the concept, which names zone C only, because local models are LLMs too and generated
profiles stay human-only. Adding a reference that breaks the rule answers `422 data_class_ref` /
`data_ref_unreleased`; so does loosening the class of a task whose references would break it.

**Follow-up drafts.** Every zone change creates a **draft** (created by the project owner, linked as follow-up), so a
human confirms each crossing before release:

| Event | Draft | Zone | Reference |
|---|---|---|---|
| Profile released (*Data → dataset*) | "Write analysis script for DS-…" | C | `profile` (schema) |
| Code task with a schema reference approved and merged | "Run script on DS-…" (`task_kind` run) | R | `dataset` (input); `run_commit_sha` = merge commit |
| Result released in the output gate | "Interpret result #…" | C | `result` (output) |

The run draft takes the script path from the merged pull request when it changed exactly one Python file outside the
tests; otherwise the box *Data* on the draft shows *Run fields* to fill in. A run task can only be set to *Defined*
with commit, script path and input dataset (`422 run_fields_incomplete`). Workflow tasks get no drafts – a workflow
run models its chain itself ([workflow engine](workflow-engine.md)). Cancel a draft that is not needed.

**Chain view.** Each task of such a chain shows the chain above its description – the same component as the
workflow run detail: zone badge, task kind, status, assignee and the handover to the next step (`profile#7 ·
Released`, `commit a1b2c3d`, `result#12 · Awaits release`). The box *Data* in the right column lists the references
(an input dataset is marked *runner only*) and, for run tasks, the script path and commit.

**What agents see.** An agent reads a released profile or result only through an open task of its own
(status *Defined* to *Changes requested*, as assignee or reviewer, and only if it may work in the task's zone) that
references it; another task in the same project is not enough, and runners (`model_location = none`) read no
released content. `tbr prepare` (also `tbr run` and MCP `start_task`) writes the section **Approved data artifacts**
into TASK.md from `GET /api/v1/tasks/<nr>/artifacts`: the released schema and synthetic sample, or the released
aggregate, inline – so the agent needs no further access. Unreleased artifacts are named with their status only;
datasets are named only (raw data never leaves zone R). Inlined reads are audited (`profile_read`, `result_read`).

## Retention and deletion

Each dataset has a retention date (default from `data_retention_days`). The scheduler job `data_retention` (hourly in
the app; `tbr-admin data retention` runs the same idempotent function, for example from a systemd timer) reminds the
project owner by Telegram seven days before the date – key, event and link only – and deletes the dataset once the
date has passed. Deleting (also by hand on the dataset page, confirmed by typing the key) drops the wrapped DEK
(crypto-shredding: every copy, also in backups, becomes unreadable), overwrites the file once and removes it. The
row stays with status *Deleted*; its audit entries are never removed. The job takes the database lock
`tbr_data_retention`, so two triggers never run at once.

## Data audit

`data_audit` is append-only and hash-chained: each entry stores the hash of the previous one and
`hash = SHA-256(prev_hash || entry)`. The end of the chain is kept in `data_audit_head`, so removing entries at the
end is detected too. Entries never contain data values or dataset names. Current actions: `dataset_uploaded`,
`dataset_archived`, `dataset_deleted`, `dataset_retention_warning`, `dataset_downloaded`, `dataset_read` (a
local-model agent read a `local_only` dataset), `data_access_changed`, `data_class_lowered` (a task looser than its
project), and for runner results `run_started`, `run_finished`, `run_failed`, `gate_checked` (check numbers only),
`result_viewed`, `result_released`, `result_rejected`, `result_kept_sealed` (with the decider's note),
`result_read` and `four_eyes_changed`.

```sh
tbr-admin data audit verify    # exit code 1 if the chain is broken
```

Migration `0024` adds two triggers that refuse `UPDATE` and `DELETE` on `data_audit`. With binary logging on and
without `log_bin_trust_function_creators`, MariaDB may refuse to create them; the migration then logs a warning and
`verify` reports the missing triggers. Create them later as a database admin:

```sql
CREATE TRIGGER data_audit_no_update BEFORE UPDATE ON data_audit FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'data_audit is append-only';
CREATE TRIGGER data_audit_no_delete BEFORE DELETE ON data_audit FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'data_audit is append-only';
```

The hash chain detects changes even without the triggers; it cannot stop someone with full database rights from
rewriting the whole chain, so note the last hash printed by `verify` in a separate place from time to time.
