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
- Backups: daily at 02:30 to `/var/backups/taskbrigade/`, kept 14 days (`TBR_BACKUP_KEEP_DAYS`). With the data
  zones on, set `TBR_BACKUP_DATA=1` to include the encrypted store (`TBR_DATA_DIR/store`) as ciphertext; the KEK is
  never part of that archive.
- **Back up `/var/lib/taskbrigade/master.key` separately** – without it the stored database password, Gitea tokens
  and settings cannot be decrypted.
- Configuration without editing files: `sudo /opt/taskbrigade/app/.venv/bin/tbr-admin config set KEY VALUE` writes
  the env file the service reads (`/etc/taskbrigade/taskbrigade.env`, found via the systemd unit), refuses unknown
  keys and invalid values and offers a restart; `config get KEY`, `config list` (secrets masked) and
  `config unset KEY` (back to the default). Omit `VALUE` for a secret to type it hidden instead of leaving it in the
  shell history. Docker: edit `.env` next to `compose.yaml` and run `docker compose up -d`.
- Emergency commands (as user `taskbrigade`): `tbr-admin set-db-password`, `tbr-admin db-status`, `tbr-admin upgrade`,
  `tbr-admin reset-admin-password`.
- `tbr-admin data scan-attachments` lists spreadsheet and archive task attachments (`.xlsx`, `.xls`, `.xlsm`, `.csv`,
  `.tsv`, `.ods`, `.zip`) already stored in `local_only` projects. Report only: it does not delete files or rewrite
  Git history. New uploads of those types are refused (`422 use_dataset_upload`) until the data store exists.
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
- A new person: use *Admin → Users & agents → + Add human* to create a human with role `member` (password or Tailscale
  login; add Tailscale logins to `UI_ALLOWED_LOGINS` in the server configuration as well). Optionally link a Gitea
  user so they can see the pull requests in the repository.
- The profile switch and the owner filter are a convenience, not a confidentiality boundary: signed-in humans can
  read and comment beyond their own projects through direct links (see [SECURITY.md](../SECURITY.md)).

## Several people: agent owners and public agents

- Every agent has an **owner** (a human) and/or is **public** (*Admin → Users & agents*, owner groups and the
  detail panel; “Public” is in “Advanced settings”; admins only). New agents belong to the human who creates them
  (changeable in the
  agent wizard *Admin → + New agent* and the detail panel) and are **not** public. The update (migration 0018) gives
  existing agents their creator as owner (the human who created their first join code), otherwise
  `BOOTSTRAP_ADMIN_ACTOR` or the first human admin, and makes them all public – nothing changes until an admin
  switches agents to private.
- Choices for assignee and reviewer – task form, task detail, duplicate and follow-up, sprint editor and batch
  capture, agent filters in backlog and kanban, project default reviewer – show a human only their **own and public**
  agents (plus humans). An agent already assigned to a task stays visible there and is never cleared silently.
