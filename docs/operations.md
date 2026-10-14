<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# Operations

Running a TaskBrigade server after the [quickstart](quickstart.md): network access, Tailscale, updates, backups
and working with several people. The security model behind the access rules is in [SECURITY.md](../SECURITY.md).

## Installation, short form

The full walkthrough is the [quickstart](quickstart.md). The install script is idempotent and configured only
through environment variables – you never edit it:

```bash
sudo TBR_DB_HOST=127.0.0.1 TBR_DB_PORT=3306 GITEA_URL=http://127.0.0.1:3001 deploy/install.sh
```

It checks Python 3.12, uv, a MariaDB client ≥ 10.5, Gitea and (only with `TAILSCALE_SERVE=1`) Tailscale.
`LAN_UI_NETWORK=192.168.1.0/24` additionally opens the UI over HTTP to that network; leave it out for localhost or
Tailscale only. Every setting the service reads is documented in [`.env.example`](../.env.example).

On first start the app runs in **setup mode**: under *Admin → Database* enter the password, then
“Test connection”, “Save”, “Upgrade schema” – no restart
needed. For a MariaDB on another host, prepare it with [`deploy/mariadb-setup.sql`](../deploy/mariadb-setup.sql).

## Gitea service token

The service token (of the Gitea service user, e.g. `taskbrigade`, entered under *Admin → Settings*;
an installation from before 0.8 keeps its user `taskmeister` – it is not renamed) needs the scopes
`write:repository`, `write:issue` (pull-request comments) and `write:user` (create repositories in its own
namespace; for repositories in an organisation use `write:organization` instead). Gitea admin rights are not
needed.

## Access rules

The app decides by the **direct connection peer**; uvicorn runs with `--no-proxy-headers`:

| Peer | Access |
|---|---|
| localhost, or `tailscale serve` (arrives on localhost) | UI and API |
| `TRUSTED_PEER_NETWORKS` (empty; Docker: the bridge gateway) | like localhost |
| `API_NETWORKS` (empty) | UI and API over HTTP – bearer tokens in cleartext, opt-in only; no Tailscale header, no git credentials |
| `LAN_UI_NETWORKS` | UI only, over HTTP with password login; the API answers 403 (no tokens in cleartext over the LAN) |
| anything else | 403 |

LAN address of the UI: `http://<server-ip>:8090`. Do not expose port 8090 to the internet.

## Tailscale

Example: the app server has the tailnet name `taskbrigade` → `https://taskbrigade.example.ts.net` (adjust the
names).

1. Once, in the Tailscale admin console under *DNS*, enable "HTTPS Certificates".
2. `sudo tailscale serve --bg --https=443 http://127.0.0.1:8090` (persists across reboots), or install with
   `TAILSCALE_SERVE=1`.
3. In `/etc/taskbrigade/taskbrigade.env`: `UI_ALLOWED_LOGINS=admin@example.org`. In the UI, enter the Tailscale login
   `admin@example.org` for the actor `admin`, and under *Settings* set the TaskBrigade URL to
   `https://taskbrigade.example.ts.net` (used for links in pull requests and notifications).
4. Agents on other tailnet machines use `url = "https://taskbrigade.example.ts.net"` in `.taskbrigade.toml`
   ([CLI agents on a second machine](cli-agents.md)); agents on the app server itself may use
   `http://127.0.0.1:8090`.

The app trusts the Tailscale identity header only on requests from localhost (which is how `tailscale serve`
arrives). Local processes on the app server could therefore forge it – acceptable on a single-user machine, see
[SECURITY.md](../SECURITY.md).

## Updates, logs, backups

