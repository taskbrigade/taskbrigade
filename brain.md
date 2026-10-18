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
  are English. Docs and `llm-install-prompt.md` name the English UI labels (the default language), taken from the
  templates or the msgids in `app/locales/de.json`, e.g. *Admin → Users & agents*, “Rotate token”; they mention
  `UI_LANGUAGE=de` once. Change a label in a template → update the docs (grep the old string).
- Commit messages: short imperative summary; reference the task number if there is one.
- Never commit secrets, real hostnames/IPs, personal names or internal project keys (see "Pitfalls").
- Docker docs and the install prompt recommend the public image `ghcr.io/taskbrigade/taskbrigade:<version>` with
  `TBR_PULL_POLICY=always`; the local build stays the alternative. Bump the example version in `.env.example` and
  `docs/docker.md` with each release.
- Reopening a sprint restores exactly the tasks that `close` moved out, from `sprints.moved_task_ids` (migration
  0039): `close` stores the moved task ids and the sprint it moved them to (null = detached) as JSON, and `reopen`
  (`app/services/sprints.py::moved_out_at_close`) moves those ids back. A task counts as skipped when it is
  done/cancelled, is already back in the sprint, or no longer sits in the close target (someone moved it again
  afterwards). Storing the ids is immune to sprint renames and to tasks moved out by hand while the sprint was still
  released – matching the sprint **name** in the `sprint` task events alone is not enough (names are only unique per
  project and pre-close moves look the same). Sprints closed before the column existed (NULL) still reopen: they fall
  back to the `sprint` events written at/after `closed_at` by the close call, limited to the same project.
- Page titles come only from `ui.page_header` in `app/templates/_ui.html` (`h1.ui-page-title`, size in `ui.css`);
  no page-specific heading sizes. Admin pages put the area into the title ("Admin: Settings", same text in
  `<title>`). `tests/test_ui_screens.py::test_page_titles_share_one_style` compares the computed title style of the
  main pages at 390/1440 px.

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
- Screenshot script also captures phone pages (`MOBILE_SHOTS`, 390×844, `is_mobile`/`has_touch`, 2× pixels, files `*-mobile.png`) in a second browser context with the same demo data.
- Docker data zones: `deploy/docker/data-zones-test.sh` (project `tbr-dztest`, subnets `172.31.251.0/24` +
  `172.31.253.0/24`, ports 18091/12223; `TBR_DZTEST_KEEP=1` keeps it) builds the images, starts both profiles and
  runs the self-test probes through the launcher, `deploy/sandbox-test/e2e_job.py` with `TBR_E2E_*` (Docker paths,
  `TBR_E2E_LAUNCHER=docker`), network/socket checks, `runner join` and SFTP upload → import → profile by the runner.
  Never together with the smoke test or the screenshot script. Unit tests without Docker: `tests/test_docker_runner.py`.
- Screenshot and layout tests: `tests/test_ui_screens.py` (marker `db`, Playwright 1.63.0 in the dev group) seeds demo
  data, serves the app with uvicorn on a free port and opens every page of `PAGES` at 390/768/1440 px (plus 360 px
  without screenshots). Checks per page: viewport meta, no horizontal page scroll (content inside an `overflow-x`
  container is allowed), labels, `aria-expanded`/`aria-pressed`, text contrast >= 4.5:1, fixed bottom tab bar not
  covering the page end, on the phone tap targets and 16 px inputs, on desktop visible focus for 25 Tab steps.
  Full-page shots (max. 2400 px, dates/times normalised, `time` masked) are compared with `tests/screenshots/` by a
  canvas pixel diff in the browser (max. 0.2 % differing pixels; actual/diff PNGs go to `TBR_SCREENSHOT_OUT`).
  New baselines: `TBR_UPDATE_SCREENSHOTS=1`; a missing baseline is written and the test fails once. Without a usable
  Chromium the tests skip; on a host whose Chromium misses libraries run a browser server and point the tests at it:
  `docker run -d --rm --network host --user $(id -u):$(id -g) -e HOME=/tmp mcr.microsoft.com/playwright/python:v1.63.0-noble
  sh -c "pip install --user --break-system-packages -q playwright==1.63.0 && python -m playwright run-server --port 39141
  --host 127.0.0.1"`, then `TBR_PLAYWRIGHT_WS=ws://127.0.0.1:39141/`. Make baselines with that image or CI's
  `playwright install --with-deps chromium` (same Chromium build); another Chromium renders differently.
- Phone flows that open only on a tap (More/quick-capture sheets, kanban status tabs and "Move to …" sheet, sticky save
  bars, user panel) plus tablet/desktop header and Inbox answer form: `tests/test_ui_mobile_flows.py` (marker `db`,
  same setup and `TBR_PLAYWRIGHT_WS` as `test_ui_screens.py`). Review copies of the screenshots: `docs/ui-redesign/review/`.

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
- 2026-10-15 – Parking never kills the agent's own background job: before `Supervisor` ends an idle waiting session
  it lists the processes below the agent (`runner.descendant_processes`, `ps -A -o pid=,ppid=,args=`). Processes
  already there `BASELINE_SECONDS` (60 s) after the start (MCP servers, CLI helpers) are ignored; any other one keeps
  the session open (waiting counts as activity, as without parking) and leaves one comment per waiting period naming
  the processes; the session is parked once they are gone. Chosen over a warning plus parking because the job would
  be lost (a Claude Code `run_in_background` job died with the parked session, ~40 min lost). If `ps`
  fails, parking works as before. Agent instructions (TASK.md, Hermes/OpenClaw skills) say to start long jobs with
  `setsid nohup <cmd> > <log> 2>&1 < /dev/null &`: the starting shell exits, the job is re-parented away from the
  agent, gets its own session and survives the end of the tmux window.
- The earlier promotion and awareness plans live in `research/promotion-plan.md` and `research/awareness-plan.md`
  (moved out of `docs/` because they name third-party X handles); they are source material only. External account
  identities and community rules were researched on 2026-10-15 and must be rechecked before outreach.
- 2026-10-16 – Model location of agents (`actors.model_location` `cloud`/`local`/`none`, migration 0020) next to
  `runtime`, and one zone helper `app/services/zones.py` for every privacy decision (`members.check_privacy`,
  `reviews.effective`, `tasks._review_jobs`/`claim`/`_agent_reviewed`, `actors.update` via `zones.check_switch`,
  the project privacy switch, the access table in *Admin → Users & agents*). Setting
  `privacy_enforce_model_location` (bool in `app_settings`, default `true`): on → an agent may work on `local_only`
  only with runtime `local` **and** model location `local`; off → the old runtime rule. Migration 0020 sets every
  actor to `cloud` and stores `false` only when agents or projects already exist, so new installations enforce and
  existing ones keep working until the admin has worked through *Admin → Settings → Model location findings*
  (`zones.findings`). Humans keep the runtime rule in both modes; `none` (runner) never gets `cloud_ok`/`local_only`
  work and is accepted only for `agent_kind = runner` (not there yet). `claude_code`/`codex` are fixed to `cloud`;
  switching to `local` in the UI needs the checkbox `confirm_local_model` (`zones.require_confirmation`, UI only –
  the admin API takes the field directly). Claim now filters work tasks by zone too (`zones.sql_classes`), so a task
  assigned before enforcement is skipped and a named claim answers `422 privacy_violation`.
- 2026-10-16 – Stage-1 workflow persistence uses seven tables and SQLAlchemy models in `app/models.py` (see `docs/workflow-data-model.md`). Runs pin `version_id`; the template's `current_version_id` can advance independently. `task_kind` is introduced once with `work`, `profile`, `run`, `approval` (default `work`); runner `run_*` task fields remain for the data-zone runner task. Schedule/trigger foreign-key columns and schedule-slot uniqueness arrive with their target tables in stages 2/3. Publishing and execution validation belong to the workflow services follow-up.
- 2026-10-16 – Spreadsheet and archive task attachments (`.xlsx`, `.xls`, `.xlsm`, `.csv`, `.tsv`, `.ods`, `.zip`) are refused with `422 use_dataset_upload` when the effective class is `local_only` or `sealed` (`app/services/attachments.py`). Effective class is the task's class (`zones.effective_class`: `tasks.data_class`, else `projects.privacy`). `cloud_ok` is unchanged. `tbr-admin data scan-attachments` lists matching rows already stored in `local_only` projects and deletes nothing. The Data page does not exist yet, so the message names the coming data store instead of linking to it.
- 2026-10-16 – Workflow stage 1 is migration `0021` (revises `0020`). Model location stays `0020` because `tests/test_model_location.py` upgrades to that revision. Both wave-1 branches had used `0020` against `0019`; `0021` restores one linear Alembic head. Databases already at model-location `0020` pick up the workflow tables on the next `upgrade head`.
- Workflow execution lives in `app/services/workflows/` (`docs/workflow-engine.md`). Service callers own the transaction; template/run/attempt locks serialize run numbering and activation, including older repeatable-read snapshots. Only activation creates tasks. Task transitions (also human overrides) update runs in the same transaction. Approval tasks start in `for_review`, use `reviewer_id` without an assignee, and approve without a PR; rejection reopens the prior agent task and creates fresh later attempts. Never route these through normal task submission.
- 2026-10-16 – Data class per task (`tasks.data_class`, migration `0022`, NULL = inherits `projects.privacy`; no
  backfill). Rules 1–4 live in `zones.check_class` (called by `tasks.create`/`tasks._set_class`, so API, UI, CLI and
  MCP share them): stricter always; looser than the project only for owner/admin (`403 data_class_forbidden`, audit
  `data_class_loosened`); `task_kind` `profile`/`run` must be `sealed` (`422 data_class_runner`) – the dataset
  reference part of rule 3 waits for `task_data_refs` (DZ 9); after start (status past `defined` or `claimed_at` set)
  only stricter (`422 data_class_downgrade`). Switching a project `local_only` → `cloud_ok` pins started inheriting
  tasks to `local_only` (`projects._pin_started_classes`). Local humans may work on `sealed` (until `data_access`
  exists); LLM agents never. A loosened task gives its cloud assignee project membership (`members.set_member(...,
  task)`), because the agent needs the repo – that is the concept's "cloud_ok code task in a local_only project".
  Task payloads carry `task_kind`, `data_class` and `data_class_effective`; badges via `_data_class.html`.
- 2026-10-16 – Workflow output contracts and placeholders live in `app/services/workflows/` (parser, resolver, publish checks, submit validation). No Jinja. Agents set outputs with `tbr submit --output/--output-json`, MCP `submit_task(outputs)`, or a `.tm-submit.md` `## Outputs` JSON block. Declared values are stored on `workflow_run_steps.output_json`; file outputs are rewritten to the merge commit on approve. Guide: `docs/workflow-outputs.md`.
- 2026-10-16 – Email as a second notification channel, outbound only (`app/services/mail.py`, migration `0023`;
  concept `docs/konzept-email.md`): TaskBrigade never reads mail (no IMAP/POP, `tests/test_email.py` checks `app/`
  and `cli/`). `notifications.channel` (`telegram`/`email`), one row per recipient and channel, written by
  `notifications._actor_rows` for the channels in `channels_for(pref, event)` that can reach the person
  (`_reachable`: Telegram enabled + linked chat; email enabled + `mail.verified_address`). `notification_prefs.
  channels_json` = `{"events": {event: [channels]}, "bundle": 0|15|60}`; NULL or a missing event = defaults
  (Telegram always, email only for `review`/`sprint_done`, `EMAIL_DEFAULT_EVENTS`); `events` stays the list of
  events with any channel, so `/status` and `tbr notify` keep working. `SETTINGS_EVENT` is Telegram only. Mute and
  quiet hours apply per row for both channels. `notifications.deliver` only sends `telegram` rows, `mail.deliver`
  (job `email_deliver`, 30 s, only the holder of `<state_dir>/email-sender.lock`) only `email` rows: same back-off
  and `MAX_ATTEMPTS`, 5xx for recipient/data = permanent, connection/TLS/login errors are retried (configuration or
  outage; a run stops at the first connection error). Throttle: distinct `notifications.email_message_id` sent in the
  last hour < `mail_rate_per_hour`; bundled rows share one Message-ID. Bundling waits until the oldest due row is
  `bundle` minutes old (a retry or quiet-hours rows go at once). Settings `email_enabled`, `smtp_*`, `mail_*` live in
  `app_settings` only (password secret); no `.env` keys. Address confirmation: `actors.email_verified_at` +
  `email_verify_codes` (24 h, SHA-256 only, bound to the address it was sent to, one-time via conditional `UPDATE`);
  `actors.update` clears `email_verified_at` when `email` changes. `GET /profile/email/verify` needs no login (the
  link proves the address). Test mail and confirmation link are sent synchronously in the request (plain SMTP error
  shown), not through the outbox.