- Admins: profile switch “Show all agents” (*Profile* → *Display*, off by default, saved as soon as it is
  toggled). Off: those choices, the agent filters and *Admin → Users & agents* show only the admin's **own**
  agents – also when other owners' agents are public; humans are always listed, and the users page names the
  hidden agents with a “Show all agents” link (`?all_agents=1`, this view only). On: all agents. *Admin → Users &
  agents* lists actors grouped by owner, with search
  (name, kind, Gitea user), All/Agents/Humans, Role and Kind filters and the number
  of matches. Owner filtering remains available in shareable URLs
  (`?owner=<name>|none&role=<role>&kind=<agent_kind>&q=<search>&type=agent|human`). Open a name or › to edit
  role, owner, runtime and parallel task limits in the detail panel (a full-screen sheet on phones).
  The panel also tests/edits Gitea credentials and rotates or revokes API tokens with confirmation.
  Stored tokens cannot be copied because only their hash is retained; after rotation, copy the new masked token
  immediately. “Advanced settings” retains model, public visibility, password, SFTP and project-access options.
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
  `local_only` projects accept only reviewer agents allowed to work there – runtime `local`, and with
  “local_only only for agents with a local model” on also model location `local` (same rule as assignments,
  [architecture.md](architecture.md#privacy)).
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
  server log audits `review_*` with `reviewer_agent=true`. The inbox lists tasks a reviewer agent reviews (or sent
  back) in its own section “Reviews by agents” below "Needs your action", not counted as items that need you; the
  project owner and admins can still overrule a running review there. The notices panel only links to that
  section. Telegram sends no review request for them
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

The delivery log (without contents) is at the bottom of *Admin → Settings* and in each profile. Email is a second
channel with its own settings, see [Email notifications](#email-notifications).

## Email notifications

Email is a second channel next to Telegram. TaskBrigade only **sends** mail over SMTP submission; it never reads the
mailbox (no IMAP, no replies, no commands by mail). Use a mailbox of its own at a mail provider (DKIM/SPF/DMARC for
your domain) and forward that mailbox to an admin, so bounces and accidental replies are not lost – TaskBrigade
only sees errors the SMTP server reports at once.

Choosing a provider: use an EU provider on **your own domain** — the sender address comes from a domain you control.
Make sure **DKIM, SPF and DMARC** can be (and are) set for that domain, or your notification mails land in the
recipients' spam folders. Pick a provider that offers a **data processing agreement** (GDPR Art. 28) for the mailbox
— the mails carry task titles and links. Authenticate with an **app password**, never the main account password
(many providers only offer app passwords with two-factor authentication on). Find the provider's documented
**limit of mails per hour** for SMTP submission and enter a clearly lower value as the TaskBrigade rate limit.

1. *Admin → Settings → Email*: “SMTP host (submission server of the mail provider)”, “SMTP port (587 STARTTLS, 465
   TLS)”, “SMTP encryption” (`starttls`/`tls`; the certificate is always checked), “SMTP user”, “SMTP password (app
   password)” (stored encrypted, never shown again), “Sender address”, “Sender name” and “Maximum mails per hour (stay
   below the provider's limit)” (default 60). These live only in the database – there are no `.env` keys. “Send test
   mail” sends a mail with the stored values and shows the SMTP error as is (it also works while the channel is
   off). Then tick “Email notifications enabled (outbound only, replies are not read)” and save. Set the TaskBrigade
   URL too: links, `List-Unsubscribe` and the Message-ID host name come from it.
2. Every human enters an address under *Profile → Email* and opens the confirmation link from the mail (valid 24 hours,
   works once). No notification goes to an unconfirmed address; changing the address (also by an admin) needs a new
   confirmation.
3. *Profile → Notifications* chooses the channel per event (table event × channel). Default: email only for “Task
   ready for review” and “Sprint completed”, everything on Telegram. “Email bundling”: immediately, or one mail every
   15/60 minutes. Mutes and quiet hours apply to both channels; mails held during quiet hours go out as one collected
   mail afterwards.

Mails have the subject `[TaskBrigade] <TASK-NR> · <event>`, the same text as the Telegram message (for `local_only`
only task number, event and link), plain text plus simple HTML without tracking, the footer “Replies to this mail are
not processed – please answer via the link in TaskBrigade.” and the headers `Auto-Submitted: auto-generated` and
`List-Unsubscribe` (link to the profile settings). The scheduler job `email_deliver` (every 30 s) sends them – only in
the process holding `<state_dir>/email-sender.lock` – with up to 8 attempts and back-off; a 5xx answer for the
recipient or the message gives up at once. The delivery log (column “Channel”, no contents, 30 days) is at the bottom
of *Admin → Settings* and in each profile.

### Sending work results by email

Agents can have a result mailed to a customer or colleague, but they never get SMTP credentials and never send
themselves: they file a **send request**, the server checks it, a human approves it, and the same `email_deliver` job
sends it (same SMTP account, same hourly limit). The email channel must be enabled (step 1 above).

1. *Projects → <key> → Email recipients* (admins and the project owner): add every external address that may receive
   results of the project, with a label and the mode “with approval” (default) or “without approval”. Addresses that
   are not on the list are refused. Humans with a TaskBrigade account and a **confirmed** address may be sent to
   without approval unless the list has an entry for their address (the entry's mode wins).
2. *Admin → Settings → Email*: “Result mails: maximum size per mail incl. attachments (MB)” (default 10) and “Result
   mails: maximum send requests per project and day” (default 20, counted over the last 24 hours).
3. The agent calls `tbr mail send <TASK> --to <address> --subject "…" --body-file result.md --attach <ref>` or the MCP
   tool `send_email`; `tbr mail list <TASK>` / `list_emails` show the status, `tbr mail recipients <KEY>` /
   `list_mail_recipients` the list. When the project has recipients, TASK.md contains the section “Sending results by
   email”. Attachments are references the server reads itself from Gitea: `attachment:<id>` (task attachment),
   `branch:<path>` (file on the task branch), `merge:<path>` (file in the merge commit), or `result:<id>` (a released
   output-gate aggregate from this project, exported as a Markdown attachment containing the released JSON).
   Quarantine files and logs are never attached. Allowed types: pdf, docx, xlsx,
   pptx, csv, md, png, jpg; zip always needs approval. The SHA-256 of each file is recorded with the request; when
   sending, the server fetches the file again and fails the mail if it changed.
4. Requests waiting for a human appear in the task's **Emails** section (recipients, subject, text, attachments with
   size and hash; “Approve and send”, “Edit”, “Reject”) and in the inbox group **Email approvals**. Admins, the project
   owner and the task's human reviewer decide; agents (also reviewer agents) cannot approve. With `--on-approve`
   (`on_approve=true`) the request goes out when a human approves the task – the review view shows it, and “Approve &
   merge” queues it. Cancelling a task rejects its open requests.

`Reply-To` is the approving human (their profile address), otherwise the project owner; the footer says that
TaskBrigade does not read replies. For `local_only` projects/tasks, every attachment must be a released `result:<id>`;
at least one is required. These mails always need a separate human approval, even for internal recipients or entries
marked “without approval”; `on_approve` is disabled. `sealed` mail is always refused. The request's data class is
preserved and the current class, result release status and attachment hashes are checked again before sending.
The result (sent with Message-ID, or the SMTP error) is written as a system
comment on the task, and the audit log gets `mail_requested`, `mail_approved`, `mail_rejected`, `mail_sent` and
`mail_failed` with task, recipients and attachment hashes – never the text. After 30 days the text and the attachment
references are cleared (recipients, subject, hashes and status stay); requests still waiting by then expire.

Troubleshooting common SMTP errors:

- **“535 … Authentication credentials invalid” / login refused**: wrong user, or a normal account password
  instead of an app password. Create an app password and enter it again.
- **“5xx … Recipient address rejected” / recipient refused**: the address does not exist at the provider, or
  the recipient has blocked you. Check the address in the profile; a new confirmation mail helps after changes.
- **“554 … relay access denied” / Relay denied**: the provider only accepts submission with login — check
  “SMTP user”/password and that you use the submission server (ports 587/465), not a plain internal relay.
- **TLS or certificate errors**: the certificate is always checked; a wrong port/encryption combination
  (STARTTLS on the implicit-TLS port or vice versa) usually causes it. Match port and encryption setting.
- **Throttling / rate limit errors (4xx)**: lower “Maximum mails per hour”; the scheduler retries with back-off.
- **Mails land in spam**: DKIM/SPF/DMARC for the sender domain are missing or wrong — set them at the provider
  and in your DNS.

If deliveries keep failing although TaskBrigade itself is healthy, check the email channel with `tbr doctor` on an
agent machine: it shows the SMTP probe (reachability and login, without credentials) and the time of the last
successful delivery, and it warns when deliveries keep failing. When the probe itself fails (SMTP server
unreachable, TLS or login refused), the server answers `502 bad_gateway` with the same plain SMTP reason instead
of an opaque error — the TaskBrigade server is still healthy, the check only found the SMTP server failing.

## Data zones: installation and migration

The data zones ([overview](data-zones.md), guide [data store](data-store.md)) are **opt-in**. They
add the sandboxed runner, the output gate and the encrypted store outside Gitea. Docker installations use the compose
profiles `data` and `data-sftp` instead ([docker.md → Data zones](docker.md#data-zones-profiles-data-and-data-sftp)).
The database migrations and the new task/actor columns always run.

**Switch on:** pass `TBR_DATA_ZONES=1` to the install/update script, or set the key first and re-run the update so
the system is provisioned:

```bash
sudo deploy/update.sh TBR_DATA_ZONES=1          # idempotent; a second run changes nothing
# or
sudo /opt/taskbrigade/app/.venv/bin/tbr-admin config set TBR_DATA_ZONES 1
sudo deploy/update.sh
```

With the switch on, `install.sh`/`update.sh` create:

| Name | Kind | Purpose |
| --- | --- | --- |
| `tbr-data` | group | Read the KEK (`/etc/taskbrigade/data.key`) and the store below `TBR_DATA_DIR` |
| `tbr-runner` | system user (`--system --shell /usr/sbin/nologin`) | Runner service; member of `tbr-data` |
| `tbr-job` | system user, own group | User of every sandbox job; no access to the store or the KEK |
| `/srv/taskbrigade-data` (`TBR_DATA_DIR`) | `taskbrigade:tbr-data 0750` | Store root |
| `…/store` | `taskbrigade:tbr-data 0750`, files `0640` | Encrypted datasets |
| `…/incoming` | `taskbrigade:tbr-data 0750` | Encrypted upload spool |
| `…/quarantine` | `tbr-runner:tbr-data 0750` | Encrypted run outputs and logs before release |
| `/opt/taskbrigade/runner-venv` (`TBR_RUNNER_VENV`) | `root:root 0755` | Pinned analysis packages (`deploy/runner-requirements.txt`: `pandas`, `openpyxl`, `numpy`, `pyarrow`, `defusedxml`) |
| `/var/lib/taskbrigade-runner` | `tbr-runner:tbr-runner 0700` | Runner token and state |
| `/etc/taskbrigade/data.key` (`TBR_DATA_KEY_FILE`) | `root:tbr-data 0640` | KEK (created once, never overwritten) |
| `/etc/polkit-1/rules.d/60-taskbrigade-runner.rules` | root | Lets `tbr-runner` start/stop only `taskbrigade-job@*` |
| `taskbrigade.service.d/50-data-zones.conf` | root | `ReadWritePaths=TBR_DATA_DIR`, `SupplementaryGroups=tbr-data` |
| `taskbrigade-runner.service`, `taskbrigade-job@.service` | root | Runner service and sandbox template ([below](#data-zone-runner)); the runner starts only after `tbr-admin runner join` |
| `taskbrigade-job@.service.d/50-limits.conf` | root | Sandbox limits from *Admin → Settings* |

The `TBR_DATA_SFTP=1` switch additionally installs the native SFTP inbox: the group `tbr-sftp`, the chroot root
`/srv/taskbrigade-sftp` (`TBR_SFTP_DIR`), the sshd drop-in `/etc/ssh/sshd_config.d/60-taskbrigade-sftp.conf` and
the systemd path/service units `taskbrigade-data-import.{path,service}`. The drop-in is only installed when
`sshd -t` accepts the whole configuration; sshd is then reloaded. Deliverers are created per project with
`tbr-admin data sftp-user add …` (see [Data store → SFTP inbox](data-store.md#sftp-inbox-native)).

The switch must be combined with `TBR_DATA_ZONES=1`: the inbox imports into the encrypted store and needs the
data group and the KEK.

The drop-in and the chroot can be verified in a throwaway container without touching the production sshd:

```bash
docker run --rm -v "$PWD":/repo ubuntu:24.04 bash /repo/deploy/sftp/check-inbox.sh
```

It installs OpenSSH, applies the drop-in, provisions one deliverer like `tbr-admin data sftp-user add` and checks
SFTP upload, no shell, no path escape and no remote forwarding (10 checks).

**Prerequisites** (`prereq.sh` checks them only while the switch is on): systemd ≥ 249 (for `ProtectProc=invisible`
and `RestrictAddressFamilies` in the sandbox template) and polkit. Both are present on Ubuntu 24.04.

**Backups.** Keep the KEK (`/etc/taskbrigade/data.key`) **separate** from the database and from `TBR_DATA_DIR`:
without it neither the database nor the store files reveal data, and a leaked store plus the KEK would open
everything. `deploy/backup.sh` copies `TBR_DATA_DIR/store` as ciphertext when `TBR_BACKUP_DATA=1` is set; the KEK is
never written into that archive. Crypto-shredding (dropping a dataset's DEK) makes every copy, also in backups,
unreadable.

**Migration of an existing installation:**

1. The database migrations run always and add the new columns/tables without touching existing tasks
   (`data_class = NULL` keeps inheriting the project privacy).
2. `tasks.data_class`, `actors.model_location` etc. are prefilled conservatively; `privacy_enforce_model_location`
   stays **off** on existing installations until an admin has worked through the findings (see
   [architecture.md](architecture.md#privacy)). New installations start with it on.
3. The opt-in step above is idempotent. **Way back:** unset `TBR_DATA_ZONES` (`tbr-admin config unset TBR_DATA_ZONES`)
   and run `deploy/update.sh`: the runner/job units are stopped and disabled, the data and the users stay.
4. Pre-existing spreadsheet attachments in `local_only` projects: `tbr-admin data scan-attachments` lists them
   (report only, no history rewrite). The optional Gitea push guard is described
   [above](#gitea-data-zone-push-guard-native-installation).

## Data-zone runner

The runner ([overview](data-zones.md)) executes `run` tasks – a reviewed and merged script on a
dataset – without any language model and without network, and stores the outputs encrypted in the quarantine for the
output gate. It runs on the TaskBrigade host itself (it reads the store and the KEK there) and needs the data zones
switched on (`TBR_DATA_ZONES=1`, [above](#data-zones-installation-and-migration)).

**Set up.** *Admin → New agent* with kind **runner** (role worker, runtime local; the model location is set to
`none`, no Gitea user is needed). The setup page shows the join command; run it as root on the TaskBrigade host:

```bash
sudo /opt/taskbrigade/app/.venv/bin/tbr-admin runner join tmj_…   # writes runner.toml + token, starts the service
sudo -u tbr-runner /opt/taskbrigade/app/.venv/bin/tbr-admin runner check      # configuration, token, key, unit
sudo -u tbr-runner /opt/taskbrigade/app/.venv/bin/tbr-admin runner selftest   # isolation probes in the real sandbox
journalctl -u taskbrigade-runner -f                                            # claims, job ids, exit codes
```

`join` writes `/etc/taskbrigade/runner.toml` (`root:tbr-runner 0640`: server URL `http://127.0.0.1:8090` by default
(`--url`), token file, data paths) and the token to `/var/lib/taskbrigade-runner/token` (`tbr-runner 0600`).

**Run tasks.** A `run` task names the merged script and the input dataset. The
[task chain](data-store.md#task-chain-across-zones) creates it as a draft after a code task is merged, a workflow
`runner` step creates it per run ([workflows.md](workflows.md#agent-once-then-runner)), or you create it over the
API/CLI:

```json
POST /api/v1/tasks
{"project": "DEMO", "title": "Monthly totals", "description": "…", "acceptance_criteria": "…",
 "task_kind": "run", "assigned_to": "runner-1",
 "run": {"commit_sha": "<merge commit of the reviewed script task>", "script_path": "analysis/totals.py",
         "dataset": "DS-0003"}}
```

Runner tasks are always `sealed`; only runners get them, and runners get nothing else. `commit_sha` must be the merge
commit of an **approved task of the same project** (`tasks.merge_commit_sha`) – an unreviewed commit never runs.
CSV/TSV/XLSX uploads and imports automatically enqueue sealed profile tasks (`task_kind` `profile`). They run the
bundled profiler in the same sandbox, without a repository script. The generated schema and synthetic sample await
human release on the dataset detail page; see [Schema profiles and release](data-store.md#schema-profiles-and-release).

**Job flow.** Claim → job (`GET /api/v1/runner/jobs/<nr>`: dataset location and wrapped key, script reference,
limits) → script fetched by the server with its service token → static pre-check (network, subprocess, `ctypes`,
`importlib`, `exec`/`eval`, `os.system`, … refused before any data is decrypted) → job directory
`/run/taskbrigade-runner/jobs/<job>/{in,script,out,log}` (tmpfs; input decrypted and checked against the dataset's
SHA-256) → `systemctl start taskbrigade-job@<job>.service` → `in/` deleted at once → `out/*` and the logs encrypted
(new DEK, wrapped with the KEK) into `TBR_DATA_DIR/quarantine/<job>/` → `run_results` row with status `quarantine`.
After a successful job the runner starts a second sandbox job `<job>-gate` with TaskBrigade's own probe
(`app/runner/gate_probe.py`) against the input and `result.json` (output gate checks 4 and 5), then the input is
gone. The server runs the [output gate](data-store.md#output-gate): a result that passes the hard checks moves the
task to *For review* and waits in the release view; a failed hard check sends the task back (*Changes requested*,
script commit cleared). The review of a run task **is** the output gate, so approving or rejecting it like a code
task is refused (`output_gate_required`). Exit code 3 ("input does not match the schema"), a time limit, a killed
job or any other exit code abort the task. The task thread only gets exit code and
runtime, never output contents; journald only gets unit start/stop.

**Sandbox** (`taskbrigade-job@.service`): user `tbr-job`; `PrivateNetwork=yes`, `IPAddressDeny=any`,
`RestrictAddressFamilies=AF_UNIX`; `ProtectSystem=strict`, `ProtectHome`, private `/tmp` and devices, no
capabilities, `NoNewPrivileges`, system call filter (refused calls fail with `EPERM`); only the four job directories
are visible under `/job` (`in`, `script` read-only); store, KEK, app state and runner state are inaccessible. Limits:
`MemoryMax`, `CPUQuota`, `TimeoutStartSec` (a oneshot unit's run time – `RuntimeMaxSec` does not apply to it) from
*Admin → Settings → Data runner* (defaults 4096 MB, 200 %, 900 s), `TasksMax=64`, `LimitFSIZE=256M`,
`LimitNOFILE=256`. stdout/stderr go to files in the job directory (`append:`), never into the journal. After changing
the limits in the UI:

```bash
sudo /opt/taskbrigade/app/.venv/bin/tbr-admin runner limits --write   # writes the drop-in, daemon-reload
```

(`update.sh` does the same on every run.) Only the polkit rule `60-taskbrigade-runner.rules` lets `tbr-runner`
start/stop `taskbrigade-job@*` – nothing else, no `set-property`, no `daemon-reload`.

**Isolation test.** `deploy/sandbox-test/run.sh` builds a throwaway systemd container (Docker, privileged, no data),
installs users, units and polkit rule from the checkout, runs the probe jobs of `tbr-admin runner selftest` (network,
reads outside the job – KEK, store, quarantine, app state –, writes outside `out/`, planted symlinks, memory, tasks,
file size, time limit), checks the polkit scope and runs end-to-end runner jobs (decrypt → sandbox → encrypted
quarantine, exit code 3, pre-check) both as `tbr-runner` and inside `taskbrigade-runner.service`. Exit code 0 only if
every check passes. On a real host, `tbr-admin runner selftest` (as `tbr-runner`) runs the same probes against the
installed units; `--all` also waits for the time limit.

**Script contract.** TASK.md of a work task shows the contract when its project holds datasets or a `run` task
depends on it: one file, input only from `TBR_INPUT` (`data.csv`, `data.xlsx`, … by the dataset's type), output only
`TBR_OUTPUT_DIR/result.json` in the format `tbr-aggregate/1`, no network/subprocesses/dynamic code, aggregates only,
exit code 0 = success, **3 = the input does not match the expected schema**, anything else = failure. Only the
standard library and the pinned packages of `deploy/runner-requirements.txt` exist in the sandbox.

**Sizing.** Job directories live on `/run` (tmpfs, on Ubuntu 10 % of RAM by default). What a job writes counts
against its memory limit, the decrypted input does not: keep the largest dataset plus `runner_memory_mb` below the
free space of `/run` (`df -h /run`), or give `/run` a larger `size=` in `/etc/fstab`.

## Data store for confidential datasets

Spreadsheets and other confidential files never go into task attachments (they would land in Git). The optional
data store keeps them encrypted outside Gitea under `TBR_DATA_DIR`, with its own key (`tbr-admin init-data-key`),
rights, retention and an append-only audit. Set-up, key handling and backups: [Data store](data-store.md).

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

## Gitea data-zone push guard (native installation)

The optional `tbr-admin data gitea-hook` installer places an executable
`hooks/pre-receive.d/taskbrigade-data-guard` inside each selected bare repository. It uses the
[Gitea 1.24 hook dispatcher](https://github.com/go-gitea/gitea/blob/v1.24.3/modules/gitrepo/hooks.go),
which runs every executable in `pre-receive.d` and rejects the push if one returns a nonzero status.
Gitea's own `pre-receive` and `pre-receive.d/gitea` files and other custom hooks remain intact.
This is a server filesystem operation, separate from the Gitea UI hook editor; keep
`[security] DISABLE_GIT_HOOKS=true` so repository users cannot edit server hooks through the UI.

Selected repositories belong to projects whose default class is `local_only`, projects with any
explicit `local_only`/`sealed` task, or projects with any dataset (including archived/deleted records).
Ordinary `cloud_ok` projects without those records are unaffected. Selection does not depend on the
global `data_enabled` menu switch; hiding Data must not remove protection. If projects share a repository,
any selected project protects the whole repository.

Install **on the Gitea host**, with the TaskBrigade application installed and its configuration/database
available to `tbr-admin`. The hook itself needs only `python3` and `git` in the Gitea service user's PATH;
it has no database, app imports, credentials, or network dependency. Use Gitea 1.24's native hook layout
and verify the dispatcher on other versions before use. Find the actual `[repository] ROOT` in Gitea's
`app.ini`; the example below uses `/var/lib/gitea/data/gitea-repositories`. Do not substitute a checkout.
The installer refuses missing bare repositories/dispatchers, unsafe names, symlinked paths and unmanaged
hook files. If the dispatcher is missing or not executable, resynchronize hooks through Gitea first.

Run with permission to read the TaskBrigade configuration/database and write the selected Gitea hook
directories (usually root for the installation only). For example, from the native app directory:

```bash
sudo TBR_ENV_FILE=/etc/taskbrigade/taskbrigade.env .venv/bin/tbr-admin data gitea-hook install --repo-root /var/lib/gitea/data/gitea-repositories --dry-run
sudo TBR_ENV_FILE=/etc/taskbrigade/taskbrigade.env .venv/bin/tbr-admin data gitea-hook install --repo-root /var/lib/gitea/data/gitea-repositories
sudo TBR_ENV_FILE=/etc/taskbrigade/taskbrigade.env .venv/bin/tbr-admin data gitea-hook status --repo-root /var/lib/gitea/data/gitea-repositories
```

Installation is idempotent and atomically replaces only the managed file (mode `0755`). Check that the
Gitea service user can traverse the parent directories and execute it. `status` exits 1 for a missing,
outdated, non-executable, or unmanaged guard, or a lookup/path error. A failing batch reports each failed
repository, exits 1, and leaves successful installs in place; correct the error and rerun.
The hook payload is packaged in the wheel; `tbr-admin data gitea-hook payload` prints it without DB access
for inspection or transport to a separate Gitea host.

**Rerun install and status after creating a project/repository, changing its repository mapping or privacy,
adding a restrictive task or its first dataset, and after updating TaskBrigade.** Selection is a snapshot,
not a push-time database lookup. There is no automatic reconciliation service. Install before allowing
data-zone work, and restrict writes during these changes. Existing guards stay installed if a project
later becomes cloud-only. Before deleting a project or changing its repository mapping, remove its guard
explicitly if appropriate; the database can no longer locate the former repository afterwards.

The guard rejects filenames ending in `.xlsx`, `.xls`, `.xlsm`, `.csv`, `.tsv`, `.ods`, `.zip`,
case-insensitively, using the same list as the attachment lock. It scans complete trees for the new tip
and every newly reachable commit relative to the old ref, so merges, renames and an add-then-delete
sequence cannot conceal data. New branches/tags scan their whole reachable history. Annotated commit
tags are supported; tags pointing at trees/blobs are rejected. Ref deletion is allowed. Git's incoming
object quarantine environment is preserved. Inspection errors fail closed.

A rejection names the offending path and asks the pusher to use TaskBrigade's encrypted **Data** upload.
Remove it from **every incoming commit**, not just the last tree, before retrying. Even synthetic CSVs
are blocked in these repositories; represent fixtures as source code/JSON instead. Existing history is
not rewritten or removed: use `tbr-admin data scan-attachments` to start reviewing known attachments.
This guard detects extensions only, with no content sniffing or size cutoff (DZ 2 defines no size
threshold). Renamed data, Git LFS objects, direct filesystem writes, and server import paths which bypass
receive-pack require separate controls. A guard is an additional protection, not a raw-data storage path.

Before rollout, use a disposable data-zone project and an ordinary cloud project on a staging Gitea:
verify code pushes succeed in both, a `.CSV` push is rejected only in the data-zone repository, and an
add-then-delete data commit and annotated tag are also rejected there. Never test with real data.

To uninstall, preview first and then remove only managed guards from **all currently mapped repositories**,
including former data-zone projects. Existing Gitea hooks and repository data remain:

```bash
sudo TBR_ENV_FILE=/etc/taskbrigade/taskbrigade.env .venv/bin/tbr-admin data gitea-hook uninstall --repo-root /var/lib/gitea/data/gitea-repositories --dry-run
sudo TBR_ENV_FILE=/etc/taskbrigade/taskbrigade.env .venv/bin/tbr-admin data gitea-hook uninstall --repo-root /var/lib/gitea/data/gitea-repositories
```

No production hook is installed by the application update, by `TBR_DATA_ZONES=1` or by running the test suite;
install it explicitly as above.

## Orchestrating from the CLI

Orchestrators (role `orchestrator`/`admin`) can maintain tasks entirely from the command line:

```bash
tbr create --project DEMO --title "…" --assign codex --due 2026-10-31 --description "…" --acceptance "…"
tbr update DEMO-0042 --assign opencode --due 2026-11-01 --prio 2   # PATCH with If-Match (version check)
tbr cancel DEMO-0042 --reason "duplicate"                          # → cancelled
tbr reset DEMO-0042                                                # → defined (same permissions as the web UI)
tbr duplicate DEMO-0042 --assign codex                             # new draft "(duplicate) …", prints its number
tbr update DEMO-0042 --reviewer review-agent                       # reviewer (human or reviewer agent); none removes it
tbr update DEMO-0050 --depends-on DEMO-0042 --depends-on DEMO-0043  # replaces the dependencies; none removes all
tbr update DEMO-0042 --data-class sealed                           # data class (stricter always; inherit = project's)
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
again after completing them. A warning lists released tasks that wait for a task that is not done and has no sprint,
sits in a planned sprint or in a later one (`waiting` in the API response). `tbr show` and the agent's `TASK.md` show the sprint with its status and goal. MCP
offers the same: `list_sprints`, `show_sprint`, `create_sprint`, `add_sprint_tasks`, `release_sprint`,
`close_sprint` and the `sprint` parameter of `create_task`/`update_task`.

In the web UI, *Backlog → New sprint* (shown to admins, orchestrators and project owners) opens the
sprint editor: header data on top, task headings on the left (Enter adds one, pasting several lines adds one per
line, drag to reorder, × removes), details of the selected heading on the right, with a ready/missing hint per
heading. *Save* creates or updates the planned sprint and all its drafts in one transaction; *Save and release
sprint* then runs the normal release. A planned sprint opens in the same editor via
*Edit in the sprint editor* on its sprint page; tasks past draft are read-only there, and removing a
saved draft cancels it.

## Workflows and scheduler

What workflows are and how to use them: [workflows.md](workflows.md). The workflow scheduler is an idempotent,
database-locked tick that processes due schedules, queued runs, event triggers, terminal tasks, timeouts, retries and reminders (rules in [workflow-schedules.md](workflow-schedules.md)). The trigger is an
operations choice, not part of the workflow logic:

- **Native installation**: the systemd units `taskbrigade-workflows.{service,timer}` call
  `tbr-admin workflows tick` once a minute. `deploy/install.sh` and `deploy/update.sh` install the units;
  `update.sh` stops the timer around the schema upgrade and starts it again afterwards (the tick also refuses to
  run while the schema is not at `head`).
- **Docker**: the app process runs the same tick as an APScheduler job each minute (the image defaults to
  `TBR_WORKFLOW_TICK=app`); no timer unit is needed in the container.

`TBR_WORKFLOW_TICK=timer|app|off` selects the trigger. The default is `timer` natively and `app` in the Docker
image. Set `off` to stop the automatic tick; the manual CLI/API entry points still work:

```bash
sudo systemctl list-timers taskbrigade-workflows.timer      # native: is the timer running?
sudo /opt/taskbrigade/app/.venv/bin/tbr-admin workflows tick          # run one tick manually
sudo /opt/taskbrigade/app/.venv/bin/tbr-admin workflows schedules     # next runs of every schedule
sudo /opt/taskbrigade/app/.venv/bin/tbr-admin workflows calendar check DE-BW 2027-01
```

*Admin → Settings* shows the last tick and warns when it is older than five minutes while workflows are enabled;
`tbr-admin db-status` reports the same on the server, and `tbr doctor` on an agent machine warns about a stale tick.
Workflows stay disabled (`workflows_enabled`) after an update; enable them under *Admin → Settings → Workflows*.
Schedules run in their own time zone (default `TZ_DISPLAY`); the web UI previews the next runs with their UTC offset.
