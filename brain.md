# Taskmeister – Developer Notes

Shared knowledge for everyone working on this repository, human contributors and coding agents alike.
Keep entries short and factual; add new findings in the same pull request as the change that produced them.

## Purpose

TaskBrigade (formerly Taskmeister) is a task manager for humans and AI coding agents. Humans create tasks and review
results; agents claim tasks via API/CLI, work on their own Git branch, ask questions or submit a pull request (Gitea).

## Architecture & stack

- Python 3.12, FastAPI + Jinja2/HTMX web UI (`app/`), SQLAlchemy + Alembic on MariaDB 10.5+ (`migrations/`).
- CLI `tbr` for agents and orchestrators, `tbr-admin` for operators, MCP server `tbr mcp` (`cli/`); package
  `taskbrigade`. `tm`/`tm-admin` are transition aliases (`cli/legacy.py`).
- Deployment without Docker: systemd units and scripts in `deploy/`; agent integrations in `deploy/agents/`.
- Task workflow and permissions: state machine in `app/services/state_machine.py`.

## Conventions

- Web UI and service/API messages use English message ids with a German catalog (`app/i18n.py`,
  `app/locales/de.json`); default language `en`. Agent-facing texts (TASK.md instructions, runner prompts, MCP
  instructions/tool descriptions/replies, `tm` help and messages) are English with a separate German catalog
  (`cli/i18n.py`, `cli/locales/de.json`). README, `docs/`, CHANGELOG, skills in `deploy/agents/` and code identifiers
  are English. Docs name German UI labels verbatim with an English gloss, e.g. *Admin → Benutzer & Agenten*,
  „Token rotieren“ (rotate token).
- Commit messages: short imperative summary; reference the task number if there is one.
- Never commit secrets, real hostnames/IPs, personal names or internal project keys (see "Pitfalls").

## Build / run / test

- `uv run pytest -q` – unit tests; DB tests (`-m db`, e.g. `tests/test_ui_cli.py`) are skipped without a test DB.
- DB tests with a throwaway MariaDB: `docker run -d --rm --name tm-test-mariadb -e MARIADB_ROOT_PASSWORD=root
  -e MARIADB_DATABASE=tm_test -e MARIADB_USER=tm -e MARIADB_PASSWORD=<pw> -p 127.0.0.1:33306:3306 mariadb:10.5`, then
  `TBR_TEST_DATABASE_URL="mysql+pymysql://tm:<pw>@127.0.0.1:33306/tm_test?charset=utf8mb4" uv run pytest -m db`
  (the old `TM_TEST_DATABASE_URL` is still read). The database name must end in `_test`. Use a long password:
  `test_backup` checks that it does not appear in a `/tmp/tm-db-*.cnf` path.
- Docker: `docker compose up -d` (needs `.env` with `TBR_DB_PASSWORD`, `MARIADB_ROOT_PASSWORD`,
  `TBR_INITIAL_ADMIN_PASSWORD`); guide `docs/docker.md`. Multi-arch: `docker buildx build --platform
  linux/amd64,linux/arm64 .` (needs QEMU/binfmt for the foreign platform). Remove a compose `.env` from the repo root
  before `uv run pytest`: the app reads `./.env`, so its `TBR_DB_PASSWORD` (or an old `TM_DB_PASSWORD`) switches
  tests into container mode. The image build copies `deploy/agents` (`.dockerignore` and `Dockerfile`): the wheel
  force-includes it, without it `uv build` fails.
- Lint: `uvx ruff@0.14.0 check .` (settings in `pyproject.toml`; tests may re-import fixtures, F811/E702 ignored
  there). `ruff format` is not enforced – most files would be reformatted.
- CI: `.github/workflows/ci.yml` (lint, test with a `mariadb:10.5` service on host port 33306, multi-arch `image`,
  `smoke`, `release` to GHCR `ghcr.io/taskbrigade/taskbrigade` on tags `v*` only); guide `docs/ci.md`. Compose smoke
  test locally: `deploy/docker/smoke-test.sh` (own project `tm-smoke`, ports 18090/13300, subnet `172.31.251.0/24`,
  removed afterwards; `TBR_SMOKE_KEEP=1` keeps it).
- README screenshots: `python3 deploy/release/screenshots.py` (`--keep` leaves the stack running). Own Compose project
  `tbr-shots` with the smoke test's ports 18090/13300/12222 and subnet `172.31.251.0/24` – never run both at once.
  Demo data comes from `deploy/release/screenshot_seed.py`, piped into `docker compose exec -T taskbrigade python -`;
  it prints `SEED {json}` with id-dependent page paths (sprint editor). Capture runs in the Playwright image with
  `--user <uid>:<gid>`, `HOME=/tmp` and the repo mounted read-only; images are 256-colour PNGs (40–85 KiB).

## Decisions (date – decision – reason)

- 2026-10-07 – Public project files: Apache-2.0 license (confirmed 2026-10-10). Public GitHub repository
  `taskbrigade/taskbrigade` (URLs in `pyproject.toml`); development stays in the internal Gitea, releases are exported
  cleaned (`deploy/release/export-public.sh`) and mirrored to GitHub through a separate release repository.
  Bundled in `app/static/vendor`: Pico.css 2.0.6 (MIT), htmx 2.0.4 (0BSD, not BSD-2-Clause), Sortable 1.15.6
  (MIT). Python dependencies are not bundled. (Bricolage Grotesque was removed with the TaskBrigade logo.)
- 2026-10-07 – `tm update` always sends `If-Match` (default: the `row_version` just read) – concurrent edits
  surface as 409 instead of being overwritten silently.
- 2026-10-07 – `tm cancel`/`tm reset` use `POST /tasks/{nr}/transition`, not dedicated endpoints – so exactly the
  web UI's permissions apply (state machine `app/services/state_machine.py`).
- 2026-10-07 – Bootstrap admin actor defaults to `admin` (`BOOTSTRAP_ADMIN_ACTOR`); migration 0001 seeds that
  actor. Migration 0005 assigns existing projects to `DEFAULT_PROJECT_OWNER`, falling back to each project's
  creator. Both are read from settings, so existing installations keep their behaviour via `.env`.
- 2026-10-08 – brain-changes instead of direct `brain.md` edits – parallel tasks kept colliding in `brain.md`.
  Agents write `brain-changes/<TASK-NR>.md` (add/change/remove per section); `state_machine._approve` checks
  change/remove against `brain.md` on the base branch before merging (`brain_conflict`, no merge) and applies the
  file after the merge in a separate service-user commit (`app/services/brain_changes.py`).
- 2026-10-07 – Own dict-based translation layer instead of Babel/gettext – no compile step and no extra
  dependency; English source strings are the msgids, `app/locales/<lang>.json` holds translations (plural entries
  as lists). Jinja uses `jinja2.ext.i18n` (newstyle). Language: cookie `tm_lang` (per browser, EN/DE switcher) →
  admin setting `ui_language` → `.env` `UI_LANGUAGE` → `en`. Per-user language in the DB is not implemented.
- 2026-10-08 – Docker entrypoint `app/container.py` (wait for DB → `alembic upgrade head` → initial admin → exec
  Uvicorn) instead of setup mode – `TM_DB_PASSWORD` set makes `TM_DB_*` authoritative (`dbconfig.from_env`), db.json
  is ignored and Admin → Database refuses to save. Native installation unchanged (variable empty).
- 2026-10-08 – `TRUSTED_PEER_NETWORKS` (CIDRs treated like loopback, `Settings.is_trusted_peer`) for the network
  policy, `Tailscale-User-Login` and git credentials – a published Docker port arrives from the bridge gateway, not
  loopback. Compose pins the subnet `172.31.250.0/24` and trusts only the gateway `.1/32`.
- 2026-10-08 – `TM_INITIAL_ADMIN_PASSWORD` is applied only while the bootstrap actor has no password; it never
  overwrites a UI change. Without `SECRET_KEY` the container persists `secret.key` in the state volume.
- 2026-10-08 – `GITEA_PUBLIC_URL` (setting `gitea_public_url`) next to `GITEA_URL` – in Docker Compose the app
  reaches Gitea as `http://gitea:3000`, agents as `http://localhost:3000`. Clone URLs, `/agents/me/git-credentials`
  (`tm git-credential` matches host+port exactly), `project.gitea_url` and PR links use the public address
  (`settings_store.gitea_public_url`, `public_gitea_link`); API calls keep `GITEA_URL`. Empty = `GITEA_URL`.
- 2026-10-08 – Compose profile `gitea`: `gitea-admin` creates the first Gitea admin with the Gitea CLI (the API
  cannot), `gitea-init` (`app/gitea_init.py`, idempotent) does the rest via the admin API with basic auth and
  stores URLs/token in `app_settings` – the app container does not see the init container's environment.
- 2026-10-08 – Docker network access in three tiers: `TRUSTED_PEER_NETWORKS` (like loopback), `API_NETWORKS` (UI +
  API with tokens over HTTP, explicit opt-in, empty by default; no `Tailscale-User-Login`, no git credentials),
  `LAN_UI_NETWORKS` (UI only) – LAN agents without Tailscale need the API, but cleartext tokens must not imply
  header or Gitea-token trust. All three are validated at startup (invalid CIDR or `/0` → error).
- 2026-10-08 – Tailscale in Docker as an optional second compose file `compose.tailscale.yaml` (enable with
  `COMPOSE_FILE=compose.yaml:compose.tailscale.yaml`), not a profile: `network_mode: service:tailscale` needs
  `ports: !reset []`/`networks: !reset {}` on the app service, which a profile cannot express. `tailscale serve`
  then proxies to `127.0.0.1:8090` in the shared namespace, so the identity header arrives from loopback.
- 2026-10-08 – `CHANGELOG.md` via `changelog.d/<TASK-NR>.md` fragments and per-project option "one review at a time
  per agent" (`projects.serialize_reviews`, migration 0006) – parallel tasks of one agent kept colliding in
  `CHANGELOG.md`, `.env.example` and docs. Fragments are applied together with brain-changes in one service-user
  commit after the merge (`state_machine._apply_repo_changes`); claim skips tasks while the agent has a `for_review`
  task in the same project and prefers `changes_requested` over new work at equal priority.
- 2026-10-08 – GHCR image `ghcr.io/taskbrigade/taskbrigade` in the workflow's `IMAGE`, pushed only by the `release`
  job on tags `v*`; branches and pull requests never publish.
- 2026-10-08 – `tm run` resumes an own *in_progress* task when no session runs for it, before claiming new work – a
  crashed session or reboot left tasks stuck. "Running" = `tm session` holds `<work_root>/.locks/session-<NR>.lock`
  (flock, taken first thing in `session()`) or a live tmux window named after the task (covers the gap between
  launch and lock). Prerequisites (tmux in tmux mode, agent command) are checked before the claim; any start failure
  after the claim releases the task with the reason (`give_back`).