- 2026-10-16 – Data store (DZ 4, migration `0024`, guide `docs/data-store.md`): `datasets` with envelope encryption
  (`app/data_crypto.py`: KEK in `TBR_DATA_KEY_FILE`, one DEK per dataset wrapped with AES-256-GCM, bound to the
  dataset key and tagged with a KEK fingerprint; files in 1 MiB AES-GCM records whose AAD holds header, index and
  final flag), ciphertext under `TBR_DATA_DIR/store`. Service `app/services/datasets.py` (rights, upload, deletion,
  retention, emergency download), UI `app/web/data_ui.py` (menu *Data*, `/data`, `/data/upload`, `/data/<key>`).
  Admin settings `data_enabled` (default off), `data_max_upload_mb`, `data_retention_days`. The header holds a random
  file id instead of the dataset id (the file is encrypted while it streams in, before the row exists); the dataset
  binding is the wrapped DEK. Status after upload is `quarantine` ("Stored – awaits profile") until the profiler (DZ 7).
- 2026-10-16 – `actors.data_access` (data right, humans only, *Admin → Users & agents → Data right*, audited): needed to
  upload as a project member and to work on `sealed` tasks (`zones.allowed_classes`/`sql_may_handle`); it replaces
  "local humans may work on sealed until data_access exists" from DZ 3. Emergency raw download: admin + data right,
  reason required, audited before the first byte. Upload: owner, admin, member with data right; delete: owner, admin;
  list/metadata: project members.
- 2026-10-16 – `data_audit` is hash-chained (`app/services/data_audit.py`) and has no foreign keys (a cascade or SET
  NULL would rewrite hashed rows). Appends lock the single row of `data_audit_head` (`FOR UPDATE` on the primary key,
  no gap locks – locking the last audit row deadlocked concurrent inserts in theory) and assign `id = last_id + 1`
  so the id is part of the hash. Triggers refuse UPDATE/DELETE; the migration only warns when MariaDB refuses to
  create them (binary logging), `tbr-admin data audit verify` reports them. `data_class_lowered` (task looser than
  its project) is written there too.
- 2026-10-16 – Retention job `datasets.retention_sweep` (APScheduler `data_retention` hourly, `tbr-admin data
  retention` for a timer): `GET_LOCK('tbr_data_retention')` on a connection of its own (the session switches
  connections at each commit), reminder once 7 days before (`datasets.retention_warned_at`, notification event
  `dataset_retention`: key, event, link only), crypto-shred after the date. The attachment lock message now points to
  *Data* (`/data/upload`).