- Update: `sudo deploy/update.sh` (backup, schema upgrade, systemd units, restart, fresh `tbr` wheel for the agents).
  An installation from before 0.8 is migrated to the new names first – see
  [below](#migrating-an-installation-from-before-taskbrigade-08).
- **Raise the version before rolling out a change** (`version` in `pyproject.toml`, then `uv lock`, and a CHANGELOG
  section with the date). The agents' `tbr self-update` and the version warning also compare the build identifier
  of the delivered wheel (`tbr version` and `/healthz` show `0.6.0+<build>`), so a forgotten bump no longer leaves the
  agents on the old code – but only the version number tells humans and the changelog what they are running.
  After the update, check `curl -s <url>/healthz` for the new version and build and run `tbr self-update` on one agent
  machine.
- Logs: `journalctl -u taskbrigade -f`
- Backups: daily at 02:30 to `/var/backups/taskbrigade/`, kept 14 days (`TBR_BACKUP_KEEP_DAYS`).
- **Back up `/var/lib/taskbrigade/master.key` separately** – without it the stored database password, Gitea tokens
  and settings cannot be decrypted.
- Configuration without editing files: `sudo /opt/taskbrigade/app/.venv/bin/tbr-admin config set KEY VALUE` writes
  the env file the service reads (`/etc/taskbrigade/taskbrigade.env`, found via the systemd unit), refuses unknown
  keys and invalid values and offers a restart; `config get KEY`, `config list` (secrets masked) and
  `config unset KEY` (back to the default). Omit `VALUE` for a secret to type it hidden instead of leaving it in the
  shell history. Docker: edit `.env` next to `compose.yaml` and run `docker compose up -d`.
- Emergency commands (as user `taskbrigade`): `tbr-admin set-db-password`, `tbr-admin db-status`, `tbr-admin upgrade`,
  `tbr-admin reset-admin-password`.
- Docker Compose installations use `deploy/docker/backup.sh`, `restore.sh` and `update.sh` instead
  ([docker.md](docker.md#backup-and-restore)).

## Migrating an installation from before TaskBrigade 0.8

Before 0.8 the server ran under the technical name `taskmeister`. `deploy/update.sh` moves such an installation to
the new names once, on its first run – the data, the database and its name stay as they are.

| Before 0.8 | Since 0.8 |
|---|---|
| `/opt/taskmeister` (install prefix; a custom `TM_BASE` stays where it is) | `/opt/taskbrigade` (`TBR_BASE`) |
| `/etc/taskmeister/taskmeister.env` | `/etc/taskbrigade/taskbrigade.env` |
| `/var/lib/taskmeister`, `/var/log/taskmeister`, `/var/backups/taskmeister` | `/var/lib/taskbrigade`, `/var/log/taskbrigade`, `/var/backups/taskbrigade` |
| `taskmeister.service`, `taskmeister-backup.service`/`.timer` | `taskbrigade.service`, `taskbrigade-backup.service`/`.timer` |
| system user and group `taskmeister` | `taskbrigade` (renamed, same UID/GID) |
| `TM_*` keys (`TM_BIND_HOST`, `TM_DB_HOST`, `TM_BACKUP_DIR` …) | `TBR_*` |
| default database name `taskmeister` | `taskbrigade` – only for new installations: `db.json` in the state directory names the database of an existing one (`TBR_DB_NAME` only sets the default) |
| backup files `taskmeister-<time>.sql.gz` | `taskbrigade-<time>.sql.gz` (old files rotate out as before) |

Unchanged: the port, Tailscale (`tailscale serve` keeps forwarding to `127.0.0.1:8090`), `master.key`, the Gitea
service user and its token, all agent tokens. During the transition the server also reads the old names – `TM_*`
in the environment and in `.env`, `/var/lib/taskmeister` while `/var/lib/taskbrigade` does not exist, the old env
file and unit for `tbr-admin config` – and logs `legacy_name` once per old name.

**Step by step** (as root on the server):

1. Pull the new version into the old checkout, so the new `update.sh` runs (the old one does not know the
   migration):

   ```bash
   sudo git -C /opt/taskmeister/app -c safe.directory='*' pull --ff-only origin main
   sudo /opt/taskmeister/app/deploy/update.sh
   ```

2. `update.sh` finds `taskmeister.service` or `/etc/taskmeister` and runs
   [`deploy/migrate-names.sh`](../deploy/migrate-names.sh):
   1. **Backup:** a database dump (the installation's own `deploy/backup.sh`) and a tarball
      `taskmeister-migration-<time>.tar.gz` of `/etc/taskmeister`, `/var/lib/taskmeister` (`master.key`, `db.json`)
      and the old units – both in the backup directory. If the dump fails, nothing is changed. An installation still
      in setup mode (no database password yet) has nothing to dump: `TBR_MIGRATE_SKIP_DB_BACKUP=1`.
   2. Stop and disable `taskmeister.service` and `taskmeister-backup.timer`.
   3. Rename the system user and group `taskmeister` → `taskbrigade` (`usermod -l`, `groupmod -n`).
   4. Move the directories (table above); the virtual environment is rebuilt when the prefix moved.
   5. Rename `TM_<X>=` → `TBR_<X>=` in the env file (backup `taskbrigade.env.bak-<time>`; an existing `TBR_<X>` wins,
      the old line is commented out) and old default paths in values.
   6. Remove the old units.

   Then it continues from the moved checkout as a normal update: pull, `uv sync`, `tbr` wheel, backup, schema
   upgrade, install and enable `taskbrigade.service` and `taskbrigade-backup.timer`, restart.
3. Check: `systemctl status taskbrigade`, `journalctl -u taskbrigade -n 50`, `curl -s http://127.0.0.1:8090/healthz`
   (`"db":"ok"`), sign in to the UI.
4. Switch the agent machines (`tbr self-update`, then `tbr setup <kind>` or `tbr doctor --fix`):
   [cli-agents.md](cli-agents.md#from-the-names-taskmeister-to-taskbrigade-taskbrigade-08). They keep working
   before that, with the old names.

`migrate-names.sh` can also run on its own (`sudo deploy/migrate-names.sh`); on a migrated installation it does
nothing. It stops with a message instead of overwriting when a target already exists (e.g. `/etc/taskbrigade`).

**Way back** (to the version before 0.8; the database schema of the new version may not fit the old code, so the
database comes back from the dump taken in step 2.1):

```bash
sudo systemctl disable --now taskbrigade.service taskbrigade-backup.timer
sudo rm /etc/systemd/system/taskbrigade.service /etc/systemd/system/taskbrigade-backup.{service,timer}
sudo usermod -l taskmeister taskbrigade && sudo groupmod -n taskmeister taskbrigade
sudo mv /opt/taskbrigade /opt/taskmeister && sudo usermod -d /opt/taskmeister taskmeister
sudo mv /var/lib/taskbrigade /var/lib/taskmeister
sudo mv /var/log/taskbrigade /var/log/taskmeister
sudo mv /var/backups/taskbrigade /var/backups/taskmeister
sudo rm -rf /etc/taskbrigade
sudo tar -xzf /var/backups/taskmeister/taskmeister-migration-<time>.tar.gz -C /   # old env file and units
sudo git -C /opt/taskmeister/app -c safe.directory='*' checkout <previous version>
sudo rm -rf /opt/taskmeister/app/.venv && sudo chown -R taskmeister:taskmeister /opt/taskmeister
sudo -u taskmeister -H bash -c 'cd /opt/taskmeister/app && uv sync --frozen --no-dev'
# restore the database from /var/backups/taskmeister/taskmeister-<time>.sql.gz (mariadb … < dump), then:
sudo systemctl daemon-reload && sudo systemctl enable --now taskmeister.service taskmeister-backup.timer
```

## Several people: owners and roles

- Every project has an **owner** (a human). The owner reviews in that project, answers questions, edits and
  distributes tasks and maintains the project settings – like an admin, but only there.
- Roles for humans: `admin` (everything) and `member` (create and manage their own projects, read and comment
  elsewhere). Roles for agents: `worker`, `orchestrator` (may also create and assign tasks) and `reviewer` (reviews
  the tasks it is entered for as reviewer and works on tasks like a worker – see "Reviewers" below).
- Every human – admins included – sees only the projects they own: project list, inbox, backlog, kanban, sprint
  filters, search and the project choices (new task, quick capture "+", sprint editor). The profile switch “Show all
  projects” (*Profile* → *Display*) is stored per human and off by default; switched on,
  all projects appear with the "Owner" filter (default "all owners", the last choice is remembered in the browser).
  API, CLI, MCP, agents and notifications are not affected.
- A new person: create them under *Admin → Users & agents* as a human with role `member` (password or Tailscale
  login; add Tailscale logins to `UI_ALLOWED_LOGINS` in the server configuration as well). Optionally link a Gitea
  user so they can see the pull requests in the repository.
- The profile switch and the owner filter are a convenience, not a confidentiality boundary: signed-in humans can
  read and comment beyond their own projects through direct links (see [SECURITY.md](../SECURITY.md)).

## Several people: agent owners and public agents

- Every agent has an **owner** (a human) and/or is **public** (*Admin → Users & agents*, columns and form fields
  “Owner” and “Public”; admins only). New agents belong to the human who creates them (changeable in the
  agent wizard *Admin → New agent* and the agent form) and are **not** public. The update (migration 0018) gives
  existing agents their creator as owner (the human who created their first join code), otherwise
  `BOOTSTRAP_ADMIN_ACTOR` or the first human admin, and makes them all public – nothing changes until an admin
  switches agents to private.
- Choices for assignee and reviewer – task form, task detail, duplicate and follow-up, sprint editor and batch
  capture, agent filters in backlog and kanban, project default reviewer – show a human only their **own and public**
  agents (plus humans). An agent already assigned to a task stays visible there and is never cleared silently.
- Admins: profile switch “Show all agents” (*Profile* → *Display*, off by default) shows all
  agents in those choices. *Admin → Users & agents* always lists all actors, with combinable filters Owner, Role
  and Kind (`?owner=<name>|none&role=<role>&kind=<agent_kind>`, shareable as a link) and the number of matches.
- **Enforced on the server** for web UI, API, CLI and MCP: a non-admin cannot assign another human's private agent
  as assignee or reviewer (`403 agent_not_visible`). Admins may assign any agent (the switch is only a display
  preference). An agent – e.g. an orchestrator in Telegram – may assign itself, public agents and the agents of its
  own owner. Tasks keep an assignee their editor could not choose; only changing it is checked. A project's default
  reviewer is checked when it is set, not when a new task takes it.
- `tbr agents` / MCP `list_agents` (`GET /actors`) list only the agents the caller may assign, with `public` or the
  owner; admins get all with `tbr agents --all` (`GET /actors?all=true`) or with the profile switch on.

## Reviewers: who approves a task

By default the project owner and the admins review. A task can name a **reviewer** instead – a human or an agent with
the role `reviewer` (*Admin → Users & agents*, role `reviewer`; humans cannot have it):

- Field *Reviewer* in the task form, the task detail view (changeable until the task is approved), the sprint editor
  and the batch capture (4th field: `Title | Agent | Prio | Reviewer`); API `reviewer` in `POST`/`PATCH /tasks`,
  `tbr create/update --reviewer <name>|none`, MCP `create_task`/`update_task` (`reviewer`).
- Project setting *Standard-Reviewer* (default reviewer, project page; API `PATCH /projects/{key}`
  `{"default_reviewer": …}`): new tasks take it, the task field stays changeable. Empty = owner and admins as before.
  `reviewer: "none"` on creation leaves a task without one.
- **No self-review:** the reviewer can never be the assignee – setting either is refused (`422 self_review`). At
  submission a reviewer that cannot review the task (same as the assignee, role lost, inactive, cloud agent in a
  `local_only` project) is removed with a note in the thread, and the review falls back to the owner and admins.
- **Humans keep the right:** the owner and admins can always approve, reject or return a conflict – also while a
  reviewer agent works on the review, e.g. to overrule it.
- Only the entered reviewer may decide (besides owner and admins); a reviewer agent has no other admin rights.
  `local_only` projects accept only reviewer agents with runtime `local` (same rule as assignments).
- A **reviewer agent** gets a task in `for_review` as a *review job*: `tbr list` and MCP `list_tasks` mark it,
  `tbr run`/`tbr prepare`/MCP `start_task` claim it with its own lease (the agent's lease minutes; `tbr heartbeat`
  extends it) and prepare a read-only checkout of the task branch with `REVIEW.md`
  (`<KEY>/.worktrees/<NR>-review`, tmux window `<NR>-review`). The agent decides with `tbr review <NR> approve|reject|
  conflict` (MCP `review_task`) – the same path as a human approval: merge, brain-changes, changelog.d,
  notifications. `tbr review <NR> release --reason …` gives the job back undecided; an expired review lease returns it
  to the reviewer's queue. Review jobs count towards *Max. parallel tasks*; with "one review at a time per agent" a
  reviewer holds at most one review job per project.
- Model of a review session: the task's tier (or concrete model, if the reviewer's allow list contains it) resolved
  with the reviewer's own model settings – there is no separate review model field.
- Display: the thread notes “Approved by <agent> (reviewer agent)”, likewise for
  rejections and returned conflicts; the history shows `review_claimed`/`review_released`/`review_lease_expired`, the
  server log audits `review_*` with `reviewer_agent=true`. The inbox lists tasks a reviewer agent reviews under
  “Review by agents” instead of "Needs your action"; Telegram sends no review request for them
  but the event “Reviewed by a reviewer agent” after the agent's decision.
- After updating the server, update reviewer agent machines too (`tbr self-update`); Hermes/OpenClaw reviewers need
  `tbr setup hermes|openclaw` once so the trigger recognises review jobs.

## Telegram notifications

1. Create a bot with @BotFather (its own bot – a bot token can only be polled by one program).
2. *Admin → Settings*: enter the token under "Telegram bot token (from @BotFather)", tick "Telegram bot enabled"
   (notifications, linking in the profile), save and check with “Test Telegram connection”. Set the TaskBrigade URL there
   too, so messages carry links.
3. Every human opens *Profile*, creates a link code and sends `/start <code>` to the bot (the code is valid
   for 15 minutes and works once). The profile also selects events, quiet hours and mutes; admins can follow all
   projects.
4. Bot commands (German name / English alias): `/inbox` (questions, reviews and aborted tasks of your projects,
   with links), `/stumm` / `/pause [2h | bis 18:00]` (mute all messages; without a duration until `/laut`),
   `/laut` / `/resume`, `/reviews aus|an [2h]` (review requests only; also `off|on`), `/status` (link, mutes,
   server), `/hilfe` / `/help`, `/stop` (unlink). `/mute`/`/unmute` still work. Messages and buttons from chats that
   are not linked are ignored.
5. Acting from Telegram, always as the linked human with that person's rights (admin or project owner):
   - Review messages carry the buttons “Approve” (asks for confirmation, then merges) and
     “Reject” (the next message becomes the review comment; `/abbrechen` stops). A merge, brain or
     changelog conflict is explained in the chat and offers “Return conflict”; conflict
     messages carry that button too.
   - Replying to a question message answers the question in the task thread, as in the inbox. Questions of
     `local_only` projects are never shown in Telegram, so a reply works only after you opened the question in the
     browser (task page or inbox); otherwise the bot sends the link.
   - Buttons expire after 7 days (confirmations after 30 minutes), work once per message and only in the chat they
     were sent to. Every action is written to the audit log with `via: telegram`. For `local_only` projects the
     bot's replies contain task number, outcome and link only.

6. From the CLI or MCP (`tbr notify`, MCP tool `notify`): `tbr notify status`, `tbr notify pause [2h | until 18:00]`,
   `tbr notify resume`, `tbr notify reviews off [1d]` / `on`. A human with an API token changes their own settings;
   orchestrators and admins may also pass `--user <name>` for a human linked with Telegram – that person gets a short
   message from the bot (even while muted), and the change is audited (`notifications_muted`/`_unmuted`,
   `target`, `via: api`). Agents have no notifications of their own. This is how an orchestrator agent such as
   `openclaw-agent` mutes notifications on a request in chat – see
   [agent-frameworks.md](agent-frameworks.md#openclaw-as-orchestrator-in-telegram).

The delivery log (without contents) is at the bottom of *Admin → Settings* and in each profile.

## SFTP storage for agent results

Agents can put results on a local SFTP server (e.g. a NAS in the LAN) into one central folder, each with its own
SFTP user. TaskBrigade is the central source of the logins; the transfer runs on the agent machine.

1. *Admin → Settings*: tick “SFTP storage enabled (agents upload results with tbr sftp)”, enter host and port (default 22), the base
   folder (default `/agent exchange`) and the storage pattern (default `{base}/{project}/{task_nr}`; placeholders
   `{base}`, `{project}`, `{task_nr}`; it must start with `{base}`). Save, then “Test SFTP connection”:
   the first time it shows the server's host key fingerprint – compare it with the server
   (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) and click “Accept fingerprint”. Agents refuse
   every server whose key does not match it; a changed server key is entered by hand in the setting.
2. *Admin → Users & agents → <agent> → SFTP storage*: SFTP user and password (stored encrypted
   like Gitea tokens, only the last 4 characters are shown), “Test access” logs in, writes a test
   file into the base folder and deletes it. Same via API: `PUT /api/v1/admin/actors/<agent>/sftp`
   `{"sftp_username","sftp_password"}`, `DELETE …/sftp`, `POST …/sftp/check`.
3. Agents fetch their own login with `GET /api/v1/agents/me/sftp-credentials` – only over HTTPS (tailscale serve)
   or from `TRUSTED_PEER_NETWORKS`, exactly like `/agents/me/git-credentials`; every fetch is audited
   (`sftp_credentials_fetched`), the password never appears in logs. `GET /api/v1/agents/me/sftp[?task_nr=…]` returns
   the same without the password (and the task folder).
4. `tbr sftp info|put|ls|get` and the MCP tools `sftp_put`/`sftp_ls` use only the configured server, refuse paths
   outside the base folder (`..`, other absolute paths) and check the host key before the password is sent. Every
   upload is noted in the task as a system comment with path, size and SHA-256. `tbr doctor` checks the agent's
   login when SFTP is enabled. TASK.md mentions the storage when it is enabled for the agent – agents use it only
   when the task asks for it.

The passwords are on the agent machines while `tbr sftp` runs (in memory only, never written to files) – keep the
SFTP server reachable only in the LAN and give each SFTP user write access to the base folder only.

## Orchestrating from the CLI

Orchestrators (role `orchestrator`/`admin`) can maintain tasks entirely from the command line:

```bash
tbr create --project DEMO --title "…" --assign codex --due 2026-10-31 --description "…" --acceptance "…"
tbr update DEMO-0042 --assign opencode --due 2026-11-01 --prio 2   # PATCH with If-Match (version check)
tbr cancel DEMO-0042 --reason "duplicate"                          # → cancelled
tbr reset DEMO-0042                                                # → defined (same permissions as the web UI)
tbr duplicate DEMO-0042 --assign codex                             # new draft "(duplicate) …", prints its number
tbr update DEMO-0042 --reviewer review-agent                       # reviewer (human or reviewer agent); none removes it
tbr review DEMO-0042 approve --comment "…"                         # or reject --comment …, conflict (owner/admin/reviewer)
tbr show DEMO-0042                                                 # incl. due date and last status change
tbr list --due                                                     # with a due-date column
```

`--due` is optional and informational – claiming does not depend on it. `tbr create` warns about drafts and
unassigned tasks, because those are never claimed. The MCP tools (`tbr mcp`) are `create_task` (with `due_date`),
`update_task`, `cancel_task`, `reset_task` and `duplicate_task`. `tbr run` writes skipped assigned tasks with the reason to
`logs/runner.log`.

### Sprints

Sprints are an optional level between project and task (rules in [architecture.md](architecture.md#sprints)).
Sprints are addressed by id or by name; a name needs the project (`-p`/`--project`).

```bash
tbr sprint list -p DEMO [--status planned]                        # id, name, status, period, progress
tbr sprint show "Sprint 1" -p DEMO                                # goal and tasks
tbr sprint create -p DEMO --name "Sprint 1" --goal "…" --start 2026-10-12 --end 2026-10-23
tbr create --project DEMO --title "…" --sprint "Sprint 1"         # in a planned sprint always a draft
tbr sprint add-tasks "Sprint 1" -p DEMO --file tasks.txt          # batch; --dry-run only checks, '-' reads stdin
tbr update DEMO-0042 --sprint "Sprint 2"                          # move; --sprint none detaches
tbr list --assigned-to all --project DEMO --sprint "Sprint 1"     # --sprint none: tasks without a sprint
tbr sprint release "Sprint 1" -p DEMO                             # admins and the project owner
tbr sprint close "Sprint 1" -p DEMO --move-to "Sprint 2"          # or --detach; admins and the project owner
```

The batch file has the same line format as *Tasks gesammelt erfassen* (batch capture) in the web UI: one task per
line, `Title` or `Title | Agent | Prio | Reviewer` (an empty reviewer means the project's default reviewer, `none`
nobody); empty lines and lines starting with `#` are skipped. All lines are checked
first, and the server creates either all of them or none. Planning (create, assign, batch) is open to admins,
orchestrators and the project owner; release and close only to admins and the project owner – others get `403`.
The release prints the tasks set to "defined" and the drafts that stay drafts with their missing fields; run it
again after completing them. `tbr show` and the agent's `TASK.md` show the sprint with its status and goal. MCP
offers the same: `list_sprints`, `show_sprint`, `create_sprint`, `add_sprint_tasks`, `release_sprint`,
`close_sprint` and the `sprint` parameter of `create_task`/`update_task`.

In the web UI, *Backlog → New sprint* (shown to admins, orchestrators and project owners) opens the
sprint editor: header data on top, task headings on the left (Enter adds one, pasting several lines adds one per
line, drag to reorder, × removes), details of the selected heading on the right, with a ready/missing hint per
heading. *Save* creates or updates the planned sprint and all its drafts in one transaction; *Save and release
sprint* then runs the normal release. A planned sprint opens in the same editor via
*Edit in the sprint editor* on its sprint page; tasks past draft are read-only there, and removing a
saved draft cancels it.