- 2026-10-08 – The server delivers `tm` in its own version: `GET /install.sh` (POSIX sh, installs uv per user, then
  `uv tool install --force --python 3.12 <url>/download/<wheel>`) and `GET /download/taskmeister-<version>-py3-none-any.whl`
  (`app/web/downloads.py`, `app/services/distribution.py`) – agent machines need no Gitea access. Wheel lookup:
  `TM_DIST_DIR` (default `<state_dir>/dist`), then `<source>/dist` (Docker image `/app/dist`, built by
  `deploy/install.sh`/`update.sh`); if missing, one lazy `uv build` with a 300 s back-off. Download and install.sh
  follow the API rule of the network policy (`AGENT_DOWNLOADS` in `app/main.py`), no login. Every response carries
  `X-Taskmeister-Version`; `cli/client.Client` warns once per process on stderr when `tm` is older, MCP `list_tasks`
  prepends the warning, `tm self-update` installs the server's version via uv.
- 2026-10-08 – Agent onboarding with one-time join codes (`app/services/onboarding.py`, migration 0007 `join_codes`):
  *Admin → Neuer Agent* (`app/web/admin_agents.py`) creates the agent without an API token and a code `tmj_…`
  (only the SHA-256 is stored, default 15 min, setting `join_code_minutes`; a new code deletes the agent's unused
  ones). `POST /api/v1/join` (no bearer, `_secure_channel` like git credentials, failures count in `api_limiter`)
  marks the code used with a conditional `UPDATE … WHERE used_at IS NULL` and rotates the agent's token; errors
  `join_code_invalid` (404), `join_code_used`/`join_code_expired` (410). `tm join` (`cli/join.py`) refuses an existing
  `.taskmeister.toml` before redeeming, so the code stays valid without `--force`.
- 2026-10-08 – Optional secret setting `gitea_admin_token` (not in `.env`) for creating agent Gitea users – the
  service user stays without admin rights. The admin token only calls `POST /admin/users`; the agent's token is
  created with the new user's own random password via basic auth, because Gitea accepts token creation
  (`POST /users/{u}/tokens`) only with basic auth, never with a token. `gitea-init` stores a token of the Gitea admin
  (`taskmeister-admin`, scopes `write:admin`, `read:user`). Without the setting the setup page shows the manual step.
- 2026-10-08 – `tm setup <kind>` (`cli/setup.py`) writes per-agent timer units with absolute paths instead of copying
  the `tm-runner@.service` template: systemd `~/.config/systemd/user/tm-runner-<agent>.{service,timer}`
  (`OnActiveSec=1min` so the first pass runs right after enabling, `KillMode=process`), launchd
  `~/Library/LaunchAgents/com.taskmeister.runner.<agent>.plist` (`AbandonProcessGroup` for the tmux server). Work
  directories outside `~/taskmeister-work/<agent>` work, and the PATH comes from where tm, git, tmux and the agent
  were found. `tm doctor` (`cli/doctor.py`) also accepts the older template timer `tm-runner@<agent>.timer`. Both
  share `Check`/`render` (ok/warn/error + hint); doctor exits 1 only on errors (outdated tm, missing timer and
  hanging tasks are warnings). Prerequisite checks come from `cli/runner.missing_prerequisites`, which also looks
  behind `env VAR=… <agent>` (OpenCode command).
- 2026-10-08 – Migration 0001 seeds only the bootstrap admin, no sample agents – the earlier seed (personal agent
  names) is unsuitable for a public project and showed new users agents they do not have. Edited in place instead of
  a new migration that deletes actors, so installations migrated earlier keep their agents
  (`tests/test_db_integration.py::test_existing_actors_survive_upgrade`).
- 2026-10-08 – `tm setup` for non-runner agents lives in `cli/setup_apps.py` (`claude-desktop`, `codex-desktop`,
  `hermes`, `openclaw`). App configs are merged, never rewritten: JSON via `json`, Codex TOML by replacing only the
  `[mcp_servers.<name>]` table (+ sub-tables) as text and accepting the result only if `tomllib` parses it and it
  differs from the old file only in that table (no TOML writer dependency). Changed files get `<file>.bak`, unchanged
  ones are not written. Framework jobs (`taskmeister-<agent>`) are created only when `<cli> … --help` lists the needed
  flags, otherwise the exact command is printed – the Hermes/OpenClaw CLIs change between versions. Nothing is
  restarted without `--restart`.
- 2026-10-08 – The wheel ships `deploy/agents/{hermes,openclaw}` and the Cowork skill zip as `cli/agent_files`
  (hatch `force-include`); `setup_apps.agent_file` falls back to `deploy/agents` in a source checkout. Edit only
  `deploy/agents`.
- 2026-10-08 – `tm-admin config get|set|unset|list` (`cli/admin_config.py`) edits the env file the service really
  reads: `TM_ENV_FILE` → first `EnvironmentFile=` of `taskmeister.service` (`systemctl show`) →
  `/etc/taskmeister/taskmeister.env`. Keys are the `Settings` fields plus `EXTRA_KEYS` (read only by the systemd
  unit/scripts: `TM_BIND_HOST`, `TM_MAX_ATTACHMENT_MB`, `TM_BACKUP_*`); values are validated by a `Settings`
  subclass with only the init source (`_OnlyInit`, no environment or cwd `.env` leaks in) plus `VALUE_CHECKS`.
  Secrets = `repr=False` fields + `DATABASE_URL`, always masked. `TM_DB_PASSWORD`, `DATABASE_URL` and
  `TM_INITIAL_ADMIN_PASSWORD` are refused natively (container mode / password in the file), `unset` is allowed.
  Writes are atomic and keep owner/group/mode; a commented example line `# KEY=…` is replaced in place. In a
  container (`/.dockerenv` or `TM_DB_PASSWORD` set) `set`/`unset` refuse with a hint to `.env`, `get`/`list` show
  the container environment. A new setting needs no extra work; a new variable read outside `Settings` belongs in
  `EXTRA_KEYS` (`tests/test_admin_config.py` checks every server key in `.env.example`).
- 2026-10-09 – Product name TaskBrigade; the technical identifiers followed in 0.8 (decision 2026-10-10, technical
  rename). Visible texts (UI en/de, page titles, TASK.md/runner/MCP texts, skills, docs) say TaskBrigade.
- 2026-10-09 – Brand and colours: design tokens (`--tb-*`, status colours `--st-*`) live only at the top of
  `app/static/app.css` and are mapped onto Pico variables. The palette is light-only: `base.html` sets
  `data-theme="light"`, so Pico's automatic dark mode is off. Brand assets in `app/static/brand/` are generated by
  `mediaassets/build_brand.py` (`uv run --no-project --with pillow --with resvg-py python mediaassets/build_brand.py`):
  the mark is redrawn as SVG, the header/README logo is the freed and recoloured source PNG, and the favicon is a
  simplified bold mark (the full mark is unreadable at 32 px).
- 2026-10-09 – `tm session` supervises the agent itself (`runner.Supervisor`, checked every `check_s`, default 60 s)
  instead of `tm run` inspecting windows: it owns the process handle and finds its own pane through `TMUX_PANE`
  (only with `mode = "tmux"`; foreground runs have no start check or stall detection). Task left *in progress* →
  stop after `stop_after_finish_seconds` with `_stop_agent`, no `finish`. Start check blocker → `_stop_agent`,
  `release` with the reason (the server's release already writes the reason as a system comment, so no extra
  comment) and `<work_root>/.agent-blocked` (JSON); `run_once` returns early while it exists and `tm doctor` removes
  it once the agent's login status command succeeds. Stall → one regular comment via `POST /tasks/{nr}/comments`
  (agents cannot post `system` comments).
- 2026-10-09 – Every `tm run` closes finished tmux windows older than `close_finished_windows_after_hours`
  (`cleanup_windows`, age from `pane_dead_time`, else the newest `logs/<NR>-*.log` mtime; unknown age is kept), after
  appending `capture-pane -S -` to that log. `tm cleanup` is the manual variant (`--stale` also closes live task
  windows whose task is no longer in progress).
- 2026-10-09 – Sprints as an optional level project → sprint → task (`app/services/sprints.py`, migration 0008,
  API `/api/v1/projects/{key}/sprints`). Sprint rules live in the service, not the UI: a planned sprint forces new
  tasks to `draft` (API adds `notice`) and returns assigned `defined` tasks to draft via `to_draft`; release reuses
  the `publish` transition per draft (`state_machine.missing_definition` lists missing fields); close only changes
  `sprint_id` of open tasks (done/cancelled stay). Sprints are addressed by numeric id (`sprint` in task payloads,
  `GET /tasks?sprint=<id>|none`); names are unique per project. Planning rights: admin/orchestrator/owner
  (`sprints.can_plan`); release/close: `access.require_manage_project`.
- 2026-10-09 – CLI `tbr`/`tbr-admin`, distribution `taskbrigade` (0.5.0); `tbr` is taken as a package name on
  PyPI/npm/crates/RubyGems, so only the console script is called `tbr`. Python modules stay
  `app`/`cli`. `tm`/`tm-admin` are entry points of the same package (`cli/legacy.py`): same commands plus one rename
  note per call on stderr, off with `TBR_NO_RENAME_NOTICE=1`, never for `git-credential`, `mcp` and
  `prepare --wake-gate`. `cli.prepare.tm_command()` finds `tbr` (PATH, then next to the running program) before `tm`,
  so TASK.md, credential helpers, MCP entries, timers and framework scripts written from now on say `tbr`. The
  runner allows `Bash(tbr:*)` and `Bash(tm:*)` plus both absolute paths (`runner.claude_cli_rules`).
- 2026-10-09 – Switching a machine from the uv tool `taskmeister` to `taskbrigade` without a gap: `uv tool install
  --force` (takes over `tm`/`tm-admin`), then delete only `$(uv tool dir)/taskmeister`, and only while no command in
  `uv tool dir --bin` still links into it (`cli/selfupdate.py`, same steps in `install.sh`). `uv tool uninstall
  taskmeister` would delete the links that now belong to `taskbrigade` (checked with uv 0.11.21,
  `tests/test_rename.py::test_uv_uninstall_would_remove_the_taken_over_links`). An old `tm self-update` requests
  `taskmeister-<v>-py3-none-any.whl`, and uv refuses a wheel whose `.dist-info` names another package – the server
  therefore serves a bridge wheel under that name (`distribution.bridge_wheel`: renamed `.dist-info`, `Name:` in
  METADATA, new RECORD), written next to the real wheel or into `build_dir()` if that is read-only (Docker `/app/dist`).
- 2026-10-09 – `tbr setup <kind>` first calls `cli.migrate.migrate()` (backups `<file>.bak`, idempotent; nothing is
  pointed at `tbr` while no `tbr` exists): repo credential helpers, MCP entries of this work directory,
  `opencode.json`; since 0.8 also the renames of the technical names (decision 2026-10-10). Hermes script and
  OpenClaw trigger are rewritten by their own setup. `tbr doctor` adds `migrate.leftovers()` (labels `tm → tbr` and
  `taskmeister → taskbrigade`, warnings only; also the global credential helper and an installation that is still
  the package `taskmeister`).