- 2026-10-16 – Work results by email (`app/services/result_mail.py`, migration `0025`, guide *operations.md → Sending
  work results by email*): agents file send requests (`POST /tasks/{nr}/emails`, `tbr mail send`, MCP `send_email`),
  never get SMTP credentials; `email_deliver` also runs `result_mail.deliver` and shares the hourly throttle
  (`mail.sent_last_hour` counts sent `outgoing_mails`). Recipients: `project_mail_recipients` (mode `freigabe`/
  `ohne_freigabe`) or an active human with a **confirmed** address (implicitly without approval; a list entry wins).
  Approval only by humans (`result_mail.may_decide`: human admin, project owner, the task's human reviewer) – never by
  an agent, so a prompt injection cannot both file and release a mail; `on_approve` requests are queued by
  `state_machine.transition` after a human approve (an agent approval leaves them pending), a cancel rejects pending
  ones. Attachments are not stored: the request records ref + SHA-256, sending re-reads the file from Gitea
  (`GiteaClient.raw_bytes`; `branch:` falls back to the merge commit) and fails on a different hash. Only `cloud_ok`
  (`zones.effective_class`) may send; `local_only` waits for the output gate (Email 3), `sealed` never. Body and refs
  are cleared after 30 days (`result_mail.cleanup` inside `deliver`); waiting requests expire then.
- 2026-10-16 – Workflow UI stage 1 (`app/web/workflows_ui.py`, view data in `app/services/workflows/ui.py`, guide
  `docs/workflows-ui.md`): the editor posts the whole draft as repeated fields (`s_<field>`/`p_<field>`, DOM order =
  step order) and saves through `workflows.api.save_draft`/`publish`, so UI and API share validation (`to_draft`
  builds the API's `Draft`). The checks panel runs `check_publish` on the *unsaved* form via HTMX. Output/input/config
  contracts are JSON textareas, not row editors. Run detail lives at `/workflow-runs/<id>`, the path the engine
  already puts into `{{run.url}}`. `_workflow_chain.html` is the one chain component (run page, task page, later
  DZ 9). There is no `workflows_enabled` switch yet; the menu shows for every signed-in actor.
- 2026-10-16 – Kanban/backlog filter `workflow` (`hide` default, `all`, `only`, template id; cookie `tm_workflow`,
  `tasks.search(workflow=…)`), counted as an active filter; “Reset filters” links add `workflow=hide`. The inbox
  moves workflow questions/reviews/aborted tasks into `workflow_*` lists (`tasks_ui.workflow_group`) shown first.
  `tasks.summary(..., workflow=)` carries the badge data, filled in batch by `tasks.workflow_badges`.
- 2026-10-16 – Workflow tick trigger `TBR_WORKFLOW_TICK=timer|app|off` (`app/config.py`): native installations run
  `tbr-admin workflows tick` once a minute through `deploy/systemd/taskbrigade-workflows.{service,timer}` (installed
  by `deploy/install.sh`/`update.sh`; `update.sh` stops the timer around the schema upgrade, and the tick itself
  refuses while the schema is not at `head`), Docker runs the same tick as the APScheduler job `workflow_tick`
  (`app/scheduler.py`, image default `TBR_WORKFLOW_TICK=app`). The native default is `timer`, the Docker default
  `app`, so neither install has to set it. `tbr doctor` (via `GET /agents/me/workflow-tick-status`, which returns
  `tick.status`) and `tbr-admin db-status` warn when the last tick is older than five minutes while workflows are
  enabled.
- 2026-10-16 – Data-zone runner (DZ 6, `app/runner/`, `app/services/runner_jobs.py`, `/api/v1/runner/jobs/<nr>`,
  migration `0028`, guide *docs/operations.md → Data-zone runner*): agent kind `runner` forces model location `none`,
  role worker, runtime local; `zones.check_kind`/`sql_kind_for` keep runners on `run`/`profile` tasks and LLM agents
  off them (claim and assignment). The runner runs on the app host (`tbr-admin runner serve`, user `tbr-runner`) and
  decrypts the dataset itself (KEK via group `tbr-data`): the server only hands out storage path and wrapped DEK, so
  plaintext never crosses HTTP. The **server** fetches the script from Gitea with its service token (runners need no
  Gitea user) and only for a `run_commit_sha` that is the `merge_commit_sha` of a `done` task of the same project –
  stricter than the concept's "commit on the default branch", which Gitea's API cannot check cheaply. The input
  dataset is `tasks.run_dataset_id` until `task_data_refs` (DZ 9) exists. `run_results` (concept §6 plus `job_id`,
  `files_json`, `dek_wrapped`, `service_result`, `task_kind`) is created here with status `quarantine`; outputs are
  encrypted with one new DEK per result, wrapped with context `run-result/<job_id>`. Runner submit goes through
  `state_machine._runner_submit` (payload key `_run_result` must be a `RunResult` object, so an API body cannot fake
  it); approving or rejecting a `run`/`profile` task like a code task raises `409 output_gate_required` – the output gate
  decides (DZ 8). Profile jobs abort with
  `profile_unsupported` until the profiler (DZ 7). Script contract in TASK.md via payload flag `script_contract`
  (work task in a project with datasets, or a `run` task depends on it).
- 2026-10-16 – Native SFTP inbox (DZ 10, `docs/konzept-datenzonen.md` §4, migration `0029`, guide
  `docs/data-store.md`): OpenSSH `internal-sftp` with a per-user chroot under `TBR_SFTP_DIR`
  (default `/srv/taskbrigade-sftp/<user>/upload`, `root:root 0755` above, `<user>:tbr-data 0730` for `upload`).
  The drop-in `deploy/ssh/60-taskbrigade-sftp.conf` (`Match Group tbr-sftp`) forces SFTP, forbids shells and
  forwarding and allows keys only; `install.sh`/`update.sh` install it only after `sshd -t` passes and then reload
  sshd. Accounts (`data_sftp_accounts`, one project each) and their keys (`data_sftp_keys`, `authorized_keys` line
  with `restrict,command="…/sftp-server"`) are managed by `tbr-admin data sftp-user …` (`app/services/data_sftp.py`).
  The systemd path unit `taskbrigade-data-import.path` starts `tbr-admin data sftp-user import`
  (`app/services/data_import.py`, DB lock `tbr_data_import`): a file is finished after 60 s without change or with
  a `.done`/`.ready` suffix, is encrypted into the store (`upload_channel=sftp`, `dataset_uploaded` audit, project
  notification), then the plaintext is removed. `TBR_DATA_SFTP=1` needs `TBR_DATA_ZONES=1`.
- 2026-10-16 – Local-model agents read `local_only` datasets (DZ 14, `docs/konzept-datenzonen.md` §11): `GET
  /api/v1/data/datasets/{key}/content` streams the decrypted file to an agent whose `model_location = local` (and
  `runtime = local` once `privacy_enforce_model_location` is on) that is a member of the dataset's project. The
  right is checked with the zone helper `zones.may_read_dataset`: only `local_only` datasets are readable, and only
  by an agent whose `allowed_classes` include `local_only` (`model_location=local`); `sealed` and `cloud_ok`
  datasets are never readable over this path, `model_location=none` (runner) agents are refused, cloud-model agents
  get `403 data_read_forbidden`, non-members `404`. Every read
  is written to the data audit (`dataset_read`, `via="api"`) and committed before the first byte streams
  (`app/services/datasets.py:open_agent_read`, `app/api/data.py`). The dataset detail page marks `local_only` as
  readable by local agents and `sealed` as runner/human only (`data_detail.html`).
- DZ 7: `app/runner/profiler.py` is a standalone trusted sandbox entry point for CSV/TSV/XLSX. Upload/import queues a sealed profile task; encrypted outputs are authenticated and ingested by `dataset_profiles`. Generated profiles stay human-only until release from the dataset detail page. Release applies aliases, recomputes the structural SHA-256 fingerprint, creates a draft cloud code task and completes the profile task; rejection permits a fresh runner attempt. See `docs/data-store.md`.
- 2026-10-16 – Workflow usage tracking and per-run budgets (WF 9): `task_usage` logs voluntary token/cost reports
  (`--usage` on `tbr submit`/`tbr heartbeat`, MCP `submit_task`/`report_waiting`, plus the runner reading agent
  output); each report increments the run's `used_tokens`/`used_cents`. A budget of 0 or NULL means no limit. At
  80 % the owner gets `workflow_budget_warning`, at 100 % the run pauses with `budget_exceeded` (no further
  `agent`/`script` step activates; `approval`/`notify` continue), the heartbeat replies `budget_exceeded`, and
  resuming requires a higher budget. Reports are unverifiable, so the run detail shows "usage unknown" until the
  first report.
- 2026-10-16 – Planning board `/planning` (`app/web/planning_ui.py`, `planning.html`, `_plan_card.html`,
  `static/planning.js`): one project at a time (`project` parameter, cookie `tm_planning_project`), columns "No sprint"
  (open, non-workflow tasks without a sprint) + released, then planned sprints by start date/id; closed ones only with
  `closed=1`, folded and not droppable. Menu item and page only for actors who may plan somewhere
  (`planning_ui.may_plan_somewhere`: admin/orchestrator role or owner of a non-archived project), others get 403.
  Drag and drop posts `POST /planning/move` (`task_nr`, `sprint` = id or `none`, `order`): `sprints.require_plan` +
  `target_for` + `assign`; finished tasks are refused (`task_finished`), errors come back as JSON `{message}` and
  the card snaps back. Only planned-sprint lists are sortable; their `order` goes to `sprints.reorder`, which sets
  `sprint_rank` for every task and the draft prio via `prio_from_order` (same formula as the sprint editor's "Prio
  from order") through `_update_draft` (`tasks.update`, also used by `save_plan`). Release/close/edit/"+ Sprint"
  dialogs post to the existing sprint routes with `back=planning` (redirect back to the board, errors as `err`);
  the release dialog loads `GET /planning/<key>/sprints/<id>/release` (`_release_preview.html`, shared with
  `sprint.html`). Quick add `POST /planning/<key>/quick-add` reuses `parse_batch` and `batch_create`, which now
  accepts `sprint=None` with `project=` for drafts without a sprint.
- 2026-10-16 – Output gate (DZ 8, `docs/konzept-datenzonen.md` §6/§8, migration `0032`, guide *docs/data-store.md →
  Output gate*): checks 1–3 and 6–9 are pure functions in `app/services/gate_checks.py`; `app/services/output_gate.py`
  decrypts `result.json` on the server, stores `checks_json` (codes, counts and locations like `t0.r3.c1`, never
  values) and decides. Checks 4 and 5 need the input and run as a **second sandbox job** `<job>-gate` with our own
  self-contained probe `app/runner/gate_probe.py` (stdlib + openpyxl/pyarrow from the runner venv; the input file is
  moved, not copied, into the probe job) – not in the runner process as the concept says, because parsing an untrusted
  spreadsheet must not happen in a process holding the KEK. A missing or failed probe fails check 4 (fail closed).
  Identifier columns: personal-looking column names, value patterns (IBAN, email, phone with leading `+`/`0`, tax id
  on the text as written) or text columns with distinct values > 50 % of rows; dates/numbers never by cardinality.
  Check 4 compares string cells, names and metric keys (also runs of up to four words); check 5 compares rows as sets
  of normalised cells (one missing cell still counts from six cells on). Check 7 needs the script to declare
  `"max_contribution": {"sum": "max"}`; the helper column is removed on release. Decisions only via the web UI
  (`/data/results/<id>`): release (`released_json` = rounded form), reject (reason required) or keep in zone R
  (`kept_sealed`, also from `checks_failed`); four-eyes (`projects.data_four_eyes`, NULL = setting `data_four_eyes`,
  default on): the releaser is neither the run task's creator nor the actor of the script task's `merged` event.
  State machine rules `gate_release`/`gate_reject`/`gate_keep`/`gate_return` (who `gate`) need the `RunResult` object
  in the payload (`_gate`), so no request can trigger them. A sent-back run task (`checks_failed` or rejected) has its
  `run_commit_sha` cleared and claim skips `run` tasks without a commit; setting new run fields (allowed in
  `changes_requested`) makes it claimable. Agents read only `released` results (`GET /api/v1/data/results/<id>`,
  404 otherwise) and only while they hold an open task in the project – until `task_data_refs` (DZ 9) narrows it.
- 2026-10-16 – Task chain across zones (DZ 9, `app/services/data_refs.py`, migration `0034`, guide *docs/data-store.md
  → Task chain across zones*): `task_data_refs` (one purpose per type: `dataset`/`input`, `profile`/`schema`,
  `result`/`output`; no FK on `ref_id`, `created_by` nullable). Rule 3 lives in `data_refs.check_rule` (refs) and
  `data_refs.check_class` (called by `tasks._set_class`); unreleased profile/result refs are refused for `local_only`
  too, not only zone C (local models are LLMs). Runner tasks keep `tasks.run_dataset_id` for the runner;
  `data_refs.sync_run_input` mirrors it as the input ref (`tasks.create`, `runner_jobs.update_run`). Follow-up
  drafts: `propose_script` (from `dataset_profiles.decide`), `propose_run`/`propose_interpretation` via the hook
  `data_refs.on_transition` in `state_machine.transition` (approve of a work task with a profile ref; `gate_release`);
  drafts are created by the project owner as follow-ups and skipped for workflow tasks. The run draft's script path
  comes from `GiteaClient.pull_files` (exactly one `.py` outside `tests/`), else empty – `normalize_run(draft=True)`
  allows that, `require_definition` refuses publishing (`run_fields_incomplete`), the task page's *Data* box has a
  *Run fields* form (`POST /tasks/<nr>/run-fields`). Agent reads of released profiles/results go through
  `data_refs.agent_may_read` (open task with the ref, assignee or reviewer, zone allowed); this replaces the
  `code_task_id` rule of DZ 7 and the "any open task in the project" rule of DZ 8. TASK.md section "Approved data
  artifacts" comes from `GET /tasks/<nr>/artifacts` (`cli.prepare.artifacts_note`, only when the payload has
  `data_refs`; reads audited `profile_read`/`result_read`). The chain view reuses `_workflow_chain.html` with cells
  from `data_refs.chain` (`zone_chain` cells carry `label`, `task_status`, `who`, `handover`).
- 2026-10-16 – Release warning for late dependencies: `sprints.late_dependencies(session, sprint, task_ids)` (one
  query) lists dependencies that are not `done` and have no sprint, sit in a planned sprint or in a later one –
  later by start date when both sprints have one, otherwise by id (creation order). The same sprint and earlier
  released/closed sprints do not count. `release_preview` and `release` both return it as `waiting` (only tasks that
  are released), shared via `_release_lists`; it is a warning only, never a refusal. The sprint page's *Waits for*
  column uses the same rule for every unfinished task; the planning card's "waits for" hint
  (`planning_ui.waiting_for`) still lists every unfinished dependency. Partial `_late_deps.html` renders the list
  (`t.waits_for`) in the release dialog and the sprint table. The project page only lists sprints and links to
  `/planning?project=KEY`; `POST /projects/{key}/sprints/new` stays for the planning board's "+ Sprint".
- 2026-10-16 – Docker data zones (DZ 12, guide and design *docs/docker.md → Data zones*): compose profiles `data`
  (`data-init` root one-shot = `TBR_DATA_ZONES=1 install.sh`, `runner-launcher`, `runner`) and `data-sftp` (`sftp`).
  The runner never gets the Docker socket: `app/runner/docker.py` (launcher, `python -m app.runner.docker`) holds it,
  listens on a 0660 unix socket in the tmpfs volume `taskbrigade-launcher` (peer uid checked with `SO_PEERCRED`) and
  accepts only `run`/`stop <job id>`/`ping`; it builds each job container from its own template (`job_spec`:
  `network_mode: none`, read-only root, `cap_drop: ALL`, `no-new-privileges`, user `tbr-job`, pids/memory/CPU/
  fsize/nofile, log driver `none` + shell redirect into `/job/log`, volume `Subpath` mounts of only this job's
  `in`/`script` (ro)/`out`/`log`, `mode=000` tmpfs over `/app`, `/home`, `/root` and the state paths). A second
  container as `tbr-job` does the `chmod -R g+rwX` of the unit's `ExecStopPost`. `DockerLauncher` is the runner-side
  drop-in for `SystemdLauncher` (`runner.toml` `launcher = "docker"`, image env `TBR_RUNNER_LAUNCHER=docker`,
  `service.make_launcher`); job limits travel in `JobDir.limits`, clamped by the launcher. Rejected: socket in the
  runner, a socket proxy (cannot filter the create body), nested bwrap/nsjail (needs userns), extra runtimes.
- 2026-10-16 – Docker ids are fixed in the images (taskbrigade 10001, tbr-runner 10002, tbr-job 10003, tbr-data
  10004; SFTP deliverers 20000 + account id) because volumes are shared between containers. Data dirs get the
  set-group-id bit (`app/runner/container.py:layout`), so store files (app) and quarantine files (runner) carry
  `tbr-data`; the app has `group_add: ["10004"]`. Tmpfs volumes (`taskbrigade-jobs`, `taskbrigade-launcher`) take
  owner/mode from `driver_opts.o`, because a tmpfs starts empty on every mount.
- 2026-10-16 – Runner network: internal compose network `taskbrigade-runner` (`TBR_RUNNER_SUBNET`, default
  `172.31.252.0/24`) with only app and runner; the app setting `TBR_RUNNER_NETWORKS` makes it an API peer
  (`Settings.api_client_networks`) and allows join codes from it (`Settings.is_runner_peer`, `/join`), but never
  loopback trust. API_NETWORKS stays opt-in through `.env` only.
- 2026-10-16 – Docker SFTP inbox: the app container cannot `useradd`, so `TBR_SFTP_PROVISION=spool` makes
  `data_sftp` write the desired state to `TBR_SFTP_SPOOL` (format `tbr-sftp-accounts/1`, 0644, purge list kept
  across writes); `deploy/docker/sftp/sftp_sync.py` (root in the SFTP image) applies it every few seconds and
  supervises `sshd -D`. Import runs as the APScheduler job `data_import` (`TBR_DATA_IMPORT=app`). Upload folders are
  `<user>:tbr-data 2770` there (native: 0730) – listing needs read, set-group-id gives deliveries the data group.
- 2026-10-16 – Workflow event triggers (WF 12, migration `0036`, `app/services/workflows/events.py`, guide
  `docs/workflow-events.md`): `workflow_events` is an outbox, `workflow_triggers` is the per-template config,
  and `workflow_trigger_fires` plus `dedupe_key` make delivery exactly once (the concept only named
  `processed_at`; a crash between the run insert and that timestamp would otherwise start a second run).
  `dataset_ready` is written when a dataset becomes `ready`, not at upload, because the concept says "after
  profiling". `workflow_failed` is written when a final step failure pauses the run: WF 8 never sets run
  status `failed`, and a manual pause or cancel does not emit. Sealed and local-only step outputs are left
  out of the payload so they are not stored on the next run. Event triggers skip on overlap; they have no
  overlap policy column in the concept. `workflow_runs.trigger_id` is the concept's foreign key.
- 2026-10-16 – Workflow step conditions (`workflow_steps.condition_tpl`, WF 13): a `when:` template resolves with the
  normal placeholder resolver (params, run fields, earlier step outputs). An empty/whitespace result or one of the
  falsy literals (`false`, `0`, `no`, `off`, `none`, `null`) skips the step with `status=skipped`,
  `error_code=condition_false` and the condition template in `error_detail` (the run detail's "reason"). An
  unresolved placeholder fails closed (`placeholder_unresolved`, run paused) exactly like a step text. Conditions
  are only allowed on `agent`, `notify`, `script` and `runner` steps – `approval`/`gate` are human/zone-boundary
  decisions and can never be skipped (publish error `workflow_condition_invalid`).
- 2026-10-16 – Parallel workflow step groups (`workflow_steps.parallel_group`, WF 13): consecutive steps sharing the
  same non-empty group name activate together; the run advances past the group only when every member is
  `succeeded`/`skipped`. Members keep their own tasks, retries, clocks and failure handling (WF 8): a failed member
  pauses the run while sibling tasks keep running. Groups may only contain `agent` and `notify` steps (publish error
  `workflow_parallel_invalid`; a lone group name is also rejected). `run.current_position` is the group's first
  member. Alembic `0036` adds both columns to `workflow_steps`.
- 2026-10-17 – The German concepts moved from `docs/` to `research/konzept-datenzonen.md` and
  `research/konzept-workflows.md` (DZ 15/WF 15) instead of being deleted: code comments cite their sections (`§n`)
  for the reasoning (design alternatives, risk table). `research/` is not part of the public export, so public docs
  never link there – they name the path as plain text. Earlier entries that say `docs/konzept-…` mean these files.
  The implemented state is documented in English: overviews `docs/data-zones.md` and `docs/workflows.md` (each with
  “Deviations from the concept”), details in the existing guides. Code comments keep citing the research file; a
  new feature documents itself in the English guides, not in the concept.
- 2026-10-17 – UI redesign foundation: the redesign tokens are `--ui-*` in a second `:root` block at
  the top of `app/static/app.css`, next to the older `--tb-*`/`--st-*` palette; pages move to `--ui-*` one by one,
  `--tb-*` can go once no page uses it. Components live in `app/static/ui.css` (loaded after `app.css`), classes
  start with `ui-`, Jinja macros in `templates/_ui.html` (`{% import "_ui.html" as ui %}`); showcase
  `/admin/ui-kit` (`app/web/admin_ui_kit.py`). `ui.css` holds no literal colours and every text/background token
  pair keeps ≥ 4.5:1 – both checked by `tests/test_ui_kit.py`; add a new pair there. Breakpoints: phone < 640,
  tablet 640–1023, desktop ≥ 1024 (`ui-hide-phone`, `ui-only-phone`, …; table → cards: `ui-table-cards` + `data-label`
  on each `td`). IBM Plex (OFL) is self-hosted in `app/static/vendor/fonts/` (Fontsource 5.3.0, Latin subset) and
  set globally through `--pico-font-family-sans-serif`/`-monospace`.
- 2026-10-17 – The shared header is `templates/_app_header.html` with `static/header.css`: one row at >=1024 px, scrollable nav plus full-width capture at 640–1023 px, bottom tabs and capture/More sheets below 640 px. `shell.js` retains the generic disclosure/tab behavior and adds N-to-capture and explicit sheet close buttons. Avatar disclosures contain profile, language and logout. Existing feature/permission checks also apply in the phone More sheet. Header assets participate in the static version hash. The phone badge uses the Inbox's Needs your action categories (questions, human reviews, aborted tasks, email approvals and data releases), with the same owner/sprint scope; workflow actions remain in their separate Inbox group.
- 2026-10-17 – Task dependencies from clients: `tbr update --depends-on` (repeatable, replaces the list, `none` clears, omitted = unchanged) and MCP `create_task`/`update_task(depends_on=…)` (list or comma-separated string, `"none"` clears on update) share `cli.client.parse_depends_on`. The server's `tasks._set_dependencies` now also refuses circles (`422`), since a circle makes tasks unclaimable forever.
- 2026-10-17 – Inbox redesign (UI 3, `app/templates/inbox.html`): action cards (review/question) with type pill, key,
  prio, actor chip, age and inline actions; a segmented filter `tab` (`all`/`reviews`/`questions`) and the sprint filter
  are URL query params. The right-hand "Notices" panel is a notification feed from the `notifications` outbox
  (`notifications.notices_for`, one entry per event, deduplicated across channels) with a colored dot per event
  (conflict amber, sprint_done green, others navy); "Mark all read" sets `notifications.read_at` (migration 0039),
  `POST /inbox/notices/read-all`. The old informational notices (agent review, unassigned, expired leases) stay in the
  panel as collapsible `details`. Review "Request changes" reuses the existing `reject` action (comment required);
  question "Send" reuses `answer`. A new `reltime` Jinja filter renders short relative ages ("12 min ago").
- 2026-10-17 – Kanban redesign (`docs/ui-redesign/kanban.html`): the board always shows all seven columns
  (`KANBAN_COLUMNS`, `app/web/tasks_ui.py`) as a grid `repeat(7, minmax(0, 1fr))` with `min-width: 1260px` and
  horizontal scroll on smaller screens; the grey column backgrounds stretch to the tallest column (`.kanban-board`
  modifier on the shared `.kanban` grid, so the planning board keeps its old behaviour). Below 640 px the board
  snap-scrolls with ~85 vw columns and a sticky row of status tabs (`data-column` buttons, `static/kanban.js`)
  replaces the "Status column" picker; the "Move" card menu opens as a bottom sheet. The card shows a mono task key
  with model/runner tags and the priority badge, a sprint line, an assignee chip and the project key; the data-zone
  badge is rendered only for `local_only`/`sealed` (not `cloud_ok`). A card filter search (`q`) was added to the
  filter row. "Swimlanes by agent" (segmented control, `view` parameter/cookie `tm_kanban_view`,
  `_kanban_swimlanes`) renders one seven-column board per assignee; drag and drop stays within each swimlane
  (one Sortable group per board). "Draft" ends with "+ Add task", "Done" with "Show all N in Backlog".
- 2026-10-17 – Planning `/planning` is a sprint accordion list instead of the column board (`app/web/planning_ui.py`,
  `_plan_sprint.html`, `_plan_row.html`, `static/planning.js`, replacing `_plan_card.html`): one full-width card per
  sprint with expand/collapse (planned/released open by default, the choice per project in
  `localStorage["tb-plan-open:<key>"]`), name, status pill, a green progress bar with "<done> / <total> done" and a
  primary "+ Task". The segmented filter Active / Planned / Closed / All plus the "Show closed sprints" toggle
  (`closed=1`) replace the old `closed=1`-only switch; closed sprints are hidden by default in every filter and shown
  dimmed. `planning_ui._sprint_rows` loads every sprint of the project with its rows in a constant number of queries
  (`filter_counts` counts the filter badges from the same rows), and `visible` narrows the list. Tasks move between
  sprints by drag & drop on the `<ul class="plan-rows">` and by the "Move to sprint …" row menu (`data-move-sprint`, a
  `details` popup) – both post `POST /planning/move` (unchanged contract: `task_nr`, `sprint` = id or `none`, `order`),
  the answer replaces the row in place. The "+ Task" button opens a dialog that posts to
  `POST /planning/<key>/quick-add` (drafts, one per batch line, audit `via="planning"` as before) and returns to the
  card (`#sprint-<id>`). Release, close, edit and reopen keep posting to the sprint routes with `back=planning`.
- 2026-10-17 – The planning board's default filter is `all` with closed hidden (`planning_ui.DEFAULT_FILTER`) – like
  the mockup, where "All" is the pressed tab: `/planning` without parameters shows planned and released sprints.
  `active` now means running, i.e. planned + released (the mockup's "Active" tab); the badge counts come from the same
  rows. `sprints_ui._planning_url` therefore returns to `filter=all` (closed sprints with `closed=1`) instead of the
  previous `filter=planned`/`filter=active` split, so a sprint just acted on stays in view.
- 2026-10-17 – Profile redesign (`docs/ui-redesign/profile.html`, `app/web/profile.py`, `app/templates/profile.html`,
  `app/static/profile.css`): the page uses the `--ui-*` components (`ui-page-header`, `ui-card`, `ui-table`,
  `ui-switch`). A sticky section nav anchors Display / Channels / Notifications / Quiet hours & mute / Delivery log
  (ids `#display`, `#channels`, `#notifications`, `#quiet`, `#delivery-log`; `#display` and `#notifications` are kept
  because `_own_projects_hint.html`, the inbox and mail headers link there). The notification matrix groups events
  into Tasks / Workflows / Data (the groups are defined in the template, filtered by the `events` mapping) with one
  Telegram/Email checkbox per event (`name="ch_<event>_<channel>"`, unchanged) and the admins-only "All projects"
  checkbox in the card header. One combined form posts to `POST /profile/notifications`, which now also saves the
  display switches (`show_all_projects`, `show_all_agents` for admins) via `form.getlist`; the display switches sit
  outside the form element and are associated with it via the HTML `form` attribute (`id="pf-settings"`), while the
  channel cards live in their own small forms (Telegram link/unlink, email save/verify) so their buttons are not the
  settings form's implicit default submit. The delivery log always renders (empty state for new users) with a status
  pill, recent 20 entries; `?log=all` shows all. On phones (<640 px) the section nav is a `<select>`
  that sets the URL hash, the matrix keeps three columns, and the save button is sticky at the bottom.
- 2026-10-17 – Projects overview (`/projects`) numbers come from `projects.overview` (one grouped query per fact:
  task counts, last activity, active agent members) instead of a query per project – the list/card view shows
  counts, agent count and last activity for every project without an N+1. "Last activity" is the newest task
  `updated_at` of the project, not `projects.updated_at`: the latter only moves on settings edits and would show
  "no activity" while tasks are being worked on. The list is always the default; the card view applies only while `?view=cards` is in the URL. Nothing is
  remembered: the `tm_projects_view` cookie is no longer written and an old value is ignored and deleted, so a
  stale cookie cannot force the card view. On the phone (< 640 px) the view switch is hidden and a small script
  drops `?view=cards` too, so the phone always uses the list. The "Current" project of the cards
  view is the first entry of the quick capture list (`common.quick_capture_projects`), so the pill matches the
  preselected project in the header. `i18n.relative_time` renders the relative times ("today", "3 days ago",
  fallback: locale date) and is registered as the `reltime` Jinja filter.
- 2026-10-17 – Projects overview scope and toolbar: `?mine=mine` filters by `owner_id == me.id` whenever the
  segment is "mine" – also for a user whose profile switch "Show all projects" is on (the switch only forces mine
  when off); the header number counts the active projects of the selected scope, not of all projects. Only the
  cards view moves the "Current" project to the front, the list keeps the chosen sort. The toolbar is one GET form
  with `onchange="this.submit()"` (selects apply immediately) and a hidden `view` input, so clicking a segment does
  not drop the other filter.
- 2026-10-17 – Phone touch floor in `ui.css` (< 640 px): `--ui-h*` become 44 px, every button,
  `[role=button]`, summary, select and text-like input gets `min-height`/`min-width: 44px !important` and form fields
  16 px text (`!important`, because page styles fix desktop heights); segments, tabs, labels with a checkbox/radio
  and listed stand-alone links (`.ui-key`, `.task-key`, breadcrumbs, result/filter links) get 44 px high boxes.
  Small links and checkboxes count as fine when their 44 px area overlaps no other target (Lighthouse rule), links in
  running text are exempt – making every inline link 44 px high would double the card heights.
- 2026-10-17 – Admin pages share `admin/_nav.html`, now `ui.tabs` (Database, Users & agents, Settings, UI kit; the
  latter three only with a ready DB) plus the message/error banners; the old `nav.subnav` styles are gone. Login and
  error pages are one `.ui-auth-card` on `.ui-page`; stand-alone forms use `.ui-form-card`. Inbox, kanban and planning
  use `ui-seg` instead of their own `.segmented`/`.plan-segment` styles (the old ones missed 4.5:1).
- 2026-10-18 – Shared page canvas (UI 15): `html, body` use `--ui-bg`; `base.html` wraps every page in
  `<main class="app-main">` (`ui.css`: max-width 1400px, centred, padding 24px, 16px below 640px, safe-area insets
  included) instead of Pico's `container-fluid`. Pages add no wrapper box, `max-width` or side padding of their own –
  `.ui-page` only sets font and text colour. `--pico-background-color` stays white because Pico paints `td`/`th` with
  it (tables would turn grey); sticky bars on the page use `--ui-bg`. `tests/test_ui_screens.py` (`CANVAS_JS`) checks
  body colour, container padding/width, transparent `.ui-page` and that the first `h1`/`.ui-page-title` starts at the
  container edge on every page and width.
- 2026-10-18 – Planning segmented-filter counts always cover every sprint of the selected project (closed ones
  included), matching the subtitle total; the "Show closed sprints" toggle only narrows the displayed list, never the
  counts (`planning_ui.filter_counts(all_rows)`). The expand/collapse chevron button is a 32px square with
  background `#F1F3F5` and `#3A4554` chevron.
- 2026-10-18 – Quick capture project select (`_app_header.html`): a native `<select>` whose options read
  "KEY – name"; with JS the closed control shows only the key in mono via `.qc-project-key` laid over the select
  (select text `color: transparent`, options keep their colour) and `shell.js` syncs key and tooltip (label `title`)
  on `change`/`pageshow`. Native keeps keyboard, Esc and the phone pickers; a custom listbox would not. Without JS
  the select shows the full option text. On phones the overlay is hidden and the full text shows in its own row.
  The project choice is not remembered: the select always starts at the first entry of `quick_capture_projects`.
- 2026-10-18 – Projects cards view polish (UI 24): the shared `ui.status` macro now handles `kind="project"`
  (idea/active/paused/archived) via `project_status_label`, so the card pill never prints a raw status value. In the
  cards view the progress bar spans the full card width (`width: 100%`, the list keeps its `140px` cell) and cards in
  a row are equal height (`.ui-card-grid` stretches, `.ui-project-card .ui-card-body` grows, the footer is pinned with
  `margin-top: auto`); the status pill stays top-right next to the key (`flex-wrap: nowrap` on `.ui-card-head`). The
  List/Cards toggle sits flush right: Pico's `[role="search"] > * { flex: 1 1 auto }` grew the toggle span while its
  inner `.ui-seg` stayed compact and left-aligned (a ~100px gap), so `.ui-toolbar > :is(.ui-seg, select.ui-select,
  .ui-toolbar-view)` is `flex: 0 1 auto` – only the search field grows, the other controls stay compact and flush.
- 2026-10-18 – Reviews by reviewer agents are Inbox action cards again (`agent_review` macro in `inbox.html`, filter
  "Reviews"): `tasks.inbox` `agent_review` (for_review with an effective reviewer agent; rows carry `submitted_at`/
  `review_claimed_at`) and `agent_decided` (status `changes_requested` whose latest `status_change` event came from
  `for_review` by the task's reviewer agent; rows carry `decision` = the action note and `decided_at`; gone once the
  assignee claims). `n_reviews` and `common.inbox_badge` count both lists; the Inbox page computes the badge before
  the segmented filter empties categories. Running reviews offer the owner/admin overrule (approve/reject via
  `/inbox/{nr}/…`); decided ones only open task/diff, because approve/reject need `for_review`. The "Review by agents"
  notice in the side panel stays. Before UI 3 agent reviews were only a "Notices" table, never "Needs your action".
- `tasks.inbox(..., cols={key: column})` merges extra columns into each row summary – use it instead of adding
  inbox-only keys to `tasks.summary` (that dict is also returned by the sprints API).
- 2026-10-18 – Profile display switches have their own form `#pf-display` , posted on every change
  via htmx to `POST /profile/display` (answers with the status text only; `<noscript>` save button as fallback).
  Before, they were bound by the `form` attribute to the notification form, whose "Save all changes" button sat far
  below – toggling a switch and leaving the page (e.g. via the language toggle next to it) silently dropped the
  change, so the switches looked broken. `POST /profile/notifications` no longer reads or clears them. This
  replaces the 2026-10-17 note that the combined form saves the display switches.
- 2026-10-18 – "Show all agents" off for an admin (`access.own_agents_only`) shows only the admin's **own** agents
  – also when other owners' agents are public, and agents without an owner are hidden too (owner decision). Applies to `access.agent_choice_filter` (assignee/reviewer choices, agent filters, project default
  reviewer) and Admin → Users & agents (`admin_users._users_page`: humans always listed, segment counts follow the
  visible rows, hint "N agents of other owners hidden – Show all agents" linking `?all_agents=1` for that view; a
  hidden agent's detail link still opens). Members have no switch and keep own + public. Display only: the API
  (`GET /actors`) and the assignment permission (`may_use_agent`) are unchanged; assigned agents stay visible via
  `keep`. Tests: `tests/test_display_switches.py`.

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
- `deploy/release/export-public.sh` drops `EXCLUDED_PATHS` (`research/`, `Marketing/`, `Produktvideo/`, `docs/naming/`, `docs/ui-redesign/`, `media/`,
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
- `tests/conftest.py:ready_db` also empties the seven workflow tables, `datasets`, `data_audit` (TRUNCATE) and `data_audit_head` (reset), `telegram_actions`, `question_reads`, `notifications`, `notification_prefs`, `telegram_link_codes`, `telegram_links`, `outgoing_mails` and `project_mail_recipients`; `test_schema_options_and_seed` counts 32 tables (with `email_verify_codes`). Reset workflow rows before projects, tasks and actors; the fixture temporarily disables foreign-key checks to handle the current-version and task/run-step cycles.
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
- Task detail (`task_detail.html`) is a read view by default (result card, rendered description, criteria checklist,
  activity, properties sidebar with inline popovers). "Edit" opens the previous form: the new-task form's cards,
  `_md_field.html` with `md_tabs` and `static/task_form.js` (it initialises `#new-task-form` or `#edit`). Project and
  status are readonly inputs without `name` (no project move, status only via actions; humans override status under
  "Admin: override status"). The edit form's error re-render passes `edit_form` (title, texts, prio, assignee, due,
  sprint, reviewer, model, and parent/dependencies when `structure` was sent) and `error_section`
  (`tasks_ui._error_section`); an empty `model`/`sprint`/`reviewer` in `edit_form` falls back to the stored value.
  "Cancel" is a link back to `/tasks/<nr>` with `data-discard` (`form_guard.js` confirms when the form is dirty or
  `data-discard="confirm"` on an error re-render) – not the task's "Cancel task". Sidebar property popovers post the
  same edit endpoint and repeat title, description, criteria and the fields they do not change. The read view parses
  acceptance criteria with the `criteria_view` filter (Markdown checkboxes; HTML is escaped).
- Draft release check (`[data-release-check]`, `data-state` blocked/unassigned/ready) is rendered from the stored task
  and re-evaluated by `task_form.js` from the editor values; the release button is disabled only while description or
  criteria are empty (state machine rule), an unassigned draft only gets the confirm (`data-unassigned` on the publish
  form). `.detail-grid` has `align-items: start` on desktop – the phone flex column needs `align-items: stretch`.
- A disabled assignee select (running task, manager without `state_machine.can_override`) sends nothing; the edit form
  adds a hidden `assigned_to` with the current agent, otherwise `edit_task` reads "" as "unassign" and the save fails.
- Tests must not touch real `/etc` paths: on a host where `/etc/taskbrigade` exists but is not readable for the user
  (e.g. a production host), `Path.exists()` raises `PermissionError` under Python 3.12 instead of returning `False`. `env_file()` checks
  the default and legacy files through `admin_config._exists`, which counts such a file as absent; tests point
  `DEFAULT_ENV_FILE`/`LEGACY_ENV_FILE` at `tmp_path` (`tests/test_admin_config.py`).
- CI actions: `astral-sh/setup-uv` publishes no major tags since v8 (`@v8`, `@v9`, `@v10` do not exist), so the workflow
  pins the full version (`@v10.3.0`). Check `git ls-remote --tags` for the tag before bumping, and keep Node 24 actions
  (`runs.using: node24` in the action's `action.yml`).
- Agent-native background features (Claude Code `run_in_background`, Codex background commands) end with the agent
  session. A process the agent CLI starts lazily after its first minute (e.g. an LSP server of OpenCode) counts as a
  job too and prevents parking – the thread comment lists it; such a session stays open like before parking existed.
- Marketing and outreach material that names third-party accounts or people (e.g. `research/bekanntmachung-plan.md`)
  belongs under `research/`, which `export-public.sh` excludes; sample posts there must not name internal project
  keys – refer to tasks as "Task #<n>".
- The binding promotion plan is `Marketing/Taskbridge_promo.md` (consolidates the plans of Task #84–#87; internal,
  excluded from the public export). The four earlier plans all sit under `research/`; never link marketing or outreach
  plans from `README.md` or put them in `docs/`, which is exported.
- Privacy: `actors.runtime = local` only says where the agent process runs, not where its model runs – a local Claude Code/Codex agent still sends everything it reads to a cloud API. `actors.model_location` covers that, but only while `privacy_enforce_model_location` is on (decision 2026-10-16). Task attachments are committed to the project repo in Gitea (`app/services/attachments.py`), which cloud agents clone, so confidential files must never be attached. Docs for both (model location, data zones, runner, data store outside Gitea): `docs/data-zones.md`; design record `research/konzept-datenzonen.md`.
- `tasks.merge_commit_sha` records the merged PR's SHA on approval, before brain/changelog follow-up commits; `result_commit_sha` remains the submitted SHA. Gitea's merge endpoint returns no SHA, so `GiteaClient.merge_pull` reads the merged PR and returns its `merge_commit_sha`. A retry can recover an already merged PR after a failed read. Historical done tasks retain NULL (no backfill). Background jobs all run in the app process (APScheduler in `app/scheduler.py`, uvicorn `--workers 1`); new periodic logic should be an idempotent, DB-locked function so it can also be triggered from a systemd timer. Docs for repeatable workflows (templates, runs, output contracts, scheduler with workday logic): `docs/workflows.md`; design record `research/konzept-workflows.md`.
- DB tests: `ready_db` empties `app_settings`, so `privacy_enforce_model_location` is **on** (default) in every DB
  test. The fixture gives the local test agents (`hermes`, `openclaw-agent`, `opencode`) `model_location = local`
  so they keep access to `local_only` projects; tests that need the old rule store `false` themselves. A form test
  that posts *Admin → Settings* must send the checkbox, or the save turns the setting off.
- A new privacy check goes through `zones` (Python: `zones.check`/`may_handle`/`allowed_classes`; SQL:
  `zones.sql_may_handle`/`sql_classes`), never a direct `project.privacy`/`actor.runtime` comparison – the two
  forms must stay equal (`tests/test_model_location.py::test_agent_reviewed_sql_matches_helper`).
- The attachment lock checks the file extension only (original name and the name after `safe_filename`, case-insensitive). It does not look at the bytes. `tbr-admin data scan-attachments` is a report; it does not rewrite Git history.
- Parallel tasks must take the next free Alembic revision. Model location is `0020`; workflow stage 1 is `0021` (revises `0020`). Two scripts with the same revision id branch `heads` and `get_current_head()` fails.
- WF 3 should extend `workflows.engine.render_step(session, run, step)` for parameter/output resolution; WF 2 only accepts literal task texts and pauses on unresolved placeholders. It stores parameters without validation. Activation sets `tasks.data_class` to the step class after `zones.check_class` with the template owner; a looser class than the project's pauses the run with `data_class_forbidden` unless the template owner is the project owner or an admin. Unsupported runner/script/gate steps and unsafe zone boundaries always pause, never auto-release. Template-wide auto-review settings, timers/retries, runner stopping and publishing validation belong to their later stages.
- Rework of a merged workflow task clears its PR/result metadata before resubmission: otherwise `_submit` would reuse the merged PR, and `_approve` could recover its old merge SHA instead of merging the rework. Old PR links remain in task events. Reviewer-agent approvals work through review services; WF 4 must add task-kind-aware API/runner preparation for branchless approval jobs.
- Task-level privacy decisions use the effective class: Python `zones.effective_class(project, task)`, SQL
  `zones.sql_effective_class()` (`COALESCE(tasks.data_class, projects.privacy)`, needs `Task` and `Project` in the
  query). Pass the task to `zones.check`/`members.check_privacy`/`reviews.check` wherever one exists; only project
  membership without a task stays on `projects.privacy`. `zones.sql_may_handle` covers `sealed` too
  (`tests/test_data_class.py::test_sql_rule_matches_helper_for_all_classes`).
- The task edit form sends `data_class=inherit` to clear the own class; empty keeps it (FastAPI empty-form rule).
- Alembic: data class per task is `0022` (revises `0021`); parallel tasks take the next free number.
- The placeholder resolver is strict: an empty string does not fill `{{...}}` unless the placeholder uses `default("...")`. `\{{` is the only escape. Decimal outputs are JSON strings. A file output rendered without a filter becomes `path@` plus the first 7 characters of the commit SHA. `*` in `path_glob` does not cross `/`.
- Approval steps pass `no_assignee_rule` when `reviewer_id` (or `reviewer: human:owner` in a definition dict) is set. The content-pipeline example assigns the project owner as reviewer, not as assignee.
- `allow_auto_review` and `data_enabled` are arguments of `check_publish`, not columns. Data zones are off until the caller passes `data_enabled=True`, so `runner` / `gate` / `script` fail `runner_without_data_zones` by default. Input bindings are resolved and returned to the run engine; `task_data_refs` does not exist yet, so nothing is inserted there.
- `smtplib.SMTPException` derives from `OSError`: catch order matters, and "is it a connection problem" must test
  `SMTPServerDisconnected` or "not an `SMTPException`" (`mail.Connection.send`), otherwise every 4xx/5xx counts as a
  lost connection.
- Email tests use `tests/smtp_fake.FakeSmtp` (real sockets on 127.0.0.1, STARTTLS or implicit TLS with a self-signed
  certificate, AUTH PLAIN, `fail_rcpt`/`fail_data` replies, `stop()` = refused connection) and patch
  `mail.ssl_context_factory` with `smtp_fake.client_context`. Parsed bodies use CRLF line endings.
- `tests/conftest.py:ready_db` also empties `email_verify_codes` and resets `email_verified_at`;
  `test_schema_options_and_seed` counts 27 tables.
- Alembic: email channel is `0023` (revises `0022`); parallel tasks take the next free number.
- Admin → Settings is built from `settings_store.GROUPS` (`SettingGroup`: key, title, hint, rows of one or two keys,
  optional `toggle` bool for the "off" pill); `SettingDef` has no `group` field any more and the API's `group` is the
  group key (`GROUP_OF`). A new setting must go into exactly one row of one group –
  `tests/test_settings_groups.py` fails otherwise. Cards are `id="settings-<key>"`; `static/settings.js` shows one
  group per URL hash, counts unsaved changes and drives the secret fields; leaving is guarded by `form_guard.js`
  (`data-guard="settings"`). The page gets secrets through `admin_settings.browser_view` (value always `None`, only
  `is_set`/`source`); the API keeps the masked `…abcd` suffix. Value checks for mail settings are
  `settings_store.MAIL_CHECKS` next to `SFTP_CHECKS`.
- The dataset upload must not use `request.form()` or `csrf_protect`: Starlette spools larger files to a temporary
  file (plaintext on disk). `data_ui.upload` parses `request.stream()` with `python_multipart.MultipartParser` and
  checks the CSRF token and the upload right when the file part begins – the form keeps the file input last.
- `data_audit` refuses DELETE through a trigger: `ready_db` uses `TRUNCATE TABLE data_audit` and resets
  `data_audit_head`. Tests that drop a trigger must recreate it (`tests/test_data_store.py`).
- A streamed query (`yield_per`) must be consumed before the next query on the same session – `data_audit.verify`
  reads the head first.
- Dataset ids are not reset between DB tests (DELETE keeps AUTO_INCREMENT): take the key from the upload redirect,
  never assume `DS-0001`.
- Alembic: the data store is `0024` (revises `0023`, email channel); parallel tasks take the next free number.
- `tests/gitea_fake.FakeGitea` serves binary branch files (`put_branch_file` with bytes) and files at a commit SHA
  (`commit_files[sha][path]`, read with `?ref=<sha>`); result-mail tests use both.
- Alembic: result mails are `0025` (revises `0024`, data store); parallel tasks take the next free number.
- DB tests that read rows the web UI just committed need `s.commit()` **and** `s.expire_all()` (the session factory
  uses `expire_on_commit=False`, and MariaDB's repeatable read keeps the old snapshot). Engine helpers such as
  `workflows.engine._latest` lock run steps `FOR UPDATE`; calling them from the test session before a UI control
  request causes a lock wait timeout – look tasks up without locking (`tests/test_workflows_ui.py:first_task`).
- systemd job unit (`deploy/systemd/taskbrigade-job@.service`, checked with systemd 255): `RuntimeMaxSec=` has **no
  effect on `Type=oneshot`** – the limit is `TimeoutStartSec=` (result `timeout`). `StandardOutput=truncate:` is
  applied again when `ExecStopPost` starts and empties the log; use `append:` on files the runner pre-creates
  (owner `tbr-runner`, group `tbr-job`, 0660). Seccomp kills with SIGSYS unless `SystemCallErrorNumber=EPERM` is set.
  The status for the runner comes from `ExecStopPost` (`$SERVICE_RESULT`, `$EXIT_CODE`, `$EXIT_STATUS` – written as
  `$$` in the unit – and the cgroup's `memory.peak`); it runs after the job's processes are killed, and the runner
  also requires `systemctl start` to succeed before it believes `success`. Files from the job are read with
  `O_NOFOLLOW` + `S_ISREG` (a symlink in `out/` must not make the runner copy the KEK into quarantine).
- Sandbox isolation tests: `deploy/sandbox-test/run.sh` (Docker, privileged systemd container, `--cgroupns=private`,
  `--tmpfs /run`) – about 2 minutes, exit 0 only if every probe passes. `tbr-admin runner selftest` runs the same
  probes on a real host as `tbr-runner`; `python3 -m app.runner.selftest` works from a checkout without the app venv
  (stdlib only). `/tmp`, `/var/tmp` and `/dev/shm` are writable inside the sandbox but private to the job.
- Alembic: the runner is `0028` (revises `0027`, workflow errors); parallel tasks take the next free number.
  `tests/conftest.py:ready_db` empties `run_results`; `test_schema_options_and_seed` counts 37 tables.
- Tests of the runner flow (`tests/test_runner_jobs.py`) drive `app.runner.service.Runner` against the in-process app
  (`ApiAdapter` with the `cli.client.Client` interface) and replace the systemd launcher by a subprocess – the server
  texts are German there (`UI_LANGUAGE=de`).
- `AllowTcpForwarding no` (sshd) only refuses **remote** (`-R`) forwards; a local `-L` forward is a client-side
  channel the server never sees. Do not use `-L` as a test for "forwarding is off" – check `-R` instead.
- `authorized_keys` must be root-owned (`root:root 0644`) inside the chroot root (`root:root 0755`, not
  group-writable), otherwise `sshd` refuses `StrictModes`. The `upload/` folder is owned by the deliverer
  (`<user>:tbr-data 0730`), so the app can read the delivered file through the data group and the deliverer never
  sees another user's files.
- Ubuntu's `install` does **not** accept `-o user:group`; use `install -d -o <user> -g <group>` (the existing
  `tm_ensure_dir` splits the owner string correctly – pass `user:group` to it, not to `install`).
- Planning board query count: `tests/test_planning.py::test_dependency_hint_and_constant_queries` counts statements
  with a `before_cursor_execute` listener on `db.engine`. `tasks._names` skips its query when no task is assigned,
  so both measured boards need at least one assignee; dependency hints come from one `task_dependencies` join
  (`planning_ui.waiting_for`). Tests that expect a sprint to appear on the board must name the filter it is shown
  under (`filter=planned|active|closed`); the default is `all` with closed hidden.
- Alembic: task data refs are `0034` (revises `0033`); parallel tasks take the next free number.
  `tests/conftest.py:ready_db` empties `task_data_refs`; `test_schema_options_and_seed` counts 42 tables.
- `cli/locales/de.json` is written with `json.dumps(..., ensure_ascii=False, indent=2)` plus trailing newline (not
  `indent=1` as noted above); the wrong indent rewrites the whole file.
- Every released output-gate result now gets an "Interpret result" draft as follow-up of its run task (also for run
  tasks created by hand); tests that count `follow_ups` of a run task must expect it.
- Pico styles every `[role=group]` as a full-width joined button bar (children `flex: 1`, inner radii 0) and every
  `nav` as `justify-content: space-between`; `ui.css` undoes it for `.ui-seg`/`.ui-chips`/`.ui-tabs`. Pico also adds
  a search icon to `input[type=search]` (`background-image`) and transitions button backgrounds (~.2 s) – a
  screenshot right after a click can still show the old segment as active.
- Project page tabs (`project_detail.html`, `static/project_detail.js`) are panels on one page selected by the URL hash;
  a hash of an element inside a panel (`#sprints`, `#mail-recipients`) selects that panel. Without JS all panels show.
  Its settings form uses `form_guard.js` (`data-guard="project"`); `[data-dirty-indicator]` inside any guarded form is
  shown while it has unsaved input. Page styles live in `static/project_detail.css` (`pd-*`) until the shared
  components of the UI redesign exist.
- Kanban route keyword clash: `app/web/tasks_ui.py::kanban` must not pass a value that `_view_ctx` already
  returns in `**vctx`. Passing `view=view` next to `**vctx` (which holds `"view"`) raises
  `TypeError: render() got multiple values for keyword argument 'view'` and breaks every `-m db` test that opens
  `/kanban` (`test_i18n.py::test_pages_in_english_with_db`, `test_mobile_ui.py::test_kanban_status_tabs_and_col_parameter`,
  `test_sprints_ui.py::test_sprint_filter_in_backlog_kanban_inbox`). Let `view` come from `**vctx` only, like
  `workflow`/`owner`.
- Sprint migrations are numbered by hand in parallel sprint tasks. Two waves merged migration `0039` twice
  (`0039_sprint_moved_tasks.py` and `0039_notification_read.py`), which left
  `main` with two alembic heads ("Multiple head revisions are present") and every `-m db` test erroring at setup.
  Renumber a colliding revision (here `0039_notification_read.py` → `0040_notification_read.py`, `down_revision`
  `0039`) so a single head remains – check `uv run alembic heads` before submitting.
- Flex rows that switch to `flex-direction: column` on phones keep `align-items: flex-start` and their `flex-basis`
  (e.g. `flex: 1 1 21rem`), which then shrink cards to their content width and turn the basis into a height. Set
  `align-items: stretch` and `flex: none` on the children (task detail `.detail-grid`, inbox `.inbox-cols`).
- Chromium keeps a date input as `document.activeElement` while focus sits in its internal day/month/year fields,
  where neither `:focus` nor `:focus-visible` matches; the focus check skips such elements. Content of a closed
  `<details>` still reports a box from `getBoundingClientRect` – layout checks must ignore it.
- Pico styles `:where(input, select, textarea) + small` as a full-width block (form help text): a `<small>` directly
  after a toolbar select wraps onto its own line – give it `width: auto; margin: 0` (Users & agents count).
  Pico also gives `<details>` a bottom margin, which pushes inline popovers (`.prop-pop`) below their row label.
- `ui.css` loads after `app.css`: a page rule with the same specificity as a `ui-*` component rule loses, also inside
  a media query (`nav.detail-tabs` is needed to beat `.ui-tabs { flex-wrap: wrap }` on phones).
- Ellipsis needs an element of its own: `text-overflow` does not apply to the anonymous text of an `inline-flex`
  chip (`.agent`, `.actor-chip`, `.ui-actor`), so the name sits in `<span class="ui-actor-name">` and the chip gets
  `min-width: 0; max-width: 100%`. In an auto-layout table (backlog) cap the chip itself – `max-width` on a `td` is
  ignored.
- Header and profile language choices include Default (EN/DE), which deletes `tm_lang`. Active entries use the normalized cookie, so an explicit choice equal to the admin default is still personal. Settings explain the override. Saving clears the local default cache; other workers may retain it for up to 30 seconds (see `docs/getting-started.md`).
- Phone/tablet CSS: `app.css` below 960 px sets `:is(button, [role=button]):not(.menu-toggle) { display: inline-flex }`
  (specificity 0,1,1) – a plain `.class { display: none }` on a button loses against it on tablets; hide with two
  classes. A fixed sheet inside an element with `position: relative; z-index: 1` (kanban `.card-move`) stays in that
  stacking context and ends up under the bottom tab bar (z-index 50): raise the open container, and park phone
  sheets and sticky bars above the tab bar (`bottom: calc(66px + env(safe-area-inset-bottom))`). Flex tabs drop the
  space between a label and its count span – use `gap`.
- Full-page Playwright shots resize the viewport, which moves a mandatory snap-scroller (kanban on phones) to a varying
  offset; `_shoot` therefore scrolls `.kanban-scroll` back to 0 first – baselines show the first column, the real start
  column is asserted in `test_ui_mobile_flows.py`.
- Header controls between 640 and 959 px get `min-height: 44px` from `app.css` (touch sizes); a fixed-height
  header control (quick capture) needs its own 44 px rule there, otherwise text drawn over it (`.qc-project-key`)
  ends up off-centre.
- Segment controls built from links (`.ui-seg > a`, e.g. the profile language switch, which is a GET navigation) do
  not get the phone touch floor `min-width: 44px` from `app.css` (that rule covers `button`, not `a`); add
  `min-width: 44px` next to the segment `height: 44px` in `ui.css`'s phone block or the short "EN"/"DE" segments
  fall under the 44 px tap-target check.
- Jinja newstyle `_()` returns markup-safe text: `&` in a translated string reaches the HTML unescaped
  (`Benutzer & Agenten`, not `&amp;`) – assert the raw text in UI tests.
- Screenshot seed (`deploy/release/screenshot_seed.py`): a workflow step with a fixed `assignee` pauses the run with
  `no_assignee` when the agent is at its parallel limit – every open task assigned to it counts, also `defined` and
  `draft` ones. The seed raises `research-agent` to `max_parallel_tasks = 3` before starting the demo run. Fixed
  actors must also be project members (`members.ensure_member`), approvals' human reviewers included.
- `api.import_draft` takes the parsed document (`transfer.parse_yaml(text)`), not the YAML string; a string fails
  with “Unknown field(s) in the document” listing single characters.
- The README's minimal workflow YAML is the seed's `RELEASE_NOTES_YAML`: change both together, so the screenshot run
  proves that the documented example still passes the publish checks.
- `screenshots.py` crops only full-page shots at `MAX_HEIGHT`; phone viewport shots are 780×1688 px and must keep the
  fixed bottom tab bar. Token prefixes (`tm_…`) are replaced by `tm_xxxxx` before every shot.

## Glossary

- **Actor** – a human or an agent account; agents authenticate with an API token.
- **Owner** – the human responsible for a project (reviews, answers questions).
- **Runner** – `tm run`, started by a systemd timer, launches a coding agent for a claimed task.
- **brain-changes** – per-task change file for `brain.md`, applied by Taskmeister on approval.

## Naming research

## Known pitfalls

- Workflow transports live in `app/api/workflows.py`, `app/services/workflows/api.py`, `cli/workflows.py` and `cli/workflow_mcp.py` (guide: `docs/workflows-api.md`). Draft PUT creates a template if needed; publishing freezes that version and later saves replace/create only a draft. Reads require project membership or human ownership/admin; outputs, defaults, traces and parameters are masked through `zones`.
- Workflow engine activation and dry-run share `workflows.rendering`: typed parameters/defaults, the WF 3 placeholder resolver and input-binding checks. Preview never adds its transient run or reserves IDs/sequences; `<new-run>` marks future IDs and later outputs stay deferred. Dataset parameters fail closed until dataset access validation exists. Scheduler tick/operator commands explicitly report unavailable until WF 6; schedules are empty in stage 1.
- Approval review jobs (`task_kind=approval`) prepare only `REVIEW.md` and use the review lease/decision lifecycle, without Git credentials, a checkout or PR. When credentials are missing, preparation first confirms a matching approval in `/agents/me/reviews` and claims that exact task; ordinary code tasks still fail before claim. The runner uses `APPROVAL_PROMPT` for these jobs.
- Gitea data guards are opt-in filesystem installs (`tbr-admin data gitea-hook install --repo-root <Gitea repository ROOT>`); no production installation is automatic. Selection includes local-only projects, projects with explicit local-only/sealed tasks, and projects with datasets, independent of `data_enabled`. Rerun installation after project/task/dataset/repository changes and app upgrades; selection is an install-time snapshot. Uninstall includes all mapped repositories so former data-zone projects can be cleaned up; remove guards before deleting/remapping projects if needed. See `docs/operations.md`.
- The standalone Python 3 guard (`app/services/hooks/pre_receive.py`, rendered by `app/services/gitea_hook.py`) shares `attachments.BLOCKED_EXTENSIONS`. It checks whole commit trees, including incoming history and peeled commit tags, preserves Git quarantine variables and fails closed on inspection errors/non-commit tags. Extension checks are not content detection; no historical cleanup occurs. Gitea 1.24 dispatches executable `hooks/pre-receive.d/*`; install only `taskbrigade-data-guard`, never replace Gitea's dispatcher or `gitea` file.
- Data zones are provisioned by `deploy/install.sh`/`deploy/update.sh` only with `TBR_DATA_ZONES=1` (concept
  docs/konzept-datenzonen.md §9); without it the scripts do not touch the system and only the database migrations
  add the new tables/columns. The switch from the environment wins over a key set earlier with
  `tbr-admin config set TBR_DATA_ZONES 1` (update.sh reads the key only when the variable is unset). `prereq.sh`
  checks systemd ≥ 249 and polkit only while the switch is on.
- The data store KEK (`TBR_DATA_KEY_FILE`, default `/etc/taskbrigade/data.key`, `root:tbr-data 0640`) is created by
  the install scripts (or `tbr-admin init-data-key`) and **never** overwritten; `deploy/backup.sh` archives the
  encrypted store only with `TBR_BACKUP_DATA=1` and never the KEK – without the key a leaked backup plus store is
  harmless, with it everything opens. `taskbrigade.service` gets the data group and `ReadWritePaths=$TBR_DATA_DIR`
  through the drop-in `/etc/systemd/system/taskbrigade.service.d/50-data-zones.conf`.
- `tbr-admin config` knows four keys the app does not read: `TBR_DATA_ZONES`, `TBR_DATA_SFTP`, `TBR_RUNNER_VENV`,
  `TBR_BACKUP_DATA` (all in `EXTRA_KEYS`, checked as 0/1 or absolute path). They belong in `.env.example` too
  (`tests/test_deploy_prereq.py` checks `ENV_KEYS`).
- The Gitea push guard and the data-zone runner sandbox are separate files (`deploy/polkit/…`,
  `deploy/systemd/taskbrigade-job@.service`); the polkit rule lets `tbr-runner` start/stop only `taskbrigade-job@*`
  units.
- The `tbr doctor` email check is a **probe, not a test mail**: `mail.smtp_probe` opens one SMTP session
  (EHLO + STARTTLS or implicit TLS + login) and closes it – no mail leaves the server and no address is
  needed. `NOOP` alone would not exercise TLS or the login. Admin → Settings keeps its real
  "send test mail" button.
- The doctor email check **warns, it does not fail**: recurring failed deliveries (last `mail.LOG_WINDOW`
  = 10 log rows) produce a warning line but exit 0 – the TaskBrigade server itself is still healthy. The
  check only errors when the probe endpoint is unavailable (`GET /agents/me/email-status`).
- `smtp_probe` raises 404 `email_disabled` when the channel is **off** (`mail.enabled()` = switch
  `email_enabled` plus host and from-address set); without the switch the check is unreachable, so
  `tbr doctor` simply omits the email line then.
- A failing probe (connect, TLS, login) answers **404 vs 502**: channel off → 404 `email_disabled`;
  the SMTP server failing → 502 `bad_gateway` with the plain MailError text (`errors.ProbeFailed`).
  Never let a `MailError` escape `smtp_probe` – it is no `AppError` and would end as a generic 500.
- `failures_in_a_row` counts the failed/retrying rows at the **end of the delivery log** – a successful
  probe does not clear it; the warning in `tbr doctor` stays until new deliveries succeed or the rows
  age out of the log.
- The probe endpoint answer contains **no credentials**, only host, security mode, last delivery time and
  status/error from the log (no message contents; the log keeps 30 days).
- The SMTP probe opens a fresh connection on every doctor run (no caching) – fine at doctor frequency,
  but mind the provider's connection rate limit if doctor or the Settings page is polled aggressively.
- Workflow scheduling uses `workflows.tick.tick(session, now)` and `workflows.schedule` for all date calculations. Tick owns commits; pass a clean session. Its MariaDB named lock stays on a separate connection across per-slot commits. Both runs and fires enforce unique schedule/UTC-slot keys. Migration `0026` follows `0025`; parallel tasks take the next free revision. The schema test now counts 36 tables (excluding alembic_version).
- Queued runs pin their version, resolved parameters and `schedule_timezone` at creation; `started_by` is NULL for schedule runs. Paused runs count as active for overlap. Manual starts need `queue=True` to enqueue or human-admin `force_parallel=True` to bypass an active run. Existing tests that deliberately start concurrently must choose explicitly.
- WF 6 exposes `/workflows/schedules` and `/workflows/{project}/{key}/schedules`; WF 5 can link to these from its template page. Shared calendars and exceptions are managed by `tbr-admin workflows calendar add/day/check`. Calendar exception changes reset affected schedule cursors to now. Cron weekday numbers follow CronTrigger (Monday=0); use weekday names.
- `TBR_WORKFLOW_TICK=timer|app|off` selects the app trigger (Docker image: app, native: timer); workflows_enabled stays false by default pending WF 7 installation onboarding. WF 7 owns systemd units/install/update/doctor. WF 8 owns timeouts/retries/reminders; WF 6 reconciles terminal tasks and activates queued runs only.
- Workflow error handling is `workflows.errors` (guide `docs/workflow-errors.md`), migration `0027` after `0026`. Work clocks count only `in_progress`, default agent/script limits 24/48 hours; clocks and reminder timestamps persist on run-step attempts. Existing active clocks begin at migration time. The tick must include paused runs for running-task timeouts, while retries/reminders defer during manual pauses.
- Agent automatic retry reuses the task via `reset_for_reassignment`, but creates a fresh run-step attempt and moves `tasks.workflow_run_step_id`. Flush the step-to-task link before setting the reverse relationship to avoid SQLAlchemy circular-dependency errors. `assignee_id` on attempts preserves retry candidate history. Failed attempts are excluded from task claims, including named/direct claims, until recovery; cancelling a task during backoff removes its pending retry.
- Workflow reminders use `workflow_wait_reminder` (owner/starter, first after step `wait_reminder_hours` default 24, then daily) and `workflow_claim_reminder` (owner, once per defined interval after `claim_reminder_hours` default 4). `workflow_paused_max_days` default 30 only adds a history/detail warning, never automatically terminates a human wait. Step config `reassign_on_idle: true` tries one eligible alternative without incrementing the attempt. Runner activation/stopping and exit-code/schema mapping remain with WF 10/11; recovery creates a fresh runner attempt through normal activation.
- Migration `0030` follows `0029` and adds `dataset_profiles`; schema tests count 40 tables. Until DZ 9 adds `task_data_refs`, `dataset_profiles.code_task_id` restricts released-profile API reads to the assigned agent of that active code task. The approved schema/sample are inline in the draft description; original names remain local in `aliases_json` and runner-only job specifications.
- Profile samples exclude all nonempty original cells (normalizing numeric/date representations), including permitted category values. This deliberately strengthens the concept's category sampling rule. Fingerprints ignore distribution/PII role changes and include ordered names/types/precision; aliases change the fingerprint. Header aliases are applied inside the sandbox to a private temporary input copy, never by the web parser.
- Profile release enforces a second human for sealed uploads (uploader cannot release). It uses the output gate's effective setting `output_gate.four_eyes()` (`projects.data_four_eyes`, NULL = admin
  setting `data_four_eyes`, default on) – never read the column directly, NULL would count as "off"; local-only profiles allow the uploader to release. Alias mappings use MEDIUMTEXT for wide workbooks. Cloud task descriptions inline at most 60,000 bytes; larger handoffs point to the same scoped profile API.
- Alembic: the output gate is `0032` (revises `0031`, task usage); parallel tasks take the next free number.
- Runner tests now see two launcher runs per successful job (`<job>` and `<job>-gate`); a fake launcher must run the
  probe's `script/entry.py` like any job or return a failure (then the gate fails checks 4/5 – fail closed).
- The default runner test dataset (`tests/test_data_store.CSV`) has two rows per account; `renv` uploads it with
  *Min. group size* 2, otherwise the output gate refuses every per-account result with k = 5.
- `runner_jobs.record_result`: `profile` jobs return after `dataset_profiles.ingest` and never reach the output gate
  checks (their result stays `quarantine`); only `run` jobs are checked.
- Email notify steps (`workflows.email`, `config.channel=email`) file result-mail requests on the latest preceding task; notify has no task of its own. `workflow_run_steps.output_json.mail_id` points to the request. Step success means the request was accepted, not SMTP delivery; approval and delivery remain in the task's Emails section/Inbox.
- `result:<id>` email attachments export only `run_results.released_json` as Markdown with JSON; never quarantine output or logs. Local-only project/task/notify mail requires only released result attachments and separate human approval (`on_approve` is disabled), sealed mail is refused. Migration `0033` after `0032` stores the request class on outgoing_mails; approval/delivery use the stricter stored/current class and recheck result status and hashes.
- WF 10 maps runner/gate steps through `workflows.data_steps` (guide `docs/workflow-engine.md`). A runner attempt succeeds when the job passes checks, while its task stays `for_review`; the following taskless gate watches that same result. Task transitions must reach the hook even after the runner attempt succeeded. Gates require human output-gate release; rejection/keeping sealed pauses. Without a gate, `keep_sealed_by_system` completes the task without releasing any data. Profile runner steps wait for the separate profile release. Script pins/repair remain WF 11.
- Workflow zone checks inspect actual placeholder reads, including config and bindings, using the strictest recorded/current producer class. Sealed summaries/values never cross; task-number/status references need no gate, released profiles use profile approval, and results need a directly following succeeded gate plus a still-released result. Dataset parameters are project-scoped numeric IDs validated with the template owner's data rights. Runner bindings currently populate the existing run fields; DZ 9 owns `task_data_refs`.
- Workflow agent consumers receive only released profile/schema samples or released aggregate JSON inline (60,000-byte limit, otherwise API links). Engine-written `input_profiles`/`input_results` IDs on the consumer attempt scope large profile reads to the assigned active task until DZ 9 adds shared reference rows. Never materialize `aliases_json`, quarantine artifacts or runner logs.
- Runner heartbeats stop the active sandbox (including the gate probe) when the task is cancelled, restarted or assigned elsewhere. Manual workflow pauses leave jobs running and defer successors. Workflow `timeout_minutes` can shorten the admin sandbox timeout; transient execution retries always create a new runner task/job/result.
- WF 11 script steps live in `workflows.scripts` (guide `docs/workflow-scripts.md`), migration `0035` after `0034`; schema tests count 43 tables. Pins are template/key/path scoped, record the human-reviewed merge SHA and profile ID, and survive matching template versions. Every engine mutation locks template before run to serialize pins and cross-run repair checks.
- A profile runner preceding its configured script step can complete locally without releasing its generated profile, provided no intervening consumer needs the profile contents. Matching pins skip the code task; mismatches/repairs wait for ordinary human profile release. Approved aliases are reused only against the exact original headers plus matching aliased fingerprint. The runner uses the run's precise pinned profile/aliases, not an unrelated latest release. Workflow profile release must not create an ad-hoc draft as well.
- Exit 3 and failed output checks restart at the producing script with fresh downstream attempts, preserving prior outputs. At most one open repair per pin and `config.max_repairs_per_run` automatic repairs (default 1); only explicit human manual repair bypasses the latter. Repair tasks carry only released schemas/samples and numbered check statuses. Completing a profile internally does not release any data; later explicit profile decisions still wake its waiting workflow.
- Workflow YAML transport is `app/services/workflows/transfer.py` (guide `docs/workflows-api.md → Export and import`). The canonical export writes only fields with a value and is stable (re-export is byte-identical); import accepts a subset and always stores an **unpublished** draft via `workflows.api.import_draft` (never publishes). `pyyaml` is an explicit dependency now. The `draft_workflow` MCP tool and `tbr workflow export|import` are thin clients of the export/import endpoints; the UI upload posts `multipart/form-data` (`action=save|publish`) to `/workflows/{project}/{key}/import`.
- Import checks reuse `workflows.publish.check_publish` with `role_candidates` built from a transient (never added) `WorkflowRun`/`WorkflowStep` probe, like `api.publish`; blocking issues raise `workflow_import_invalid` and nothing is stored. `assignee`/`reviewer` in a document are actor names or ids (no `human:owner` resolution at import time – use an id/name).
- The canonical export omits a field only when it equals the editor/API default (`StepDraft`, `app/api/workflows.py`): `retry_backoff_s` default 300, `max_retries` default 2 for `runner` steps and 0 otherwise. A non-default value – including `retry_backoff_s=0` and a runner's `max_retries=0` – is always written, so the round trip is lossless (`transfer.DEFAULT_RETRY_BACKOFF_S`, `transfer.DEFAULT_MAX_RETRIES`). Boolean document fields (`optional`, `required`) are read through `transfer._boolean`, which refuses non-boolean values instead of coercing by truthiness (`optional: no` is an import error, not `True`).
- Compose: every fixed-name volume the app mounts must be renamed in the smoke-test and screenshot overrides
  (`deploy/release/screenshots.py:DATA_VOLUMES`), otherwise their `down -v` deletes the real stack's data or key –
  `tests/test_docker_runner.py::test_test_stacks_rename_every_app_volume` checks it. Their runner network uses
  `172.31.253.0/24`; new subnets also go into the IP allow list of `deploy/release/export-public.sh` and
  `tests/test_deploy_prereq.py`.
- Containers without capabilities: root without `CAP_FSETID` silently loses the set-group-id bit on `chmod` of a
  directory whose group it is not in (SFTP container needs `FSETID`); root without `CAP_DAC_OVERRIDE` cannot read a
  0640 file of another user (the SFTP spool is 0644). sshd with `UsePAM no` refuses accounts with a locked (`!`)
  password even for keys – `useradd --password '*'`.
- Job containers under `network_mode: none` with Docker's default seccomp profile may create `AF_INET`/`AF_NETLINK`
  sockets (only the empty own namespace); the self-test's network probe checks "loopback only" there instead of
  refused families (`selftest.probes(docker=True)`). Exit codes above 128 are reported as `signal`.
- `docker compose down` of a kept test stack needs the same required variables (`TBR_DB_PASSWORD`, …) as `up`;
  without them it fails silently in scripts and the next run meets the old MariaDB volume (unhealthy app).
- Claims only take tasks of `active` projects, and an `active` project needs a Gitea repository – throwaway e2e
  stacks without Gitea create the project as `idea` and set the status directly (`deploy/docker/data-zones-e2e.py`).
- Workflow event triggers are delivered once. Clearing `processed_at` does not start another run for the same
  trigger, and editing a trigger does not replay events that were already processed. A paused template does
  not fire, but the tick still marks the event processed. `workflow_failed` means the run was paused by a
  final step failure, not that its status column is `failed`.
- DZ 13 output policies live in `gate_privacy` and `datasets.gate_policy_json` (migration `0038` after `0037`). Only human owners/admins change them through the CSRF-protected dataset page. Noise is off and limits unlimited by default; blank limits mean unlimited, zero blocks release. Positive epsilon/sensitivity are paired; epsilon limits require noise. Decimal parameters have six places. Sensitivity is a human-reviewed **whole-output L1 bound under add/remove-one-person adjacency**, after rounding/helper removal, not an observed-data estimate or per-cell bound. See `docs/data-store.md → Noise and query budgets` for contribution bounds, fixed public labels/shape and the limits of the guarantee.
- `output_gate.decide` locks result/task, then `gate_privacy.release` locks and refreshes the dataset row before charging persistent `released_count`/`epsilon_spent`. No independent commits: artifact, usage, audit and task transition roll back together. Policy changes and artifact deletion never reset counters. Existing releases are counted on upgrade; old exact releases have no epsilon charge. Accounting is per dataset ID, not across duplicate uploads/versions.
- Noise is OS-random four-uniform Laplace at scale sensitivity/epsilon over all numeric cells (including counts and metrics), rounded to cents. Helper columns never leave. The release requires confirmation and the current policy token; `released_json` is sampled once and reused by all downstream readers. `release_policy_json` preserves parameters/usage on the result and in the audit. Earlier noisy counts cannot support the differencing heuristic: check 8 warns instead of treating them as exact history. Gate checks, labels, profiles and raw access are outside the noise mechanism; do not claim whole-system differential privacy.
- Workflow role bindings (`workflow_role_bindings`) have no UI, API or CLI; a step with `agent_role` pauses with
  `no_assignee` until an operator inserts rows (project, role, actor, rank). Docs say so in `docs/workflows.md`.
- Gaps of the data zones against the concept (documented in `docs/data-zones.md`): no automatic expiry of
  quarantined run outputs (`data_quarantine_days`), no warning for CSV-like text in `cloud_ok` descriptions, no KEK
  rotation.
- Backlog HTMX updates target `backlog-content` so filter counts, status chips and row versions refresh together. `status_set=1` distinguishes an explicitly empty status selection from the default statuses; older `all=1` links still work. Bulk edits use `POST /backlog/bulk` with task numbers and row versions, one transaction per task, and ordinary task update/transition rules without implicit overrides.
- Admin users overview uses `?detail=<actor>` for its side panel; existing full forms remain at `/admin/users/<actor>` for advanced settings. Panel POSTs use `?panel=1`; the compact `/summary` endpoint changes only role, runtime, owner and parallel-task limit. API tokens cannot be recovered from their hashes: panel copying is available only immediately after rotation, in a masked password field with a `no-store` response. Revoke clears token metadata without deactivating the actor.
- Workflows page start templates live in `app/services/workflows/starters.py` (plain data for `api.Draft`, role-based agent/approval/notify steps only, so they publish without data zones; `tests/test_workflows_redesign.py` runs `check_publish` on each). `/workflows/new?starter=<id>` prefills the form and the hidden `starter` field makes `POST /workflows/new` store the example steps; schedules and triggers are not created, the starter's `next_step` hint is shown after creation. Tab counts come from `workflows.ui.tab_counts`, passed as `wf_counts` to the pages that include `_workflow_tabs.html` (pages without it show no counts).
- The Projects ownership notice uses `owner_hint`, computed from ownership across all projects before status/search filters. `own_only` is only the display preference and does not mean the user owns no projects. Progress tracks exclude cancelled tasks, remain empty without tasks and use green/amber/navy for done/review/in progress.
- `.task-title` at 24px belongs only on the task-page `h1`. Backlog and other tables reuse the class on the title cell; a font-size on the bare class makes those titles 24px. List task titles (Backlog link, Planning `.plan-title`, Kanban `.card-title`) take their font from the shared `.ui-task-title` in `ui.css` (14px/600, line-height 1.35); `.card-title` alone only sets the margin, and the 15px inbox card title is scoped to `.action-card .card-title`.
- Inbox notices default to unread deliveries; `?notices=all` includes read history. `POST /inbox/notices/read` takes explicit IDs or `all` with the rendered UTC `until` and sprint filter; `/inbox/notices/unread` takes IDs. The rendered `max_id` ceiling additionally protects same-second arrivals despite MariaDB timestamp precision. Both use UI CSRF/origin protection and scope every mutation to the signed-in actor. Read responses return only changed IDs for undo. Simultaneous copies across notification channels share one feed item with multiple IDs; repeated events at different times remain separate.
- No automatic hyphenation: `app.css` sets `html { hyphens: manual }` and `tests/test_ui_kit.py` fails on any `hyphens: auto` in `app/static/*.css` (it split "Gesamtbericht"/"tablet" on cards). Long words use `overflow-wrap: anywhere`. Rendered Markdown headings (`.md h1`–`h6`) are 16/14px so they stay below the 24px page title.
- Pico draws a chevron as `summary::after` on every `<details>`; icon-only summaries (kanban card "⋯" menu, planning
  row menu) need `summary::after { content: none }`, otherwise a stray "ˎ" appears next to the icon.
- Kanban on phones opens on the first column (`KANBAN_DEFAULT_COLUMN` = first key of `KANBAN_COLUMNS`) unless `?col=`
  names one; `?col=` beats the stored scroll position, and `static/kanban.js` keeps the filter form's hidden `col`
  on the column in view. Tests that cut the done column out of the HTML must match `class="kanban-col"
  data-column="done"` – the phone status tab carries `data-column="done"` too and comes first.
- Inbox notice origin checks use browser-controlled `Sec-Fetch-Site: same-origin` when present, alongside the mandatory session CSRF token. Tailscale terminates HTTPS while the backend deliberately uses `--no-proxy-headers`, so comparing the browser's HTTPS Origin with the backend's HTTP base URL rejects valid requests. Other Fetch Metadata site values are refused; clients without Fetch Metadata retain the strict Origin comparison. Regression tests use real password login cookies and rendered CSRF headers for both admin and member actors.
- Pico gives `section`, `li` and `details` a bottom margin (≈30px, ≈3px, ≈15px). Inside a flex/grid container with
  its own `gap` (planning cards, task rows, `ui-stack`) these margins add on top of the gap and double the spacing –
  reset them on the component (`margin: 0`, as `.plan-sprint`, `.plan-row`, `.plan-move`, `.ui-stack > section` do)
  instead of shrinking the gap. Measure in the browser (`getComputedStyle(el).marginBottom`); the CSS file alone
  does not show it.
- Workflow overview mutations live in `workflows.actions`: deletion archives under the template lock shared with starts, blocks all nonterminal runs, and disables schedules/triggers. Archived templates are hidden from `visible_templates` by default; `include_archived=True` keeps their run counts available. Duplication copies the latest version via API models rather than the editor form, preserving JSON and enum values containing commas; automation and script pins are not copied.