- 2026-10-09 – Parallel-task limit default 2 (`actors.max_parallel_tasks`, migration 0009): only agents still on the
  old default 1 are raised to 2 – a raised limit was set on purpose, a 1 cannot be told apart from the default.
  Claim at the limit: a named task (`task_nr`) fails with `409 parallel_limit` naming the running tasks; a claim
  without a number keeps returning 204 (runners rely on it), and `tbr claim`/`tbr prepare`/MCP `start_task` explain
  it client-side (`cli.client.parallel_limit_note`). *Admin → Benutzer & Agenten* shows "in progress / limit".
- 2026-10-09 – Open points and follow-up tasks: submit takes optional `open_points` (list of strings, stored as JSON in
  `tasks.open_points`, cleaned by `state_machine.clean_open_points`, max. 50 × 1000 chars, also appended to the PR
  text); `tbr submit --open-point` (repeatable), MCP `submit_task(open_points=…)`, `.tm-submit.md` section
  `## Open points` (`cli.client.split_open_points`). Follow-ups are linked by `tasks.follow_up_of_id` (not
  `parent_task_id`, which means subtask); UI `/tasks/<NR>/follow-up` (`tasks.create_follow_up`) creates a draft in the
  same project with the selected points in the description (list) or acceptance criteria (checklist); API
  `POST /tasks` accepts `follow_up_of`.
- 2026-10-10 – `tbr setup openclaw` works per OpenClaw agent (`--openclaw-agent`, default: the agent of the existing
  automation `taskmeister-<agent>`, else `main`): the skill goes to that agent's workspace (`openclaw agents list
  --json` → `agents.entries.<id>.workspace` in `openclaw.json` → `workspace`/`workspace-<id>`; an unknown id is
  refused), the automation is created with `--agent --session isolated --no-deliver --timeout-seconds 3600`. OpenClaw
  embeds the trigger script into the automation, so an existing automation is refreshed with `automations edit <id>
  --trigger-script` when the embedded script differs or is not visible; `tbr doctor` compares the embedded script and
  checks the file in the work directory only as a fallback. Reason: on a gateway with several agents, setup used to
  overwrite `main`'s skill and create automations for `main`.
- 2026-10-10 – White work area (`--tb-bg: #FFFFFF`) and a kanban that fits the page – feedback after the 0.5.0
  branding: the grey area and the scrolling board were rejected. Surfaces are separated by `--tb-line` borders only;
  hover areas on white use `--tb-hover`, kanban columns `--tb-col`. `.kanban` gets the column count as
  `style="--cols: N"` and uses `repeat(var(--cols), minmax(0, 1fr))` from 1024 px on; below that it scrolls
  horizontally (`grid-auto-columns: minmax(12rem, 1fr)`).
- 2026-10-10 – Sprint editor (`app/web/sprint_editor.py`, `sprint_editor.html`, `static/sprint_editor.js`): one page
  for header data, task headings and details; saving goes through `sprints.save_plan` (create/update sprint,
  `batch_create` for new entries, `tasks.update` with `row_version` for drafts, `cancel` for removed drafts) in one
  web session, "save and release" adds `sprints.release` to the same transaction. Only planned sprints; tasks past
  draft are read-only and only keep their position. The list order is stored in `tasks.sprint_rank` (migration
  0010) – prio stays a separate field ("Prio aus Reihenfolge" fills it from the order on request). Sprint page and
  editor sort by `sprints.ordered_tasks` (rank, then prio, id).
- 2026-10-10 – Telegram notifications via an own bot and long polling (`app/services/telegram.py` API client,
  `notifications.py` events/recipients/settings/outbox, `telegram_bot.py` link codes, commands, poller thread;
  migration 0011). No webhook: the server stays unreachable from the internet. Only the process holding
  `<state_dir>/telegram-bot.lock` (flock) polls and sends (`scheduler.telegram_deliver` checks
  `telegram_bot.owns_lock()`); errors are backed off in the thread, never raised. Settings `telegram_enabled` +
  secret `telegram_bot_token`; `telegram_chat_id`/`NOTIFY_TELEGRAM_CHAT_ID` is unused (field kept so env files load).
  Recipients: project owner plus admins with `notification_prefs.all_projects`, humans with a `telegram_links` row
  only. The outbox `notifications` doubles as delivery log: `text` is cleared once sent/failed, rows older than 30
  days are deleted. `local_only` messages carry task number, event and link only. Link codes: `secrets.token_urlsafe(12)`
  (fits Telegram's deep link `?start=`), SHA-256 only, 15 min, one-time via conditional `UPDATE`.
- 2026-10-10 – Runner messages reach the owner through optional request fields old servers ignore: `POST
  /tasks/{nr}/release` with `signal: "blocked"` (`runner.give_back`) and `POST /tasks/{nr}/comments` with
  `signal: "stalled"` (stall note) – event `agent_blocked`; for the stall only the first line goes into the message.
- 2026-10-10 – Acting from Telegram (`app/services/telegram_actions.py`, migration 0012): review buttons approve
  (confirmation step), reject (next message = review comment, `/abbrechen` stops) and return conflict; a reply to a
  question message answers it. Callback data `tb:<token>` (`secrets.token_urlsafe(16)`, SHA-256 in
  `telegram_actions`, bound to human/task/kind, 7 days, confirmations 30 min) instead of signed payloads – revocable
  and one-time without a key. All buttons of a message share `keyboard`; the first press marks them all used with a
  conditional `UPDATE`, later presses repeat `result`. Actions go through `state_machine.transition` as the linked
  human inside `session.begin_nested()`, so a failed merge rolls back only the action and the detached conflict
  notification is still written. Audit: `review_approve`/`review_reject`/`review_reject_conflict`/
  `question_answered`/`notifications_muted`/… with `via="telegram"` and `outcome`. `local_only` replies use fixed
  texts (`LOCAL_ERRORS`) without branch, file or agent names; a reply to a `local_only` question needs a
  `question_reads` row (written by the task page and the inbox) – Telegram never shows the question.
- 2026-10-10 – Bot commands in both languages, all accepted regardless of the server language: `/stumm`|`/pause`|
  `/mute`, `/laut`|`/resume`|`/unmute`, `/hilfe`|`/help`, `/reviews aus|an|off|on`, `/inbox`, `/status`,
  `/abbrechen`|`/cancel`. `setMyCommands` registers the names of the server language when the poller sees a new
  token. Mute ends accept durations and clock times (`notifications.parse_until`: `2h`, `bis 18:00`, `until 6:30`).
- 2026-10-10 – Notification settings over the API (`app/services/notify_settings.py`, `/api/v1/notifications/settings`,
  `tbr notify`, MCP `notify`) so an orchestrator agent (e.g. `openclaw-agent` in Telegram) can mute on a free-text request. Without
  `user` the caller's own settings (agents get `422 notify_user_required`); another human only for role
  orchestrator/admin and only if that human is linked with Telegram (else `404 not_linked`). Durations use
  `notifications.parse_until` on the server (TZ_DISPLAY). Audit `notifications_muted`/`_unmuted` with `target` and
  `via="api"`; a change by someone else queues the outbox event `notifications.SETTINGS_EVENT` (`settings_changed`),
  which `is_muted` never mutes, is not held in quiet hours and is not in `EVENTS` (log labels: `LOG_EVENTS`).
- 2026-10-10 – OpenClaw orchestration from a chat is a second skill `deploy/agents/openclaw/taskmeister-telegram/`
  next to `taskmeister`, installed by `tbr setup openclaw` into the same workspace. `setup_apps.skill_text` replaces
  `<work-dir>` with the work directory (the chat has no assignment that names it). The skill allows only `tbr`
  commands; `tbr projects`/`tbr agents` were added so it needs no MCP or API call for keys and assignees.
- 2026-10-10 – Build identifier next to the version (`cli/build.py`) – the 0.5.0 rollout of several changes without a
  version bump left `tbr self-update` saying "up to date". Identifier = first 10 hex chars of SHA-256 over the sorted
  `path,hash` RECORD lines of package files (no `*.dist-info`/`*.data`, no `../` scripts, no `.pyc`/`.pth`): uv copies
  those lines unchanged on install, so the server computes it from the wheel it delivers (`distribution.build_id`,
  cached by path/mtime/size, never builds) and `tbr` from its own RECORD; editable installs (`direct_url.json`
  `dir_info.editable` or a `.pth` entry) have none. Header `X-Taskmeister-Build`, `/healthz` `"build"`, display
  `<version>+<build>` (`build.label`). `client.needs_update`: older version, or same version with two known, different
  builds – a newer CLI or an unknown build never triggers an update. `find_wheel` picks the newest wheel of the
  version from `TM_DIST_DIR` and `<source>/dist`.
- 2026-10-10 – Kanban columns are defined in `KANBAN_COLUMNS` (`app/web/tasks_ui.py`) as `(key, statuses, drop
  target)`; `_kanban_columns` groups the cards. `questions` and `aborted` share the column `waiting` ("Questions /
  Aborted"): both wait for a human decision. Dropping a card there always targets `aborted` (`data-status` of the
  list is the drop target, not the card status), so a drop never produces an ambiguous "ask". Order in the column:
  aborted, then questions, each by `updated_at` ascending (longest waiting first). Aborted cards use `#FDECEA` with
  border `#C0392B`; secondary text on them uses `--tb-text-2-tint`.
- 2026-10-10 – Model choice per task (`app/services/model_choice.py`, migration 0013): tasks have an optional
  `model_tier` (`simple`/`standard`/`complex`) and `model`; agents have `model_default`, `model_tiers` (JSON object
  tier → model) and `models_allowed` (JSON list). The server resolves the model for the assignee (concrete model >
  tier mapping > default model > none) and sends it as `model_effective` `{model, source, applies}` in the task
  payload, so the runner needs no extra call and the UI and TASK.md show the same value. `applies` is true only for
  `claude_code`/`codex`/`opencode` (`APPLYING_KINDS`); Hermes, OpenClaw and desktop apps only show it. A concrete
  model must be on the assignee's allow list (`422 model_not_allowed`, checked before anything changes); a
  reassignment keeps the tier and drops a model the new agent does not allow (event `model_dropped`). The runner
  adds the switch from `runner.MODEL_FLAGS` by agent program right after the program (`[runner] model_flag`
  overrides, `{model}` placeholder as an alternative) and posts `POST /tasks/{nr}/model-used` (event `model_used`,
  errors ignored for older servers).
- 2026-10-10 – An unavailable model found by the start check (`BLOCKERS["model"]`, also checked when the agent quits
  before the start check) **aborts** the task instead of releasing it and writes no `.agent-blocked`: the failure is
  task-specific, and a release would make the runner claim the same task again on the next pass.
- 2026-10-10 – Profile switch „Alle Projekte anzeigen“ (show all projects) as a column `actors.show_all_projects`
  (migration 0014, default 0 for everyone incl. admins), saved via `POST /profile/display`. Off
  (`access.own_projects_only`): `tasks_ui._owner_ctx` fixes the owner filter to `me` (`owner_locked`, select hidden,
  cookie `tm_owner` neither read nor written), `_filter_options(s, me, keep=…)` limits projects/sprints to own
  projects (`keep` = a project the page was opened for, e.g. `/tasks/new?project=X`), `quick_capture_projects`, the
  projects list and the new-sprint editor filter too; hint `_own_projects_hint.html` when nothing is owned. On: owner
  filter as before, default `all`. Display preference only – direct links, permissions, API/CLI/MCP, agents and
  notifications are unchanged; the bootstrap session without an actor still sees everything.
- 2026-10-10 – SFTP storage (`cli/sftp.py` transfer + path/host-key rules, `app/services/sftp.py` settings, per-agent
  login, task note; migration 0015) with `asyncssh`, not paramiko: only `cryptography` underneath, the host key is
  checked in `SSHClient.validate_host_public_key` against the pinned `SHA256:` fingerprint before the password is
  sent (`known_hosts=([], [], [])` forces the callback; `config=None`, `client_keys=None`, `agent_path=None` keep
  the user's `~/.ssh` out), and `asyncssh.SFTPServer(chroot=…)` gives the tests a real server (`tests/sftp_fake.py`).
  The server never transfers files: agents fetch their login over `/agents/me/sftp-credentials` (secure channel
  like git credentials, audit `sftp_credentials_fetched`) and report uploads via `POST /tasks/{nr}/sftp-uploads`
  (system comment, assignee only). The storage pattern is resolved on the server (`/agents/me/sftp?task_nr=`), so
  `cli` needs no copy of it. Paths are checked with `sftp.inside` before connecting; `..` is refused outright.
  Settings `sftp_*` live in `app_settings` only (no `.env` keys); `SettingDef.max_int` allows port 65535.
- 2026-10-10 – Never two sessions per task: before claiming, `tbr run` stops live windows of own *changes_requested*
  tasks (`stop_returned_windows`), and `_launch_tmux` stops and closes every window with the task's name first
  (`stop_task_windows`: SIGINT, SIGTERM, SIGKILL to the pane's process group, `STOP_SIGNALS`, then `close_window`).
  Signalling the whole group ends the old `tbr session` without `finish` – otherwise it would see its task in progress
  again and release it as "finished without a closing step" when its agent ends. If the session lock is still held
  afterwards, the start fails and the task is given back. The grace period `stop_after_finish_seconds` is therefore
  cut short for a task that came back from review.
- 2026-10-10 – Kanban period filter for the done column (`done` parameter, cookie `tm_done_range`, default `3d`;
  `tasks_ui.DONE_RANGES`: `today`/`3d`/`7d`/`30d`/`all`). Filtering happens in SQL via `tasks.search(done_since=…)`
  on `COALESCE(done_at, updated_at)` (also sort key `done_at`), not after loading. `3d`/`7d`/`30d` count back from
  now (rolling); only `today` starts at local midnight in `TZ_DISPLAY` (`tasks_ui.done_since`). The column shows at
  most `KANBAN_DONE_LIMIT` (100) cards, newest first, with a hint linking to the backlog (`status=done`,
  `sort=done_at`); the counter shows the period count and "of N" (all done tasks for the other filters). The owner
  filter and the profile switch apply unchanged; the period filter applies to the done column only.
- 2026-10-10 – Technical rename taskmeister → taskbrigade before the first public release (0.8.0), with the old names
  still read instead of a hard cut – running installations (native, Docker, agent machines) must not fail between
  the server update and switching each agent. New: `.taskbrigade.toml`, `TBR_*`, `/opt|etc|var/lib|var/log|var/backups
  /taskbrigade`, `taskbrigade.service`/`-backup.*`, system user `taskbrigade`, default DB `taskbrigade`, Compose
  project/service/image/network `taskbrigade` and volumes `taskbrigade-*`, work dir default `~/taskbrigade-work`,
  MCP name, skills, scripts and jobs `taskbrigade(-…)`, timers `tbr-runner-<agent>`/`com.taskbrigade.runner.<agent>`,
  tmux session `taskbrigade`, header `# TaskBrigade assignment`, `X-TaskBrigade-Version/-Build`, class
  `TaskBrigadeMCP`, logger names `taskbrigade.*`, Gitea token names `taskbrigade(-admin|-agent)`. Compatibility
  (each with a note: CLI stderr via `cli.legacy.note_once`, server log `legacy_name` via `app.config.note_legacy`):
  `TM_<X>` for every `TBR_<X>` (Settings: `_LegacyEnvSource` below env and `.env`; others: `app.config.getenv`,
  `cli.legacy.getenv`), `.taskmeister.toml` (`client.agent_config`: the new name wins per directory),
  `~/.config/taskmeister/config.toml`, `/var/lib/taskmeister` while `/var/lib/taskbrigade` does not exist,
  `taskmeister.service`/`/etc/taskmeister/taskmeister.env` in `tbr-admin config` (`TM_<X>` keys count as `TBR_<X>`;
  set/unset also removes the old line), both header names sent by the server and read by `tbr`, both assignment
  headers in triggers/skills, the old Codex trust marker `# tm-runner`, Gitea service user `taskmeister` kept by
  `gitea-init` while the stored token belongs to it. The DB is never renamed: `db.json` names it natively,
  `TBR_DB_NAME`/`TBR_DB_USER=taskmeister` are written into a migrated Docker `.env`.
- 2026-10-10 – Migrations of existing installations run once from the update scripts. Native:
  `deploy/migrate-names.sh` (called by `update.sh` when `taskmeister.service` or `/etc/taskmeister` exists; DB dump +
  tarball of etc/state/units first, `usermod -l`/`groupmod -n` keep UID/GID, directories moved, env keys renamed by
  `tm_env_rename_legacy` in `prereq.sh`, old units removed; `update.sh` then re-execs itself from the moved
  checkout). The old `update.sh` does not know the migration – pull into the old checkout first
  (docs/operations.md). Docker: `deploy/docker/migrate-names.sh` (from `update.sh` when `taskmeister-db` exists and
  `taskbrigade-db` does not; `.env` renamed by awk `tm_env_migrate` – no Python on a Docker host, bash 3.2 for macOS;
  dump of the old stack found by Compose labels; old containers and network removed because the subnet is reused;
  volumes copied with Compose labels, old volumes kept as the way back). Agent machines: `tbr setup <kind>` and
  `tbr doctor --fix` (`cli/migrate.py`; plain `tbr doctor` renames only the config file); Hermes jobs and OpenClaw
  automations are renamed with `edit --name` when `--help` lists the flags, otherwise the old job keeps its old files.
  Both migrations were run end to end (Docker stack of 0.7.1 → 0.8.0 incl. way back; native in an Ubuntu 24.04
  container with stubbed systemctl).
- 2026-10-10 – Screenshot demo data goes through the app's services inside the container (state machine, sprints,
  `onboarding.provision_gitea`), not through the REST API: agents, sprint release, approvals with real merges and
  pull requests need admin rights and several actors' tokens. Task branches are created with Gitea's contents API
  (`new_branch`) instead of `git push`. The seed is not idempotent – each run uses fresh volumes.
- 2026-10-11 – Duplicating tasks (`tasks.duplicate`, migration 0016 `tasks.duplicate_of_id`, API
  `POST /tasks/{nr}/duplicate`, `tbr duplicate`, MCP `duplicate_task`): a new draft in the same project, never
  assigned unless `assign` is given. Rights like sprint planning (`tasks.can_duplicate` = `sprints.can_plan`: admin,
  orchestrator, project owner) – stricter than `create`, which also lets workers and members create tasks. The title
  prefix `(duplicate)` is an `N_` msgid; `duplicate_title` strips the prefix of every UI catalog first, so duplicates
  of duplicates never stack prefixes in either language. A concrete model is copied even without an assignee (the
  task form's reassignment rule drops it later if the agent does not allow it); with `assign` it is dropped at once
  (event `model_dropped`). `duplicate_of_id` is separate from `follow_up_of_id` (follow-up = new work from open
  points, duplicate = the same work again). The spec file link (`detail_task_git_url`) is not copied – it names the
  original's file.
- 2026-10-11 – `cli.prepare._ensure_worktree` brings every reused worktree up to `origin/<base>` via
  `merge_base_branch`, not only when a remote task branch exists: no own commits → `merge --ff-only`, own commits →
  merge (conflicts stay as markers), dirty worktree → untouched and `prepare` adds `stale_base_note` (commits behind,
  `commits_behind`) to TASK.md/`Prepared.note`. A freshly created worktree without remote branch still starts on
  `origin/<base>` without a merge. `_sync_local_base` fast-forwards the clone's local `<base>` after the fetch
  (`update-ref` if not checked out; skipped when diverged or dirty). Reason: a task restarted after a question kept the
  base of its first run and missed tasks merged in between (WEB1-0010). All entry points (`tbr prepare`, `tbr run`,
  MCP `start_task`) go through `prepare()`.
- 2026-10-12 – Reviewer agents (`app/services/reviews.py`, migration 0017): role `reviewer` (agents only), optional
  `tasks.reviewer_id` (human or reviewer agent) and `projects.default_reviewer_id` (applied by `tasks.create` when
  `reviewer` is missing; `none` = nobody). State machine rule `who="reviewer"` for approve/reject/reject_conflict:
  admins and the project owner as before, plus the entered reviewer if `reviews.may_review` (eligible role, active,
  not the assignee, privacy). No self-review is refused on setting either field (`422 self_review`); at submit an
  ineligible reviewer is dropped with a system comment so the review falls back to the humans. Review jobs reuse the
  claim endpoint: `POST /agents/me/claim {"reviews": true}` returns `job: review` (only role reviewer, review job
  before work), a named for_review task of the reviewer is always a review job, its own held job only refreshes the
  lease. The review lease lives in `tasks.review_claimed_at`/`review_lease_expires_at` (the task status stays
  `for_review`), is extended by the normal heartbeat endpoint, swept with the task leases and cleared whenever the
  task leaves `for_review`. Review jobs count towards `max_parallel_tasks`; with `serialize_reviews` one held review
  job per project. No separate review model field: `reviews.model_for` resolves the task's tier (or its concrete
  model if the reviewer allows it) with the reviewer's model settings. Old runners never get review jobs (no flag).
- 2026-10-12 – Review sessions on agent machines: `prepare` handles `job: review` in `prepare_review` – detached
  checkout of `origin/<branch>` in `<KEY>/.worktrees/<NR>-review` (reset with `checkout --force --detach` when
  reused) with `REVIEW.md` (excluded like TASK.md); the runner names window, lock and log `<NR>-review`, starts
  `tbr session --review` with `REVIEW_PROMPT` (`[runner] review_prompt`), the supervisor runs while the job is held
  and `finish_review` gives an undecided job back (`POST /tasks/{nr}/review/release`). Decisions go through
  `tbr review <NR> approve|reject|conflict|release` / MCP `review_task` (`cli.client.review_task`). The OpenClaw
  trigger also matches `# TaskBrigade review job` / `# TaskBrigade-Review-Auftrag`.
- 2026-10-12 – Agent owner and public agents (migration 0018: `actors.owner_id` (FK to a human, `SET NULL`),
  `actors.is_public`, `actors.show_all_agents`). One rule in `access.may_use_agent(by, agent)`: humans as assignees
  are never restricted; admins any agent; otherwise public agents, the agent itself, a human's own agents, an agent's
  owner's agents. The server checks it only when the assignee or reviewer *changes* (`tasks._assign`, `tasks.create`,
  `tasks.update`, `reviews.set_reviewer`, project `default_reviewer`) – a task keeps an assignee its editor could not
  choose, and a project default reviewer is applied to new tasks without checking the creator
  (`set_reviewer(..., project_default=True)`). Error `403 agent_not_visible`. UI choices use
  `access.agent_choice_filter` (SQL; admins see only own + public unless „Alle Agenten anzeigen“ is on) through
  `tasks_ui.agent_choices(session, me, keep)` and `reviews.candidates(session, me)`; `keep` holds the current
  assignee/selected filter. `GET /actors` filters the same way (`?all=true` for admins). Migration: owner = creator of
  the agent's first join code (actors have no `created_by`), else `BOOTSTRAP_ADMIN_ACTOR`, else the first human
  admin; all existing agents public.
- 2026-10-12 – Conscious waiting for background runs (migration 0019: `tasks.waiting_reason`/`waiting_until`/
  `waiting_file`). `POST /tasks/{nr}/heartbeat` takes an optional body (`waiting`, `for_minutes`, `watch_file`,
  `done_waiting`; `tbr heartbeat --waiting/--for/--watch-file/--done-waiting`, MCP `report_waiting`); a plain heartbeat
  (the runner's own) leaves the state alone. Default 120 min, silently capped at 24 h (`tasks.WAIT_MAX_MINUTES`), a new
  call extends. Cleared by every status change (`state_machine.transition`, override, reassignment). The payload field
  `waiting` (`{reason, until, watch_file}`) is only set while the state is valid and the task is in progress, so the
  UI and the runner need no clock logic. `tbr` makes `--watch-file` absolute (the runner on the same machine reads it).
  `Supervisor` treats waiting as activity (stall detection resumes `stall_minutes` after expiry); with a watch file it
  tracks (mtime, size) and sends the usual `signal: "stalled"` comment ("Background run is no longer writing: …", last
  lines of the file) once per silence. `Heartbeat`/MCP heartbeat threads continue past `max_session_hours` while the
  response says `waiting`. `local_only` rules are unchanged (notification carries no text).
- 2026-10-13 – Several runner agents may share one tmux session, so tmux housekeeping only judges the own
  agent's windows: `_launch_tmux` tags each window with the window option `@taskbrigade_work_root` (resolved work
  root), `list_windows` reads it plus `pane_current_path`, and `own_window` decides by the tag, for untagged windows
  (older tbr) by the pane directory lying in the work root. `cleanup_windows` (runner pass and `tbr cleanup`),
  `stale_windows` (`--stale`) and `tbr doctor` skip foreign windows; doctor only counts them ("plus N of other
  agents"). Before, a foreign agent's live window counted as stale and `tbr cleanup --stale` would have killed it.
- 2026-10-13 – Parked waiting tasks: a CLI agent cannot stay in its session for hours, so after `tbr heartbeat
  --waiting` it ends the run without `release` (TASK.md step 5). An interactive agent that stays at its prompt is
  stopped by `Supervisor` once the task is waiting and the window has not changed for `park_idle_seconds` (default
  120; `finish` is skipped). `finish` also leaves a task with a valid `waiting` in progress. Each `tbr run` pass
  extends the lease of such tasks without a session by a plain heartbeat (`runner.keep_parked`) and does not resume
  them (`resumable_task(parked=…)`) until the waiting state ends or the watched log is missing or unchanged for
  `stall_minutes` (`watch_file_silent`, mtime only). Before, agents used step 4 (`release "Continuation needed"`),
  which cleared the waiting state and put the task back to defined every pass.
- 2026-10-13 – The agent setup page (`admin/agent_setup.html`) shows a fresh join code twice: alone in `.join-token`
  (`#join-token`, label "Temporary sign-in token:", copy button) and inside the `tbr join` command. The orange comes
  from `--tb-token-bg`/`--tb-token-fg` (not the brand `--tb-orange`, which is for the logo only; text contrast is
  checked in `tests/test_join_token_box.py`). Without a fresh code (`code` empty) there is no box.
- 2026-10-14 – MCP attachments (`cli/attachments.py`, tools `attach_files`/`attach_text`, `create_task(attachments=…)`)
  read host files only below allowed roots – a chat model chooses the paths, so a prompt injection could otherwise
  exfiltrate `~/.ssh/…`. Default roots: work directory, `~/Downloads`, `~/Documents`, `~/Desktop`; `[mcp]
  attach_roots` in `.taskbrigade.toml` replaces (not extends) the list. Paths are resolved (symlinks) before the root
  check; hidden components (except `.worktrees`) and secret-like names (`SECRET_PATTERNS`) are checked on both the
  resolved path and the name as given (a link `id_rsa` → harmless file is refused too). Accepted files of a call are
  uploaded even when others are refused. The client only knows the server's default size limit (10 MB);
  `[mcp] max_attachment_mb` follows a raised `TBR_MAX_ATTACHMENT_MB`. `tbr attach` shares the upload
  (`attachments.upload_paths`) but has no root restriction – the human names the files.
- 2026-10-14 – Mobile UI ("compact header" mockup) by available width, never user agent or orientation: below
  960 px `base.html` shows logo, "+ Task" (opens the same quick-capture form as a sheet) and a menu button; below
  600 px phone layouts. `static/shell.js` drives `[data-toggle][aria-controls]` → `.collapsible.is-open` (menu,
  quick capture, filter panels, task edit form), `[data-tabs]` (task detail "Description / Thread") and
  `details[data-fold-narrow]` (folded on narrow screens at load). Folding only applies with `html.js` (inline script
  in `<head>`); without JavaScript everything stays visible. Layout changes are CSS only, so rotating keeps typed
  input, the chosen kanban column and filters.
- 2026-10-14 – Kanban widths: >= 1024 px all columns share the width (unchanged); 600–1023 px (phone landscape) only
  `.kanban` scrolls sideways with 240 px columns (three full columns at 844 px); < 600 px one column chosen with the
  "Status column" picker (`col` query parameter, default `KANBAN_DEFAULT_COLUMN`, kept in the URL and in the
  filter form's hidden `col`). The picker is a view choice, never a status change.
- 2026-10-14 – Kanban "Move" menu per card (touch/keyboard alternative to drag and drop): `GET /kanban/<nr>/moves`
  lists only column drop targets that `tasks_ui.allowed_moves` accepts (generic state machine rule + `check_allowed`,
  never an override); the move itself is the existing `POST /kanban/move`. Drag and drop keeps its admin override
  and starts on touch screens only after a long press (`delay`, `delayOnTouchOnly`) so swiping scrolls the board.
- 2026-10-14 – Backlog below 900 px: the same table rows become cards (`table.cards`, cells `c2`/`c3`/`card-hide`);
  the column-head sorting is replaced by sort fields in `_backlog_table.html` that belong to the filter form via
  `form="backlog-filter"` (htmx collects `form.elements`), so they follow every table update. The inbox hides empty
  action categories ("All done" line) and folds non-empty notices with a count.

## Pitfalls

- Security boundary: `Tailscale-User-Login` and `/agents/me/git-credentials` trust the direct loopback peer;
  a local process can forge the header. `--no-proxy-headers`, binding to `127.0.0.1` and a trusted app host are
  required; details and remaining findings are in `SECURITY.md`.
- `tm git-credential` only returns credentials for a matching Gitea scheme and exact host incl. port; without a
  configured Gitea URL the output is empty. `/healthz` does not expose internal error texts.
- No internal hosts/IPs, personal names or internal project keys in code, tests, docs, deploy files or example
  configs. Example values: Gitea `gitea.example.internal:3001`, DB `db.example.internal`, tailnet
  `taskbrigade.example.ts.net`, LAN `192.168.1.0/24` (client IP in tests `192.168.1.50`), human `admin`/`alex`,
  project `DEMO`/`KET`, agents `openclaw-agent`/`research-agent`.
  The concrete internal markers are not part of the repository: keep them in a local, untracked patterns file
  outside the repo (one regular expression per line). Check before submitting (no output expected):
  `git grep -n -I -E -f /path/to/private-patterns.txt -- ':!uv.lock' ':!app/static/vendor'`; the same file goes
  to `export-public.sh --patterns-file` and `TBR_PRIVATE_PATTERNS_FILE` (`tests/test_deploy_prereq.py`).
- Login name `admin` is first checked against `BOOTSTRAP_ADMIN_PASSWORD_HASH`; if that fails, an actor named
  `admin` with its own password is tried (`app/web/auth_routes.py`).
- Install path: `TBR_BASE` (default `/opt/taskbrigade`) in `deploy/install.sh`/`update.sh`; install.sh and update.sh
  substitute it into the systemd units (update.sh installs the units on every run).
- `tests/test_owner.py` mixes DB-free permission tests with the migration tests
  (`test_migration_sets_configured_owner`, `test_migration_falls_back_to_creator`). Only the migration tests
  carry `requires_db`; the module `pytestmark` stays `db`, otherwise the permission tests would be skipped without
  a test DB.
- The server claim (`app/services/tasks.py:claim`) does **not** filter by `due_date`; it picks tasks that are
  assigned, `defined`/`changes_requested`, in an active project and without open dependencies (plus
  `max_parallel_tasks`). Why an assigned task is not picked up is logged in the runner's `logs/runner.log`.
- Orchestrator CLI helpers (`parse_due`, `patch_task`, `transition`, `explain`) live in `cli/client.py` and are
  shared by `cli/tm.py` and `cli/mcp_server.py`.
- After changing `deploy/agents/cowork/taskbrigade/SKILL.md`, rebuild `taskbrigade-skill.zip` (path inside the
  zip: `taskbrigade/SKILL.md`). Without a `zip` binary use Python `zipfile` (ZIP_DEFLATED).
- This repo's own `brain.md` is still edited directly in tasks that change the brain-changes mechanism itself:
  the running server applies change files only once it has been updated.
- `gitea_fake` keeps branch-only files in `FakeRepo.branch_files` (`put_branch_file`); a merge copies them to `main`.

- Installationspfad: `TBR_BASE` (Default `/opt/taskbrigade`) in `deploy/install.sh`/`update.sh`; install.sh und update.sh
  setzen ihn beim Kopieren in die systemd-Units ein. Voraussetzungen stehen in `deploy/prereq.sh` (von install, update
  und backup gesourct): Python genau 3.12, uv, git, systemd, MariaDB-Client ≥ 10.5 (`mariadb-dump`).
  Fehlende apt-Pakete (`python3.12`, `git`, `curl`, `mariadb-client`) installiert install.sh selbst; uv lädt es
  nach `/usr/local/bin`, außer `TBR_INSTALL_UV=0`. Gitea: Platzhalter (`example.internal`/`example.com`/`example.org`)
  nur Warnung, eine gesetzte `GITEA_URL` muss `GET /api/v1/version` mit HTTP 200 beantworten, sonst Abbruch.
  `update.sh` meldet Gitea nur (kein Abbruch). Tailscale nur Pflicht bei `TAILSCALE_SERVE=1`. Meldungen Englisch.
  Ablauf für Fremde: `docs/quickstart.md`. `.env.example` enthält jede Settings-Variable; Bools und Integer nur
  als Kommentar, damit ein leeres `KEY=` pydantic beim Start nicht umwirft. Lokaler Ubuntu-Default im Example:
  `127.0.0.1:3306` (Code-Default ohne Env bleibt 3307, Tests hängen daran). `BOOTSTRAP_ADMIN_ACTOR` ist per Default
  `admin`, weil Migration 0001 genau diesen Admin-Menschen seedet. Bestehende Installationen tragen den bisherigen
  Akteur in der Env-Datei ein.
- Der Server-Claim (`app/services/tasks.py:claim`) filtert **nicht** nach `due_date`; abgeholt wird, was zugewiesen,
  `defined`/`changes_requested`, im aktiven Projekt und ohne offene Abhängigkeiten ist (plus `max_parallel_tasks`).
  Warum ein zugewiesener Task liegen bleibt, zeigt `logs/runner.log` des Runners (seit 0.3.0).
- Orchestrator-CLI-Hilfen (`parse_due`, `patch_task`, `transition`, `explain`) liegen in `cli/client.py` und werden
  von `cli/tm.py` und `cli/mcp_server.py` gemeinsam genutzt.
- Änderungen an `deploy/agents/cowork/taskbrigade/SKILL.md` → `taskbrigade-skill.zip` neu packen (Pfad im Zip:
  `taskbrigade/SKILL.md`). Ohne `zip`-Binary: per Python `zipfile` (ZIP_DEFLATED).
- Docs layout: `README.md` (overview, Mermaid architecture diagram) links `docs/quickstart.md`, `cli-agents.md`
  (runner agents on a second machine), `desktop-apps-mcp.md`, `agent-frameworks.md` (Hermes/OpenClaw),
  `agent-skill.md`, `architecture.md`, `operations.md`. The Cowork skill links `docs/desktop-apps-mcp.md` – renaming a
  doc means updating that reference and rebuilding `taskbrigade-skill.zip`. Screenshot references stay commented out
  (`<!-- ![…](screenshots/x.png) -->`) until the image exists; the list is in `docs/screenshots/README.md`.
- Tests run with `UI_LANGUAGE=de` (`tests/conftest.py`), so existing UI assertions check German texts;
  `tests/test_i18n.py` covers English and fails on missing/stale entries in `app/locales/de.json` – add every new
  `_()`/`ngettext()`/`N_()` string there. In templates a literal `%` inside `_()` must be written `%%`.
- Never bind `_` as a throwaway variable in modules that import `gettext as _` (e.g. `a, _, b = x.partition("/")`)
  – it turns `_` into a local and breaks `_()` calls in that function.
- The default language is cached for 30 s (`i18n.default_language`); `settings_store.update` clears the cache when
  `ui_language` changes, tests clear it in the autouse fixture.
- CLI language is chosen on the agent machine, not by the web UI: `TM_LANGUAGE` → `language = "de"` in the agent's
  `.taskmeister.toml` (set by `load_config`, otherwise looked up via `find_config`) → `en`. Every new `_()`/`N_()`
  string in `cli/` needs an entry in `cli/locales/de.json` (`tests/test_cli_i18n.py` fails on missing/stale entries
  and differing placeholders). Typer help texts are `help=_(…)` (translated at import), MCP tool texts
  `description=_(…)` in `build_server`; prompts/instructions are `N_()` constants translated where used.
- Tests force `TM_LANGUAGE=en` (`tests/conftest.py`), so CLI/MCP/runner assertions check English while web UI
  assertions check German (`UI_LANGUAGE=de`); API error texts inside CLI output are still German.
- `tm prepare` output starts with `# Taskmeister assignment <NR>` (German: `# Taskmeister-Auftrag <NR>`);
  `deploy/agents/openclaw/taskmeister-trigger.js` matches both – an older installed trigger only knows the German one.
- The `_`-as-throwaway pitfall also applies to `cli/` (`for _ in …`, `a, _, b = x.partition(…)` would break
  `auto_trust_claude` and `attach`); use `_attempt`, `_sep` etc.
- `_` is the translation function (`app.i18n`). Never bind `_` as a variable in a function that calls `_()` (e.g.
  `scheme, _, token = header.partition(" ")`) – every `_()` call there becomes a `TypeError`.
  `tests/test_no_shadowed_gettext.py` enforces this for `app/` and `cli/`.
- Public export uses the committed `HEAD` only and rejects tracked changes; untracked files are omitted. The
  export requires `gitleaks` and accepts an external patterns file for organization-specific markers. Mirror
  only the sanitized release repository, never the internal working repository or its history.
- Public export scans text found in all archived files, including binary files with NUL bytes; readable internal
  addresses in an asset must fail the export even when the secret scanner does not classify them.
- Compose `pull_policy` defaults to `build` (`TM_PULL_POLICY`): otherwise `image: taskmeister:local` is first looked
  up on Docker Hub on a fresh machine. An Argon2 hash in the compose `.env` must be single-quoted (`$` interpolation).
- Docker Desktop and the userland proxy rewrite client addresses to the gateway: with `TM_PUBLISH` beyond
  `127.0.0.1` every client would be a trusted peer there.
- Docs layout: `docs/docker.md` (Docker Compose) is linked from README "Getting started".
- Gitea reserves the user name `admin`; the profile's Gitea admin is `giteaadmin`. A Gitea admin may create tokens
  for other users (`POST /users/{u}/tokens` with basic auth); scope `all` is not used, explicit `write:*` scopes are.
- Compose profiles: `docker compose down` without `--profile gitea` leaves the Gitea container running.
  `${VAR:?…}` must not be used for profile-only variables – Compose interpolates the whole file even without the
  profile.
- Port 3000 is often taken on dev machines; for a local end-to-end test set `TM_GITEA_PUBLISH=127.0.0.1:3300` and
  `GITEA_PUBLIC_URL=http://localhost:3300` together (they must match, Gitea's `ROOT_URL` follows the latter).
- Compose `env_file: .env` is read from the project directory; `docker compose --env-file X` only feeds
  `${…}` interpolation, not the container environment. For smoke tests pass container variables through an extra
  `-f` override with `environment:` (and use `-p <name>`; network and volume names in `compose.yaml` are fixed, so a
  smoke stack collides with a real one on the same host).
- With `compose.tailscale.yaml`, the published port and the stack network belong to the `tailscale` service; the app
  service has neither (`tests/test_compose_files.py` checks this and the serve config).
- Docker scripts `deploy/docker/{backup,restore,update}.sh` (shared `common.sh`) must run on macOS bash 3.2 and BSD
  tools: no GNU-only sed (`T`), no `mapfile`, empty arrays as `${a[@]+"${a[@]}"}`. Volume tar/untar uses
  `docker run --rm -v <volume>` with the service image, not `docker compose run` (that rebuilds with
  `pull_policy: build` and mixes progress output into stdout). `docker compose config --images <svc>` also lists
  dependencies – read a service's image/pull_policy with `tm_service_field`. Restore runs `docker compose create`
  first, otherwise `docker run -v` creates the volumes without Compose labels (warning on every `up`).
- Docker scripts and the profile `gitea`: `dc` in `common.sh` adds `--profile gitea` once the volume
  `taskmeister-gitea` exists, because users start the profile with `--profile gitea` (not `COMPOSE_PROFILES`) and
  plain `docker compose stop/pull/up` would skip `gitea`, `gitea-admin` and `gitea-init`. `up -d --wait` with the
  profile works: the exited one-shot containers do not fail `--wait`. Agent Gitea users/tokens:
  `docker compose exec -u git gitea gitea admin user create|generate-access-token --raw`.
- Docker backup/restore was tested end to end (also with the profile `gitea`): backup → `down -v` → restore brings
  back tasks, `master.key` (Gitea token decrypts), logins, API tokens and the Gitea repos; no prior `up` needed.
- Plan related tasks as a dependency chain when they touch the same files (compose, `.env.example`, docs) –
  depending only on a common predecessor lets one agent start them in parallel branches.
- Run `act` from a regular clone, not from a git worktree: act copies the directory and the worktree's `.git` file
  points to a missing path, so Git tests fail with `fatal: not a git repository: (null)` (git runs in the test
  process's cwd). act also leaves `job.services.<id>.ports` empty – the workflow uses the fixed port 33306.
- The demo agent created by `gitea-init` is a `worker`; workers may create tasks assigned to themselves, which is
  how the smoke test creates its task without an admin token.
- tmux launch in `cli/runner.py`: the window is opened with a placeholder (`cat`), then `remain-on-exit` and
  `pipe-pane` are set and the real command starts via `respawn-pane -k`. With the command on `new-window`, a
  `tm session` that dies at once closes the window before `remain-on-exit` applies, so the failure is invisible.
  `capture-pane` of a dead pane may miss the early output; read the run log instead. Address the window only by the
  id from `new-window -P -F "#{window_id}"`: `<session>:<name>` is ambiguous as soon as two windows share the task
  name and tmux answers "can't find window" (claim/give-back loop). Any failure after `new-window` closes the window
  (content to the new run log), otherwise the leftover window counts as a running session.
- Runner tests in tmux mode use a fake `tm` (`fake_tm` in `tests/test_runner.py`): the real `tm session` in the
  window would call the API at `http://x` and die, which the start check now reports as a failed start.
- The tm warning goes to stderr only: stdout of `tm git-credential` is read by git and stdout of `tm mcp` is the
  MCP protocol. The script URL in `/install.sh` is `app_url` (Admin settings) or the request's base URL – behind
  `tailscale serve` without `app_url` that is `http://`, so set `app_url` or run with `TM_URL=<url> sh`.
- `pkill -f <pattern>` inside a Bash tool call can match the calling shell itself (its command line contains the
  pattern) and kill it (exit 144); stop test servers by PID instead.
- `actors.create(..., issue_token=False)` returns `Created.token = None` (wizard agents get their token via
  `tm join`); callers that show the token must not use that variant.
- Gitea `GET /users/{name}` needs the `read:user` scope; `POST /admin/users` needs `write:admin` and a site admin.
  Checked against `gitea/gitea:1.24`: an agent user created this way with a `write:repository`/`read:user` token gets
  push access as a collaborator.
- respx: registering a route with the same pattern again replaces the mock of the earlier route (used in
  `tests/test_onboarding.py` to give `GET /user` per-token answers on top of `FakeGitea`). A respx side effect must
  return a plain `httpx.Response`; wrap a `TestClient` response (`httpx.Response(r.status_code, headers=…,
  content=r.content)`) when forwarding a CLI call to the in-process app.
- `deploy/release/export-public.sh` drops `EXCLUDED_PATHS` (`research/`, `docs/naming/`, `media/`,
  `mediaassets/ChatGPT-Bild*.png`, `tasks/`) from the archived `HEAD` before scanning; internal notes may stay in the
  working repository. `tasks/` holds the attachments of TaskBrigade's own tasks (mockups, review screenshots), which
  TaskBrigade commits into this repository like into any project repository. `tests/test_export_public.py` checks
  that every such tracked file is covered.
- Tests for `tm setup`/`tm doctor` must patch `cli.setup._run` (systemctl, loginctl, launchctl) and `shutil.which`
  and point `HOME`/`XDG_CONFIG_HOME` to `tmp_path` – otherwise they install real timers or edit `~/.codex/config.toml`
  on the developer machine (`tests/test_setup_doctor.py`, fixture `home`).
- DB tests: the agents `hermes`, `openclaw-agent`, `codex`, `claude-code` and `opencode` come from `tests/conftest.py`
  (`TEST_AGENTS`, inserted by `ready_db` after the schema is created), not from the migration. Tests that build the
  schema themselves (e.g. `_migrate_old_projects` in `tests/test_owner.py`) insert the agents they need.
- `cli/setup_apps.py` calls subprocesses through `cli.setup._run` (module attribute, not an imported name) so tests
  that patch `cli.setup._run` also cover it. Tests: `tests/test_setup_apps.py` (reuses the `home` fixture of
  `tests/test_setup_doctor.py`, which clears `CODEX_HOME`; an autouse fixture clears `HERMES_HOME` and
  `OPENCLAW_STATE_DIR` – otherwise the tests would write into a real Hermes/OpenClaw installation).
- The Hermes/OpenClaw CLI syntax used by `tbr setup` (`hermes cron create "every 10m" <prompt> --script --skill
  --name`, `openclaw automations add "*/10 * * * *" <prompt> --name --agent --session isolated --no-deliver
  --timeout-seconds --trigger-script`, `automations edit <job-id> --trigger-script|--agent`) was taken from their
  docs, not tested against a real installation; `--trigger-script` is not in the public OpenClaw CLI reference (it
  was used by hand on a real gateway). The JSON of `openclaw automations list --json` / `agents list --json` is not
  documented: `cli/setup_apps.py` reads `[…]`, `{jobs|automations|items: …}`, `agentId`/`effectiveAgentId` and the
  embedded trigger from `trigger.script` or `triggerScript` – adjust the readers there if a version differs.
- Never default an OpenClaw path or automation to `main` when an automation for the TaskBrigade agent already
  exists: several agents share one gateway, and `main`'s workspace belongs to another agent.
- `tm-admin config set` on a native installation needs `sudo` (the env file is `root:taskmeister` 0640 in a 0750
  directory); `get`/`list` also work as user `taskmeister`. A value starting with `-` must follow `--`
  (`tm-admin config set KEY -- -1`), otherwise Typer reads it as an option.
- Pico 2 defines its light theme on `:root:not([data-theme=dark]), [data-theme=light]`; a plain `:root` override of a
  `--pico-*` colour loses against it. Override colours with the same selector (after Pico, as in `app.css`).
- Secondary text `#617180` reaches only 4.44:1 on the selection colour `#EAF2F8` and 4.35:1 on the kanban column grey;
  use `--tb-text-2-tint` (`#56636F`) for small text on tinted areas. Fire orange `#D95D39` on white is 3.77:1 – logo
  and large accents only, never small text or controls.
- `<html>` carries `data-theme="light"` after `lang`: tests match `'<html lang="de" '`, not `'<html lang="de">'`.
- UI screenshots on a machine whose Playwright Chromium misses system libraries (e.g. `libasound.so.2`): run the
  script in `mcr.microsoft.com/playwright/python` with `--network host` against a local `uvicorn app.main:app`.
- Screen patterns for the start check (`BLOCKERS`, `BUSY_MARKERS`, `ANSWER_MARKERS` in `cli/runner.py`) only look at
  the bottom 20 lines, skip everything while "esc to interrupt" is visible and ignore lines up to the agent's last
  answer (`⏺`, `•`) – a working agent quoting "rate limit" from the code must not be released as rate-limited. The
  unsent-prompt check compares the first 30 characters of the longest command argument (the prompt).
- `tm doctor` tests: the `healthy` fixture patches `cli.runner._tmux`, otherwise the developer machine's real
  `taskmeister` tmux session shows up as stale windows. `doctor.agent_login_checks` calls `cli.setup._run` as a
  module attribute so the fixture's `_run` patch covers `claude auth status` too.
- `tests/conftest.py:ready_db` empties tables by an explicit list – a new table (e.g. `sprints`) must be added
  there, and `test_db_integration.py::test_schema_options_and_seed` counts the tables.
- FastAPI turns an empty form value into the parameter's default, so `Form(None)`/`Form("")` cannot tell "field
  sent empty" from "field missing". The task edit form therefore sends `sprint=none` to detach a task; an empty or
  missing `sprint` keeps the current sprint (`tasks_ui.edit_task`).
- Sprint UI (`app/web/sprints_ui.py`, template `sprint.html`) only calls `app/services/sprints.py`; the release
  preview is `sprints.release_preview` (same rule as `release`, without changes). Batch lines are parsed in
  `sprints_ui.parse_batch`; the batch is created only when every line is valid. The sprint filter in
  backlog/kanban/inbox is the query parameter `sprint` (empty = all, `none`, or one id; other values are ignored).
- Sprint helpers for CLI and MCP live in `cli/client.py` (`resolve_sprint`, `parse_batch`, `sprint_line`,
  `release_report`, `close_report`). A sprint name needs the project (`GET /projects/{key}/sprints`, compared
  without case); a numeric value is used as id without a lookup. The CLI batch parser mirrors
  `app/web/sprints_ui.parse_batch` (cli must not import `app`), additionally skips `#` comment lines and leaves the
  agent check to the server (`Entry n: …`). Changing the UI line format means changing both.
- `tests/test_sprints_cli.py` runs `tm` and `TaskmeisterMCP` with a real `cli.client.Client` against the
  in-process app (fixtures `tm_as` from `tests/test_ui_cli.py`, `senv` from `tests/test_sprints.py`); MCP tools get
  the same `httpx.Client` patch as `tm_as`.
- Text replacements `tm` → `tbr`: `"tm"` as a whole Python string is code (lookups, comparisons), `tm-runner`,
  `.tm-submit.md`, `[tm]`, `MARIADB_USER=tm` and `TM_*` are not the command; German compounds (`tm-Paket`) need a
  look of their own. Installed OpenClaw triggers and Hermes skills match the header `# Taskmeister assignment` – it
  must not change.
- `cli/locales/de.json` is not sorted; keep the key order when rewriting it (`json.dumps(..., indent=1)` plus
  trailing newline), otherwise the diff covers the whole file.
- DB test agents are inserted and reset with `max_parallel_tasks=1` (`tests/conftest.py`), not the column default
  2 – otherwise claim tests depend on whether the schema was created in the same test.
- Pico's `:where(input,select,textarea,…) + small` rule sets `display:block; width:100%` and also matches a `<small>`
  after a checkbox inside a label (text nodes do not break `+`). A margin-left indent then overflows the form; help
  text under a checkbox label uses `small.field-help` (`width: auto`, `margin-left: 2rem`).
- UI screenshots: the host Playwright Chromium misses `libasound.so.2`; the Docker image
  `mcr.microsoft.com/playwright/python:v1.63.0-noble` with `--network host` works, but needs
  `pip install --user playwright==1.63.0` inside (the Python package is not preinstalled) and must not run with `-I`.
- Sprint editor: the list travels as JSON in the hidden field `entries` of `#sprint-form`; the detail fields sit
  outside the form on purpose, so `form_guard.js` (snapshot of the form's FormData, `data-guard="sprint"`) reports
  unsaved changes only when the model changes, not when another entry is selected. The script writes the field only
  on edits – the server-rendered JSON is the guard's initial state.
- `audit(..., **fields)` passes `fields` as `extra` to `logging`: names of LogRecord attributes (`created`, `name`,
  `msg`, `args`, `module`, …) raise `KeyError: Attempt to overwrite …` – use e.g. `drafts_created`.
- A notification for a request that fails afterwards (merge/brain/changelog conflict → 409, rollback) is stored with
  `notifications.task_event(..., detached=True)`: rows are written in an own session from the session's
  `after_transaction_end` hook. Writing them in a second session while the request transaction is still open hangs
  in `Lock wait timeout` – the request holds row locks on `actors`/`tasks` that the insert's foreign keys need.
- Telegram tests replace `telegram.api_factory` with a fake (`tests/test_telegram.py::FakeApi`); `TelegramApi`
  error messages never contain the request URL, because the URL contains the bot token.
- Quiet hours and mute times in the profile use `TZ_DISPLAY`; the DB stores naive UTC. A permanent mute is
  `notifications.FOREVER` (9999-12-31).
- `tests/conftest.py:ready_db` also empties `telegram_actions`, `question_reads`, `notifications`,
  `notification_prefs`, `telegram_link_codes` and `telegram_links`; `test_schema_options_and_seed` counts 19 tables.
- Telegram button/reply tests (`tests/test_telegram_actions.py`) drive `BotThread().handle(api, update)` with the
  `FakeApi` of `tests/test_telegram.py`, which records `markups`, `answers` (answerCallbackQuery) and `edits`
  (editMessageReplyMarkup) and returns `message_id` 1000 + n. A fake merge conflict needs `gitea.conflict = {...}`;
  `merge_fails` alone gives `merge_failed`, not `merge_conflict`.
- A reply only answers the question it was sent for: if the task's latest question is more than 5 s newer than the
  notification (`STALE_QUESTION_S`), the bot refuses instead of answering a different question.
- `tests/test_notify.py` checks that both OpenClaw skills contain the section `## Orchestrator via Telegram` with
  fixed phrases ("Ask back", "Confirm before", "task number and link", "only `tbr` commands", "never approve", …) and
  that `docs/agent-frameworks.md` names role `orchestrator`, `tbr setup openclaw`, tokens and `tbr notify` – keep them
  when rewording.
- `uv tool install --force <wheel-url>` with an unchanged version reuses uv's cached copy of the earlier build (checked
  with uv 0.11.21 over HTTP). `tbr self-update` and `install.sh` therefore pass `--reinstall-package taskbrigade`; the
  download also sends `Cache-Control: no-cache`.
- Raise the version before every rollout (`pyproject.toml`, `uv lock`, CHANGELOG section) – the build check is a
  safety net (docs/release-checklist.md, docs/operations.md).
- Task forms send `model_tier`/`model` as `none` to clear them; an empty or missing value keeps the stored value
  (same FastAPI empty-form rule as `sprint`). The edit handler only passes changed values, so an unchanged model is
  not re-checked against a new assignee but dropped by the reassignment rule. Sprint-editor entries without the
  `model_tier`/`model` keys keep the task's values (`sprints.OPTIONAL_PLAN_FIELDS`).
- New templates need a context in `tests/test_i18n.py::_contexts`; templates that include `_model_fields.html`
  need `agent_models` (agent → `{allowed, applies}`, from `tasks_ui._filter_options`) and `mf`.
- UI tests reading a project `<select>`: the quick capture in the header comes first on every page; take the last
  `<select name="project">` (`tests/test_owner.py::_options`). Test humans start with "Show all projects" off
  (`ready_db` resets `show_all_projects=0`) – a UI test that needs other owners' projects turns it on
  (`_show_all`) instead of passing `owner=all`, which is ignored while the switch is off.
- A plain `<small>` after a checkbox inside a label (e.g. `<input> KEY <small>name</small>`) is pulled up over the
  previous row by the same Pico `input + small` rule (negative `margin-top`). Wrap the label text in a `<span>`
  (`admin/agent_new.html`, `tests/test_agent_new_ui.py`) or use `small.field-help`.
- `cli/sftp.py` runs asyncssh through `asyncio.run`, or in a worker thread when an event loop is already running (the
  MCP server calls sync tools inside its loop). Its messages use the CLI catalog (`cli/i18n.py`); server-side
  validation messages for `sftp_*` settings come from `settings_store.SFTP_CHECKS` so the UI stays in its language.
- `cli/sftp.py` sets the `asyncssh` logger to WARNING – at INFO it logs every channel open/close.
- `prepare` asks `GET /agents/me/sftp` for the TASK.md note; fake clients in tests must answer that path or raise
  `ApiError` (respx: an unmocked route raises `AllMockedAssertionError`, which is not caught).
- Kanban UI tests that depend on "now" patch `app.services.tasks.utcnow` (the route calls `svc.utcnow()`) and set
  `get_settings().tz_display` explicitly (`tests/test_kanban_done.py`); `render_board` in `tests/test_kanban.py` needs
  the `done_*` context keys.
- A skill folder left in a framework's skills directory is still loaded (by its frontmatter name), so old skill
  folders are moved out to `<work dir>/.backup-taskmeister/`, never renamed in place to `*.bak`.
- `docker volume create` without the labels `com.docker.compose.project`/`com.docker.compose.volume` (= the key in
  compose.yaml, e.g. `gitea-data`) makes Compose warn "already exists but was not created by Docker Compose" and
  `down -v` leave it behind.
- The export's built-in scan allows only documented example ranges (loopback, `192.168.1.0/24`, `10.0.0.0/24`,
  `172.17.0.0/16`, the pinned Compose/smoke subnets `172.31.250.0/24`/`172.31.251.0/24`, netmasks) and hosts ending in
  `.example.internal`/`.example.ts.net`. Test values that must trip the scan (e.g. in `tests/test_export_public.py`)
  are built at runtime, otherwise the repository fails its own export.
- `export-public.sh` calls `gitleaks dir --redact --no-banner -- <stage>` (gitleaks ≥ 8.19 takes the path as an
  argument; the earlier `--source` aborted every real export, the fake gitleaks in the tests did not notice). Probe
  run 2026-10-10 with gitleaks 8.28.0 and a local patterns file outside the repo, on a scratch clone with this
  task's brain-changes applied: no leaks, no pattern hits, 287 files, one commit. Before that, gitleaks flagged
  test fixtures (now `# gitleaks:allow`) and the README phrase "REST API, SQLAlchemy/Alembic" (generic-api-key).
- Gitea answers a merge right after creating a pull request with `405 Please try again later` while its
  mergeability check runs; the state machine then reports a "merge conflict". Scripts that submit and approve at
  once must poll `GET /repos/{o}/{r}/pulls/{n}` until `mergeable` is true (`screenshot_seed.wait_mergeable`).
- Gitea side effects are not rolled back with the DB transaction: a seed run that fails after
  `provision_gitea` leaves the Gitea users behind, and the next run fails with "Gitea user … already exists" – start
  over with fresh volumes.
- httpx logs every request on INFO including the full URL – for Telegram that URL contains the bot token
  (`api.telegram.org/bot<id>:<secret>/…`). `setup_logging` sets `httpx`/`httpcore` to WARNING, and `JsonFormatter`
  passes every finished line through `logging_setup.redact` (bot tokens → `bot<redacted>`), which also covers
  exception texts and tracebacks. Never log request URLs of APIs that carry secrets in the path.
- The task edit form sends parent task and dependencies only for drafts, with the hidden marker `structure=1`; without
  the marker both stay unchanged (FastAPI cannot tell an empty field from a missing one). `tasks.update` accepts
  `parent_task_nr`/`depends_on` (replaces the list) and refuses self-dependencies and parent cycles.
- Fake API clients in runner/prepare tests must answer `GET /agents/me/reviews` (or raise `ApiError`):
  `resumable_task`, `own_running_task`, `stale_windows`, `tbr list` and MCP `list_tasks` ask for review jobs.
- Inbox split: `tasks.inbox` lists `for_review` tasks with an effective reviewer agent under `agent_review` (SQL
  `_agent_reviewed` mirrors `reviews.effective` – keep both in sync); a human reviewer sees its for_review tasks with
  the owner filter `me` even in other owners' projects. Telegram sends no `review` event when a reviewer agent takes
  the review, but `agent_review` after its decision; a human reviewer gets the `review` event too.
- Batch line format is `Title | Agent | Prio | Reviewer` in both parsers (`app/web/sprints_ui.parse_batch`,
  `cli.client.parse_batch`); an empty reviewer means the project default, `none` nobody.
- DB test agents (`tests/conftest.py`) are inserted and reset as public without owner (`is_public = (actor_type =
  'agent')`, like after migration 0018); agents created in a test through `actors.create` or the admin API are
  private and owned by the creating admin – a member or orchestrator test that assigns them must make them public or
  set the owner.
- `Actor.owner` is a self-referencing relationship; API schemas read the name through the property `owner_name`
  (`Field(validation_alias="owner_name")`).
- tmux user options (`@…`) need `set-option -w` to land on the window; without `-w` they become session options and
  every window in the session would show the same owner.
- Without `uv`, run the tests with a venv that has `pytest` and `ruff==0.14.0` and `PYTHONPATH` pointing at the
  site-packages of `~/.local/share/uv/tools/taskbrigade`; a newer ruff reports hundreds of false findings.
- New-task form (`task_new.html`, `static/task_form.js`): `_md_field.html` with `md_tabs` renders "Edit / Preview"
  tabs (preview via `hx-include="#md-<name>"`; the textarea is only hidden, so its value is always submitted); without
  `md_tabs` the other forms keep the collapsible preview. The folded sections open server-side when they hold values
  or when `tasks_ui._error_section` maps the error to them (model error codes → "model"; the parent-project message
  or an entered task number in the message → "links"); add new error codes there. "Cancel" goes to the hidden field
  `back` (same-site referer path, else parent task, project or backlog – `tasks_ui._back_url`). Pages can set
  `{% block body_attrs %} class="tinted"{% endblock %}` for the light grey page background; the folded section is
  called "Task links" (DE „Verknüpfungen“). Chosen files cannot survive a validation error (browser rule) – the user
  has to pick them again.
- Filter selects inside `.filter-more` (`display: contents` on desktop) are no longer direct children of `.filters`;
  their desktop widths come from `.filters > .filter-more > …`, otherwise Pico's `width: 100%` stacks them.
- Task detail error re-renders: `_run(..., **error_ctx)` passes `edit_form` (entered values, form opens) or
  `comment_body` (thread tab selected, text kept) to `task_detail.html`; new error paths that should keep input pass
  them the same way.
- Mobile screenshots/overflow checks: `document.documentElement.scrollWidth` must equal the viewport width at 320,
  390 and 844 px; only `.kanban` may scroll sideways. Demo data for screenshots must use neutral names (no internal
  project keys from mockups).
- FastAPI ≥ 0.121 runs the code after `yield` of a dependency with the default scope "request" only after the
  response was sent – a commit there races with the client's next request (`tbr join` → `whoami` got 401). API routes
  take the session as `session: Session = SessionDep` (`app/api/deps.py`, `Depends(get_session, scope="function")`),
  never `Depends(get_session)`; `tests/test_session_commit.py` checks every route. The web UI commits inside the
  handler (`web_session()`), it is not affected.
- Task detail (`task_detail.html`) uses the new-task form's cards, `_md_field.html` with `md_tabs` and
  `static/task_form.js` (it initialises `#new-task-form` or `#edit`). Project and status are readonly inputs without
  `name` (no project move, status only via actions). The edit form's error re-render passes `edit_form` (title,
  texts, prio, assignee, due, sprint, reviewer, model, and parent/dependencies when `structure` was sent) and
  `error_section` (`tasks_ui._error_section`); an empty `model`/`sprint`/`reviewer` in `edit_form` falls back to the
  stored value. "Discard changes" is a link back to `/tasks/<nr>` with `data-discard` (`form_guard.js` confirms when
  the form is dirty or `data-discard="confirm"` on an error re-render) – not the task's "Cancel task".
- Draft release check (`[data-release-check]`, `data-state` blocked/unassigned/ready) is rendered from the stored task
  and re-evaluated by `task_form.js` from the editor values; the release button is disabled only while description or
  criteria are empty (state machine rule), an unassigned draft only gets the confirm (`data-unassigned` on the publish
  form). `.detail-grid` has `align-items: start` on desktop – the phone flex column needs `align-items: stretch`.
- A disabled assignee select (running task, manager without `state_machine.can_override`) sends nothing; the edit form
  adds a hidden `assigned_to` with the current agent, otherwise `edit_task` reads "" as "unassign" and the save fails.

## Glossary

- **Actor** – a human or an agent account; agents authenticate with an API token.
- **Owner** – the human responsible for a project (reviews, answers questions).
- **Runner** – `tm run`, started by a systemd timer, launches a coding agent for a claimed task.
- **brain-changes** – per-task change file for `brain.md`, applied by Taskmeister on approval.

## Naming research


