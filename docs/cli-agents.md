<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# CLI agents on a second machine (Claude Code, Codex, OpenCode)

The [quickstart](quickstart.md) runs Claude Code on the TaskBrigade server itself. This guide sets up terminal
coding agents on **another Linux or macOS machine** – a workstation or a GPU box with a local model – and lets them
pick up tasks unattended with the runner.
For workflow run commands and previews, see [Workflow API, CLI and MCP](workflows-api.md#cli).

> Tested with all three agents on Ubuntu 24.04 (task → tmux window → commit → `tbr submit` → pull request → done).
> The snags found along the way – the trust dialog swallowing a keystroke, approval prompts for `&&` chains and
> heredocs, `tbr` with an absolute path in OpenCode, the Codex sandbox under AppArmor – are handled by the runner or
> described below.

The guide is written so that Claude Code on the **agent host** can work through it step by step. Steps marked
**⏸ Admin** are done by a human with admin rights (tokens, trust dialogs, sudo): stop there and say so. **Never
paste tokens into a chat.**

## In short

1. **⏸ Admin:** *Admin → Users & agents → New agent* – name, kind, runtime, role, projects. The
   page shows a one-time code and the command to copy.
2. On the agent host, as the agent's user:

   ```bash
   curl -fsSL https://taskbrigade.example.ts.net/install.sh | sh          # only if tbr is missing
   tbr join https://taskbrigade.example.ts.net tmj_… --dir ~/taskbrigade-work/claude-code --setup claude-code
   tbr doctor --work-root ~/taskbrigade-work/claude-code
   ```

That is all: the next timer pass (within 10 minutes) picks up the agent's tasks. `tbr join` fetches the API token,
`--setup` writes the runner configuration, checks tmux and the agent command and installs the timer, `tbr doctor`
checks everything once more. The CLI itself must be installed and logged in once ([step 4](#4-one-time-preparation-of-the-clis));
`tbr setup` names what is missing. The rest of this guide explains the details and the manual way.

## How it works

- Per agent, a **timer** fires every 10 minutes – a systemd user timer on Linux, a launchd agent on macOS (both
  installed by `tbr setup`): `tbr run` calls `tbr prepare`.
  **No task → exit immediately, no LLM call.**
- Before claiming, `tbr run` checks that `tmux` (with `mode = "tmux"`) and the agent command (first element of
  `[runner] command`) are on the `PATH`. If one is missing it prints what to install (macOS: `brew install tmux`,
  Linux: `sudo apt install tmux`; agents: `npm install -g …`), exits with code 1 and claims **nothing**.
- If the start fails after the claim anyway (tmux window, `tbr session` or the agent command), the runner gives the
  task back with the reason – it never stays *in progress* without anyone working on it.
- Own task still *in progress* without a running session (session crashed, machine rebooted): the next `tbr run`
  **resumes** it (like `tbr prepare --resume-own`) before claiming anything new. While its session runs, no second run
  starts: `tbr session` holds `~/taskbrigade-work/<agent>/.locks/session-<TASK>.lock` (flock), and a live tmux window
  named after the task counts as running too. This applies to the systemd timer as well.
- If there is a task, it is claimed, the repository is cloned or updated and a Git worktree is created on the task
  branch (`TASK.md` contains assignment, thread and instructions). The runner then opens a window named after the
  task in the tmux session **`taskbrigade`** and starts the agent there **interactively**. Watch and intervene with
  `tmux attach -t taskbrigade`.
- The agent reports back itself: `tbr submit` (done → pull request), `tbr ask` (question), `tbr abort` (cannot solve
  it). While it runs, the runner extends the lease.
- If the agent exits without reporting back, the runner wraps up: new commits → push and submit, otherwise give the
  task back. The window stays open afterwards for inspection ("Pane is dead").
- Interactive agents stay open after `tbr submit`/`ask`/`abort`. `tbr session` checks the task status every minute;
  once the task has left *in progress* (submitted, question, aborted, released, cancelled or reassigned) it waits
  `stop_after_finish_seconds` and then stops the agent (Ctrl+C, then SIGTERM, then SIGKILL) – without a second
  closing step. The window shows the reason („KET-0007 is for_review – agent stopped“) and counts as finished.
- If a task comes back (e.g. *changes requested*) while its old window is still running, `tbr run` stops that window
  right away (Ctrl+C, then SIGTERM, then SIGKILL) and closes it before starting the new one – the window content is
  appended to the old run log. A task never runs in two windows; a window that fails to start is closed and the
  task given back.
- **Start check:** `start_check_seconds` after the start, `tbr session` reads its own tmux window. A known blocker –
  login expired or not logged in, usage/rate limit – stops the agent, gives the task back with the reason (it
  appears in the thread, e.g. „claude-code: login expired or not logged in – run `claude /login` on <host>“) and
  writes the block file `<work directory>/.agent-blocked`: `tbr run` claims nothing until `tbr doctor` finds the agent
  logged in again and removes the block. If the start prompt is still unsent in the input field, the runner presses
  Enter (at most twice, logged in the window) and checks again; if it stays unsent, the task is given back the same
  way. The patterns live in `BLOCKERS` in `cli/runner.py` (per agent program: `claude`, `codex`, `opencode`).
- **Stall:** if the agent window does not change for `stall_minutes`, the runner writes one comment into the thread
  („No activity in the agent window since … – please check“, with the last lines of the window). The agent keeps
  running. Start check and stall detection need `mode = "tmux"`.
- **Waiting for a background job:** an agent that starts a long job (e.g. `nohup` model test) writes a log file and
  reports `tbr heartbeat <NR> --waiting "<reason>" --for 2h --watch-file <log>` (default 2 h, max. 24 h; repeat it
  before it ends, add progress as a comment, `--done-waiting` ends it). While the waiting state is valid there is no
  stall note, no stall notification, and the runner keeps extending the lease (also beyond `max_session_hours`). The
  task detail and the kanban card show "waiting for a background run: <reason> (until <time>)". With `--watch-file`
  a change of the file (modified time or size) counts as activity; if it stays unchanged for `stall_minutes`, the
  runner writes „Background run is no longer writing: <path>“ with the file's last lines instead of the window. When
  the waiting state expires unextended, normal stall detection resumes. Hermes/OpenClaw and MCP agents use the same
  call (MCP: `report_waiting`).
- **Parked while waiting:** a CLI agent does not have to stay in its session while it waits. After reporting the
  waiting state it ends the run **without** `release` (step 5 in TASK.md); an interactive agent that stays at its
  prompt is stopped once its window has not changed for `park_idle_seconds` (default 120). The runner then parks the
  task: it stays in progress, every `tbr run` pass extends the lease with a plain heartbeat and starts no new
  session, and it is resumed with a new session once the waiting state ends (expired or `--done-waiting`) or the watched log is missing
  or has not changed for `stall_minutes`. `tbr doctor` lists a parked task as waiting, not as hanging.
- **Old windows:** every `tbr run` closes windows whose agent has ended (`pane_dead`) more than
  `close_finished_windows_after_hours` ago (by tmux's `pane_dead_time`, or the run log's last write on tmux versions
  without it). The window content is appended to the run log under `logs/` first. Windows with a running agent and
  the start window `runner` are never closed; cleanup failures only go to `logs/runner.log`.
- Each agent has its own identity in `~/taskbrigade-work/<agent>/.taskbrigade.toml` (API token, plus Git access
  through `tbr git-credential`, valid only in its own repositories).

<!-- Screenshot placeholder – see screenshots/README.md:
![tmux session taskbrigade with an agent working on a task](screenshots/runner-tmux.png)
-->

| TaskBrigade actor | Gitea user | runtime |
|---|---|---|
| `claude-code` | `claude-code` | cloud |
| `codex` | `codex` | cloud |
| `opencode` | `opencode` | local or cloud (step 2) |

The names are examples; any names work. A fresh installation contains only the admin, so **⏸ Admin:** create each
agent under *Admin → Users & agents → New agent*, then create the Gitea users and link each
one to its actor (*Admin → Users & agents → <agent> → Gitea*) – or let TaskBrigade do it with a join code (step 3).

## Network: how the second machine reaches TaskBrigade

TaskBrigade answers the API only on loopback, and through `tailscale serve`, which also arrives on loopback. On the
LAN only the web UI is open (see [operations.md](operations.md)). An agent on another machine therefore uses
**Tailscale**:

1. On the TaskBrigade server: `sudo TAILSCALE_SERVE=1 deploy/install.sh` (or `tailscale serve --bg --https=443
   http://127.0.0.1:8090`), with HTTPS certificates enabled in the Tailscale admin console.
2. Join the agent host to the same tailnet.
3. Agents use `url = "https://taskbrigade.example.ts.net"` (your tailnet name instead).

The agent host also needs to reach Gitea for `git clone`/`git push`; use a Gitea URL that resolves from there
(the examples use `http://gitea.example.internal:3001`).

## 0. Check prerequisites

```bash
tailscale status | grep -i taskbrigade
curl -fsS https://taskbrigade.example.ts.net/healthz          # expected: "db":"ok"
command -v uv git tmux claude codex opencode
claude --version; codex --version; opencode --version
```

`uv` is installed by step 1 if missing. Missing `tmux`: **⏸ Admin** (`sudo apt install tmux`; macOS:
`brew install tmux`). You only need the agent CLIs you actually want to run. `tbr setup` and `tbr doctor` check these
programs again later.

## 1. Install `tbr`

The TaskBrigade server delivers `tbr` in its own version – no Gitea or GitHub access needed on the agent host:

```bash
curl -fsSL https://taskbrigade.example.ts.net/install.sh | sh
tbr version                                                    # tbr and server version
```

The script runs as the agent user (no root) on Linux and macOS. It installs `uv` into `~/.local/bin` if it is
missing (official installer from astral.sh), then `tbr` as a uv tool from the server's wheel
(`/download/taskbrigade-<version>-py3-none-any.whl`; Python 3.12 and the dependencies come from PyPI). Running it
again leaves an installed `tbr` of the same version alone. If `~/.local/bin` is not on your `PATH` yet, it says so
(`uv tool update-shell`). The server address in the script is *Admin → Settings → TaskBrigade URL*,
otherwise the address the request came in on; `TBR_URL=<url>` overrides it:
`curl -fsSL <url>/install.sh | TBR_URL=<url> sh`.

Only peers that may use the API can fetch `/install.sh` and `/download/` (loopback/`tailscale serve`,
`TRUSTED_PEER_NETWORKS`, `API_NETWORKS`); a `LAN_UI_NETWORKS`-only address gets 403.

**Update** (for example after the TaskBrigade server was updated): in the agent's work directory, so the
`.taskbrigade.toml` there names the server,

```bash
cd ~/taskbrigade-work/<agent>
tbr self-update                                                # installs the server's version (uv tool)
```

`tbr` warns once per call on stderr when it is older than the server
(`Warning: tbr 0.4.0 is older than the TaskBrigade server (0.5.0) – update with: tbr self-update`) or the same version
but another build (`… tbr 0.6.0+1a2b3c4d5e is another build than the TaskBrigade server (0.6.0+9f8e7d6c5b) …`); the
MCP server shows the same warning at the top of `list_tasks`. The server sends its version in the
`X-TaskBrigade-Version` header of every response and the build identifier of the wheel it delivers in
`X-TaskBrigade-Build` (both also in `/healthz`; for a `tbr` before 0.8 also as `X-Taskmeister-Version`/`-Build`). `tbr version` shows both sides as `<version>+<build>`, and
`tbr self-update` installs whenever version or build differ. A `tbr` installed from a source checkout (editable) has
no build identifier and compares versions only.

Alternative without the server (development): `uv tool install --python 3.12 --from <checkout> taskbrigade`.

### From `tm` to `tbr` (TaskBrigade 0.5)

The CLI is now called `tbr` (`tbr-admin` on the server), its package `taskbrigade` (before: `taskmeister`).
`tm` and `tm-admin` stay as full aliases during the transition – same commands, same output – and print one note
per call on stderr (`Note: tm is now called tbr …`). `TBR_NO_RENAME_NOTICE=1` hides it. Machine interfaces never
print it: `tm git-credential`, `tm mcp` and `tm prepare --wake-gate`.

Switching an agent machine over, without an agent losing its command at any point:

1. Update the server (`deploy/update.sh` or `deploy/docker/update.sh`).
2. On the agent machine, in the work directory: `tm self-update`, then `tbr self-update` – or in one step
   `curl -fsSL <server>/install.sh | sh`.
   - An old `tm self-update` asks for `taskmeister-<version>-py3-none-any.whl`. The server serves the same wheel under
     that name with the package name `taskmeister` in its metadata (uv refuses a wheel whose metadata names another
     package), so the old uv tool gets the new code – including `tbr`.
   - `tbr self-update` and `install.sh` then install `taskbrigade` with `uv tool install --force`, which takes over
     the links `tm`/`tm-admin` from the old tool, and afterwards delete only the old tool's environment
     (`$(uv tool dir)/taskmeister`). Not `uv tool uninstall taskmeister`: it would delete the `tm` links that now
     belong to `taskbrigade`. The old environment is kept (with a warning) while one of the commands still points
     into it.
3. `tbr setup <kind>` in the work directory (the same kind as before) switches what still calls `tm`: the credential
   helper in the agent's repos, MCP entries of Claude and Codex Desktop for this work directory, the Hermes check
   script, the OpenClaw trigger and `opencode.json` (since 0.8 also the names, see below).
   Every changed file keeps its previous version as `<file>.bak`; a second run changes nothing. Even without this
   step the next `tbr run`/`tbr prepare` sets the repos' credential helper to `tbr`.
4. `tbr doctor` lists every place that still calls `tm` (warning `tm → tbr`), with the command that switches it –
   including a global `credential.helper "!tm git-credential"` and an installation that is still the old package.

Nothing breaks in between because `tm` keeps working and nothing is pointed at `tbr` before `tbr` exists. Triggers
written since 0.5 accept the assignment header `# TaskBrigade assignment <NR>` (sent since 0.8) as well as the old
`# Taskmeister assignment <NR>` and fall back to `~/.local/bin/tm` on a machine that has no `tbr` yet. The runner allows `Bash(tbr:*)` and `Bash(tm:*)` (each also with its absolute path) for
Claude Code; an own `claude_allow` that only lists `Bash(tm:*)` gets `Bash(tbr:*)` added.

### From the names `taskmeister` to `taskbrigade` (TaskBrigade 0.8)

Since 0.8 every technical name is TaskBrigade's. The old names are still read during the transition – each with a
note on stderr (`Note: … from before TaskBrigade 0.8 …`, hidden with `TBR_NO_RENAME_NOTICE=1`, never for
`git-credential`, `mcp` and `prepare --wake-gate`) – so an agent machine keeps working before it is switched:

| Before 0.8 | Since 0.8 | Read during the transition |
|---|---|---|
| `<work dir>/.taskmeister.toml` | `<work dir>/.taskbrigade.toml` | yes; in the same directory the new file wins |
| `~/.config/taskmeister/config.toml` | `~/.config/taskbrigade/config.toml` | yes, if the new one does not exist |
| `TM_URL`, `TM_TOKEN`, `TM_CONFIG`, `TM_LANGUAGE` | `TBR_URL`, `TBR_TOKEN`, `TBR_CONFIG`, `TBR_LANGUAGE` | yes; the new name wins |
| `TM_BIN`, `TASKMEISTER_WORK_ROOT` (Hermes check script) | `TBR_BIN`, `TBR_WORK_ROOT` | the old script keeps its names until `tbr setup hermes` replaces it |
| `~/taskmeister-work/<agent>` (default in docs and join commands) | `~/taskbrigade-work/<agent>` | existing directories stay where they are – nothing is moved |
| runner timer `tm-runner-<agent>` / launchd `com.taskmeister.runner.<agent>` | `tbr-runner-<agent>` / `com.taskbrigade.runner.<agent>` | the old timer keeps running until it is switched |
| tmux session `taskmeister` | `taskbrigade` | an explicit `tmux_session` in `[runner]` stays |
| MCP server entry `taskmeister` | `taskbrigade` | the old entry keeps working |
| skills `taskmeister`, `taskmeister-telegram`; `taskmeister-prepare.sh`, `taskmeister-trigger.js` | `taskbrigade`, `taskbrigade-telegram`; `taskbrigade-prepare.sh`, `taskbrigade-trigger.js` | the old job keeps using its files until it is renamed |
| Hermes cron job / OpenClaw automation `taskmeister-<agent>` | `taskbrigade-<agent>` | – |
| assignment header `# Taskmeister assignment <NR>` | `# TaskBrigade assignment <NR>` | the shipped triggers and skills recognise both |
| response headers `X-Taskmeister-Version`/`-Build` | `X-TaskBrigade-Version`/`-Build` | the server sends both, `tbr` reads both |

Switching an agent machine (after the server, see [operations.md](operations.md#migrating-an-installation-from-before-taskbrigade-08)):

1. `tbr self-update` in the work directory (as usual).
2. `tbr setup <kind>` (the same kind as before) or `tbr doctor --fix`. Both, idempotent and with backups:
   - rename `.taskmeister.toml` → `.taskbrigade.toml` (same content and mode; the old file stays as
     `.taskmeister.toml.bak`). `tbr doctor` does this step by itself, without `--fix`.
   - stop and disable the old runner timer (`systemctl --user disable --now tm-runner-<agent>.timer`, also an enabled
     template instance `tm-runner@<agent>.timer`; launchd `bootout`), keep its files as `*.bak` and install
     `tbr-runner-<agent>` (`tbr setup … --no-timer` only stops the old one).
   - rename the MCP entry `taskmeister` of this work directory to `taskbrigade` in Claude Desktop and Codex Desktop
     (other entries untouched, previous file `<file>.bak`). Restart the app afterwards. In Claude Code (CLI) an entry
     added by hand is renamed by hand: `claude mcp remove taskmeister`, then `claude mcp add … taskbrigade …`
     ([section 8](#8-creating-tasks-from-your-own-claude-code-sessions-actor-orchestrator-claude)). Tool names change with the server name (`mcp__taskbrigade__…`), so adjust
     permission rules that name the old ones.
   - Hermes/OpenClaw: install skill and script under the new names and rename the job/automation `taskmeister-<agent>`
     with `hermes cron edit`/`openclaw automations edit --name` – never a second job next to it. Only when the CLI
     does not offer the needed flags, the old job stays (with a warning and the exact commands) and keeps its old
     files; once no old job is left, the old skills move to `<work dir>/.backup-taskmeister/` (a skill folder left in
     the framework would still be loaded) and the old script/trigger to `<file>.bak`.
   - Claude Desktop/Cowork skill: upload `taskbrigade-skill.zip` (path printed by `tbr setup claude-desktop`) and
     remove the old skill `taskmeister` in Claude under Settings → Capabilities → Skills.
3. `tbr doctor` – warnings with the label `taskmeister → taskbrigade` name whatever is left, each with
   `tbr setup <kind>` or `tbr doctor --fix` as the fix.

Way back on an agent machine: the previous files are the `*.bak` next to them (`.taskmeister.toml.bak`, the
`tm-runner-*` units, the app configurations) and `<work dir>/.backup-taskmeister/`; an older `tbr` reads only the old
names.

**Name clash: the ebook reader `tbr`.** The Rust terminal ebook reader `tbr` (crates.io, `cargo install tbr`)
installs a binary of the same name. If its directory (e.g. `~/.cargo/bin`) comes first on `PATH`, `tbr` starts the
reader; `install.sh` warns about another `tbr` on `PATH`. Then call `~/.local/bin/tbr`, put `~/.local/bin` first or
keep using `tm`. `tbr setup` writes absolute paths into timers, MCP entries and scripts, so they are not affected.

## 2. Check privacy (local or cloud?)

`runtime` (where the agent runs) and the **model location** (where its model runs) decide whether an agent may get
tasks from projects marked "local agents only" (`local_only`): with *Admin → Settings* “local_only only for agents
with a local model” on (the default for new installations) both must be `local` (details:
[architecture.md](architecture.md#privacy)).

- **Claude Code** and **Codex** send code to Anthropic or OpenAI → model location `cloud` (fixed). Running the CLI on
  your own machine does not make it local.
- **OpenCode:** depends on the configured model, for example a local Qwen3-Coder 30B behind your own provider.
  Look at the configuration (`~/.config/opencode/opencode.json` or `.jsonc`, sections `provider` / `model`): if
  `baseURL` points to your own host, localhost or the LAN → runtime and model location `local`. If it uses a cloud
  provider → model location `cloud`. **⏸ Admin** sets both when creating the agent in TaskBrigade (and can change them
  later); “local model” needs the confirmation “I have checked that this agent uses no cloud model”.

## 3. Work directories and identities

### Recommended: join code

**⏸ Admin:** in TaskBrigade *Admin → Users & agents → New agent* enter name, kind, runtime, model location, role
and the projects the agent may work on. For an existing agent (e.g. one an older version created at installation), open it and follow
“Join code for tbr join / Gitea setup”.

- With a **Gitea admin token** in *Admin → Settings* (“Gitea admin token”; the Docker profile
  `gitea` sets it) TaskBrigade creates the agent's Gitea user and token itself. The user gets no admin rights; the
  token has the scopes `write:repository` and `read:user`.
- Without it, the page guides through the manual step (create the Gitea user and a token, enter both, TaskBrigade
  checks the token) – the same as linking a Gitea user on the agent's page.

The page then shows a one-time code and the command to copy. For the kinds `claude_code`, `codex`, `opencode`,
`hermes` and `openclaw` the command already ends with `--setup <kind>` (Hermes/OpenClaw:
[agent-frameworks.md](agent-frameworks.md); desktop apps: [desktop-apps-mcp.md](desktop-apps-mcp.md)). On the agent host, as the agent's user:

```bash
curl -fsSL https://taskbrigade.example.ts.net/install.sh | sh       # only if tbr is missing (step 1)
tbr join https://taskbrigade.example.ts.net tmj_… --dir ~/taskbrigade-work/claude-code --setup claude-code
```

`tbr join` exchanges the code for a fresh API token (the agent's previous token becomes invalid), creates the work
directory and `.taskbrigade.toml` with mode 600, and checks `tbr whoami` and the Git access. For `claude_code` and
`codex` it also writes the `[runner]` section shown below; for other kinds add one yourself. It never overwrites an
existing `.taskbrigade.toml` without `--force` and refuses before using the code, so the code stays valid.
`--language de` writes `language = "de"`. If it reports a missing credential helper, run
`git config --global credential.helper "!tbr git-credential"` once – only needed for Git outside the runner; `tbr run`
sets the helper in each clone itself. A typo in `--setup` is reported before the code is used.

### `tbr setup`: runner, prerequisites, timer

`tbr join … --setup <kind>` runs `tbr setup` right after joining; on its own, in an already joined work directory:

```bash
cd ~/taskbrigade-work/<agent>
tbr setup claude-code             # or: codex | opencode;  --work-root <dir> instead of cd
tbr setup codex --no-timer        # manual operation: tbr run yourself (or from cron)
```

It prints one line per step (`[ok]`, `[warning]`, `[error]` with a hint after `→`) and exits with 1 if something is
missing; run it again after fixing it – it is idempotent.

- **`[runner]`** for the kind (the sections below) in `.taskbrigade.toml`; an existing `[runner]` is replaced and the
  previous file kept as `.taskbrigade.toml.bak` (mode 600). `url`, `token` and `language` stay untouched. For
  OpenCode it also writes `opencode.json` with the approvals (only if missing).
- **Prerequisites:** tmux and the agent command on the `PATH` – the same check `tbr run` makes before claiming,
  with install hints.
- **CLI preparation** ([step 4](#4-one-time-preparation-of-the-clis)): Codex – sets
  `check_for_update_on_startup = false` in `~/.codex/config.toml` if the key is missing and warns when AppArmor
  restricts user namespaces (Ubuntu 24.04); Claude Code – warns when it was never started and logged in
  (`~/.claude.json` missing); OpenCode – reminds you to check the runtime (local/cloud).
- **Timer** every 10 minutes, with absolute paths of `tbr` and the work directory and a `PATH` made of the
  directories where `tbr`, git, tmux and the agent command were found:
  - Linux: `~/.config/systemd/user/tbr-runner-<agent>.service` and `.timer`, enabled with
    `systemctl --user enable --now`. Without linger the timer only runs while the user is logged in – `tbr setup`
    says so (**⏸ Admin:** `sudo loginctl enable-linger <user>`). No systemd user session (some containers):
    use `--no-timer` and a cron job `*/10 * * * * <tm> run --work-root <dir>`.
  - macOS: launchd agent `~/Library/LaunchAgents/com.taskbrigade.runner.<agent>.plist` (`StartInterval` 600,
    `AbandonProcessGroup` so the tmux server survives the pass), loaded with `launchctl bootstrap gui/<uid>`; output
    in `<work directory>/logs/launchd.log`.

### `tbr doctor`: check an agent machine

```bash
tbr doctor --work-root ~/taskbrigade-work/<agent>     # or in the work directory without --work-root
```

| Check | Error / warning when … | Hint |
|---|---|---|
| Configuration | no `.taskbrigade.toml` (or `TBR_URL`/`TBR_TOKEN`) | `tbr join` |
| Connection | `/healthz` not reachable or db not ok | url, network, Tailscale |
| tbr version | `tbr` older than the server (warning) | `tbr self-update` |
| Token | rejected (401) or agent inactive | new join code, `tbr join … --force` |
| Git access | no Gitea credentials, Gitea rejects the token | admin links the Gitea user |
| Runner | no `[runner]` (warning), tmux or agent command missing | `tbr setup <kind>`, install command |
| Desktop app / framework | instead of Runner and Timer: MCP entry, skill, check script, cron job/automation | `tbr setup <kind>` |
| Timer | timer not active, linger off (warnings) | `tbr setup <kind>` |
| Tasks | own task *in progress* with an expired lease or without a running session (warnings) | next `tbr run` resumes |
| Agent login | the agent CLI is not logged in (`claude auth status`, `codex login status`, `opencode auth list`); warning if it cannot be checked | log in, e.g. `claude /login` |
| Runner block | the start check has blocked the runner (error, with the reason); removed once the agent login is ok | fix the cause, run `tbr doctor` again |
| SFTP | only when SFTP storage is enabled on the server: no SFTP login for this agent (warning), login or host key fails (error) | admin: *Admin → Users & agents → <agent>* “Test access”; network to the SFTP host |
| tmux windows | open windows in the runner session, finished ones and agents still running although their task ended (warning) | `tbr cleanup [--stale]` |

Missing Git credentials are an error only with a `[runner]` section or a desktop app/framework set up by `tbr setup`
(an orchestrator identity does not need Git).
`tbr doctor` exits with 1 if any check fails and never prints the token.

The code is valid for 15 minutes (*Admin → Settings*, “Join code validity (minutes)”), works once and is stored
only as a hash; creating a new one invalidates the previous one. It is redeemed only over HTTPS through
`tailscale serve` or from loopback/`TRUSTED_PEER_NETWORKS` – the same rule as Git credentials; `API_NETWORKS` peers
get `403 insecure_channel`. Expired, used or unknown codes get a clear error (`join_code_expired`,
`join_code_used`, `join_code_invalid`); ask the admin for a new one.

### Manual alternative

For each agent (`claude-code`, `codex`, `opencode`):

```bash
install -d -m 700 ~/taskbrigade-work/<agent>
```

Then create `~/taskbrigade-work/<agent>/.taskbrigade.toml` with `chmod 600` – content per agent below. Do **not**
fill in the token yourself; leave the placeholder. Ready-made files are in
[`deploy/agents/runner/`](../deploy/agents/runner/).

**⏸ Admin:** in TaskBrigade *Admin → Users & agents → <agent> → Rotate token*, copy the token
and replace `PASTE_API_TOKEN_HERE` with `nano ~/taskbrigade-work/<agent>/.taskbrigade.toml`.

Prompts, `TASK.md` and `tbr` messages are English. For German, add `language = "de"` at the top of the file (above
`[runner]`) or set `TBR_LANGUAGE=de`; the workflow rules are the same in both languages.

### claude-code

```toml
url = "https://taskbrigade.example.ts.net"
token = "PASTE_API_TOKEN_HERE"

[runner]
command = ["claude", "--permission-mode", "auto", "{prompt}"]
mode = "tmux"
tmux_session = "taskbrigade"
# Auto mode: Claude Code approves harmless actions itself (with acceptEdits only file edits are approved and
# every other command prompts). The runner additionally passes approvals as --allowedTools.
# Add the project's test commands, for example:
# claude_allow = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git:*)", "Bash(tbr:*)", "Bash(tm:*)", "Bash(npm test:*)"]
```

Optional for every agent (defaults shown; `0` switches each one off):

```toml
stop_after_finish_seconds = 120          # stop the agent this long after its task left "in progress"
close_finished_windows_after_hours = 24  # tbr run closes finished tmux windows older than this
start_check_seconds = 60                 # look for login/limit blockers and an unsent start prompt
stall_minutes = 30                       # one thread comment if the window does not change this long
park_idle_seconds = 120                  # end an idle session whose task waits for a background run (parked)
```

### codex

```toml
url = "https://taskbrigade.example.ts.net"
token = "PASTE_API_TOKEN_HERE"

[runner]
command = ["codex", "--sandbox", "workspace-write", "--ask-for-approval", "never",
           "-c", "sandbox_workspace_write.network_access=true", "{prompt}"]
git = "runner"
mode = "tmux"
tmux_session = "taskbrigade"
```

Check with `codex --help` that the installed version has `--sandbox`, `--ask-for-approval` and `-c`, and adjust if
needed. Rationale: `tbr` needs the network; `never` so the run does not hang on an approval prompt.

**The runner does Git (`git = "runner"`, the default for Codex):** inside its sandbox Codex locks the Git metadata
(`.git`, including that of a worktree in the main clone) – `git commit` fails with
`index.lock: Read-only file system`. So Codex only edits files and finally writes `.tm-submit.md` (first line: commit
message, then the summary). After the run, `tbr session` commits outside the sandbox, pushes and submits. Codex still
calls `tbr ask`/`abort`/`comment` itself.

**SFTP storage from the Codex sandbox:** with `network_access=true` Codex calls `tbr sftp put <NR> <files…>` itself
(the SFTP server must be reachable from the agent machine; `tbr doctor` checks the login). If a sandbox cannot
reach it, TASK.md tells the agent to say so in its summary instead – a human then uploads the files. A runner-side
upload list in `.tm-submit.md` is not implemented yet.

### opencode

```toml
url = "https://taskbrigade.example.ts.net"
token = "PASTE_API_TOKEN_HERE"

[runner]
command = ["env", "OPENCODE_CONFIG=/home/<user>/taskbrigade-work/opencode/opencode.json",
           "opencode", "--prompt", "{prompt}"]
mode = "tmux"
tmux_session = "taskbrigade"
```

Plus `~/taskbrigade-work/opencode/opencode.json` with approvals for this agent only (your global OpenCode
configuration stays untouched):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "edit": "allow",
    "bash": {
      "*": "ask",
      "git *": "allow", "tbr *": "allow", "tm *": "allow", "/home/<user>/.local/bin/tbr *": "allow",
      "ls *": "allow", "cat *": "allow"
    }
  }
}
```

Check with `opencode --help` or the OpenCode documentation that `--prompt` and `OPENCODE_CONFIG` (additional
configuration, merged with the global one) are named like this in your version; adjust otherwise.

### Verify (per agent)

```bash
cd ~/taskbrigade-work/<agent>
tbr whoami                          # name, runtime, gitea_username correct?
printf 'protocol=http\nhost=gitea.example.internal:3001\n\n' | tbr git-credential get | grep username
tbr list                            # connection and permissions ok, shows own tasks
```

If `tbr git-credential` reports "404 … keine Gitea-Credentials" (no Gitea credentials) → **⏸ Admin** links the
Gitea user in TaskBrigade (*Admin → Users & agents → <agent> → Gitea*).

### Model per task (tiers)

Simple tasks can run on a cheaper, faster model and complex ones on a stronger one. A task carries an optional
**model tier** – `simple`, `standard` or `complex` (UI: *einfach / standard / komplex*) – and optionally a
**concrete model**. Each agent maps the tiers to models; **⏸ Admin** sets this under *Admin → Users & agents →
<agent> → Modelle* (models): *Standardmodell* (default model), one model per tier (*Stufe*) and the
*freigegebene Modelle* (allow list) a task may name explicitly.

| Agent | simple | standard | complex | Allow list (examples) |
|---|---|---|---|---|
| claude-code | `haiku` | `sonnet` | `opus` | `haiku, sonnet, opus` |
| codex | `gpt-5.6-luna` | `gpt-5.6-terra` | `gpt-5.6-sol` | `gpt-5.6-terra, gpt-5.6-sol` |
| opencode | a local model, e.g. `ollama/qwen3:32b` | a cloud model, e.g. `openrouter/…` | a stronger cloud model | the models configured in OpenCode |

Use exactly the names the CLI accepts (`claude --model`, `codex -m`, `opencode --model`). The runner picks the model
in this order: concrete model of the task → the agent's model for the task's tier → default model → nothing. With
nothing it adds no switch, and the CLI uses its own default – tasks and agents without settings behave as before.

- The runner adds the switch right after the program: Claude Code `--model <m>`, Codex `-m <m>`, OpenCode
  `--model <m>` (table `MODEL_FLAGS` in `cli/runner.py`, which also knows Gemini `-m` for a later extension). Another
  CLI: `model_flag = "--llm"` in `[runner]` (`"--llm="` for a single `--llm=<m>` argument, `""` for none), or put
  `{model}` into `command` – without a model that argument and a switch right before it are left out.
- The model used is recorded at every start in the task history (event `model_used`), and TASK.md shows it
  (`- Model: …`).
- A concrete model must be on the assigned agent's allow list, otherwise creating, changing or assigning the task
  fails with a clear message. Reassigning keeps the tier; a concrete model the new agent does not allow is dropped
  (event `model_dropped`).
- If the CLI reports the model as unknown or not part of the plan ("model not found", "issue with the selected
  model", "not supported when using Codex with a ChatGPT account", …), the start check stops the agent and hands the
  task back to a human as *aborted* with the reason in the thread – choose another tier or model and reassign it. The
  agent itself is not blocked and keeps taking other tasks.

Set the tier in the task form, the sprint editor, with `tbr create/update --tier simple|standard|complex` (German
`einfach`/`komplex` work too; `none` removes it) and `--model <name>`, or with MCP `create_task`/`update_task`
(`tier`, `model`).

### Task dependencies

An agent can claim a task only once all tasks it depends on are *done*. Orchestrators set the dependencies without the
web UI: `tbr create … --depends-on DEMO-0042 --depends-on DEMO-0043` at creation, and `tbr update DEMO-0050
--depends-on DEMO-0042 --depends-on DEMO-0043` later. `tbr update --depends-on` **replaces** the whole list,
`--depends-on none` removes all dependencies, and without the option they stay unchanged. In MCP, `create_task` and
`update_task` take `depends_on` (a list of task numbers or a comma-separated string; `"none"` clears on update).
`tbr show` / `show_task` list the dependencies with their status. The server refuses unknown task numbers,
self-dependencies and dependency circles, and the usual edit permissions apply (orchestrator, admin, project owner);
the message is printed as is.

### Editing and reopening sprints

Admins, orchestrators and the project owner change a sprint's name, goal or dates without the web UI:

```bash
tbr sprint update "Sprint 1" -p DEMO --name "Sprint 1b" --goal "Ship the login" --start 2026-10-12 --end 2026-10-23
```

Only the options you pass are sent; the others stay as they are. `--start none` and `--end none` clear a date,
`--goal ""` clears the goal (`--goal -` reads the goal from stdin, as on `tbr sprint create`). The status does not
change here – releasing, closing and reopening stay separate commands. The reply is the same view as
`tbr sprint show` (or `--json`).

Admins and the project owner reopen a closed sprint with `tbr sprint reopen "Sprint 1" -p DEMO`. By default the
tasks that were moved out when it was closed come back (their status unchanged); `--no-restore` (same as
`--no-restore-tasks`) leaves them where they are. Restored and skipped tasks are listed. Reopening a sprint that is
not closed, a duplicate name, an end date before the start, an unknown sprint and a missing permission are reported
as the server's message (no stack trace).

The same two operations exist as MCP tools: `update_sprint(project, sprint, name, goal, start_date, end_date)`
(`"none"` clears a date) and `reopen_sprint(project, sprint, restore_tasks=True)`. `sprint` is an id or a name.

### Reviewer agents

An agent with the role `reviewer` (*Admin → Users & agents*) reviews tasks it is entered for as reviewer and
works on its own tasks like a worker. Its runner needs no extra setup: `tbr run` claims a review job before new work
(in the same `max_parallel_tasks` budget), checks the task branch out read-only in `<KEY>/.worktrees/<NR>-review`
with `REVIEW.md` (task, criteria, thread, the assignee's summary and open points, `git diff --stat` against the base,
pull request link, commands) and starts the agent in the tmux window `<NR>-review` with a review prompt
(`[runner] review_prompt` overrides it; placeholders `{agent}` and `{task_nr}`). The agent decides with `tbr review
<NR> approve|reject|conflict`; if it ends without a decision, `tbr session --review` gives the job back to its queue
(the thread says why). The review lease is extended like a task lease; the review model is the task's tier resolved
with the reviewer's own model settings. Claude Code needs no extra permission (`Bash(tbr:*)`, `Bash(git:*)` are in
the default list). Give the reviewer agent read access to the repository – TaskBrigade adds it as a read
collaborator when it is entered as reviewer.

## 4. One-time preparation of the CLIs

An agent started automatically must never get stuck in a dialog.

- **Claude Code** asks "Do you trust the files in this folder?" in every new Git repository – that is, in every task
  worktree (trusting the parent folder does not apply to Git repositories). The runner confirms this dialog in the
  tmux window automatically – **unless** the folder brings its own approvals ("pre-approves … permissions", for
  example a committed `.claude/settings.json`); then you decide in the window. The runner passes approvals for
  git/tbr/tm as start parameters (`--allowedTools`), so do **not** create a `.claude/settings.local.json`.
  On the very first start of Claude Code on the host, complete login/onboarding once interactively.
- **Codex** shows an update dialog at startup. `check_for_update_on_startup = false` in `~/.codex/config.toml`
  prevents it (`tbr setup codex` adds it if the key is missing).
  The runner handles the per-worktree trust dialog itself (Codex does not inherit trust from the parent folder; the
  runner adds exactly the worktree and removes it afterwards).
- **OpenCode** starts without a dialog. In `opencode.json`, allow the absolute path of `tbr` as well as `tbr *` (see
  above) – older `TASK.md` versions call `tbr` with its full path.
- **Compound commands:** approval rules such as `Bash(git:*)` or `git *` only match single invocations. An `&&`
  chain or a heredoc (`git commit -F - <<…`) triggers an approval prompt, and the run hangs. `TASK.md` and the
  runner prompt therefore tell agents to call `git`/`tbr` one at a time and to pass commit messages with
  `git commit -m` (observed with Claude Code).

### Codex sandbox on Ubuntu 24.04 (AppArmor)

Ubuntu 24.04 sets `kernel.apparmor_restrict_unprivileged_userns = 1`. Then Codex's bwrap sandbox cannot create user
namespaces ("bwrap: setting up uid map: Permission denied" or "loopback: Failed RTM_NEWADDR"), and Codex cannot run
**any** command – not even `tbr ask`. Check before the first task:

```bash
sysctl kernel.apparmor_restrict_unprivileged_userns     # 1 = restriction active
cd /tmp && codex sandbox -- /bin/echo sandbox-ok          # must print "sandbox-ok"
```

If the test fails: **⏸ Admin** creates an AppArmor profile that allows user namespaces for Codex only. Find the path
first with `readlink -f "$(command -v codex)"` and put it into the profile. File `/etc/apparmor.d/codex` (create
with sudo):

```
abi <abi/4.0>,
include <tunables/global>
profile codex /home/<user>/.codex/packages/**/bin/codex* flags=(unconfined) {
  userns,
  include if exists <local/codex>
}
```

Load and check:

```bash
sudo apparmor_parser -r /etc/apparmor.d/codex
cd /tmp && codex sandbox -- /bin/echo sandbox-ok          # now "sandbox-ok"
```

Not recommended: `--sandbox danger-full-access` – Codex would then run without any sandbox.

## 5. One test run per agent (before the timer)

**⏸ Admin:** create a small task in project `DEMO` and assign it to the agent (for example "Create `<agent>.md`
with a heading").

```bash
tbr run --work-root ~/taskbrigade-work/<agent>
tmux attach -t taskbrigade          # window named after the task; switch with Ctrl-b n / Ctrl-b w
```

Expected: the agent works through `TASK.md`, pushes and calls `tbr submit`; in TaskBrigade the task is in "Review"
with a pull request. Then quit the agent (`/exit` or Ctrl-C); the window reports that the agent already reported
back itself. Close the window with `Ctrl-b &`.

Without a task, `tbr run` only prints that there is no task. If it reports `Prerequisites missing – no task claimed`,
install what it names (or put the absolute path into `[runner] command`) and run it again.

## 6. Set up the timer

`tbr setup <kind>` installs the timer (Linux: `tbr-runner-<agent>.timer`, macOS: `com.taskbrigade.runner.<agent>`) –
see [`tbr setup`](#tbr-setup-runner-prerequisites-timer). Check it with `tbr doctor`.

**Manual alternative (Linux)** with the template units from the repository (or from your clone in
`~/tools/taskbrigade`):

```bash
mkdir -p ~/.config/systemd/user
cp ~/tools/taskbrigade/deploy/agents/runner/tbr-runner@.service ~/.config/systemd/user/
cp ~/tools/taskbrigade/deploy/agents/runner/tbr-runner@.timer ~/.config/systemd/user/
```

[`tbr-runner@.service`](../deploy/agents/runner/tbr-runner@.service) sets
`PATH=%h/.local/bin:%h/.npm-global/bin:%h/.opencode/bin:/usr/local/bin:/usr/bin:/bin`. Adjust it so that every
directory from `command -v tbr git tmux claude codex opencode` is included. `KillMode=process` is required so that a
tmux server started by the runner keeps running after the pass.

```bash
systemctl --user daemon-reload
systemctl --user enable --now tbr-runner@claude-code.timer tbr-runner@codex.timer tbr-runner@opencode.timer
systemctl --user list-timers | grep tbr-runner
loginctl show-user "$USER" -p Linger       # Linger=no → ⏸ Admin: sudo loginctl enable-linger <user>
```

Without linger, user timers only run while the user is logged in.

## 7. Operation

- Watch/intervene: `tmux attach -t taskbrigade`; one window per task.
- Check the machine: `tbr doctor --work-root ~/taskbrigade-work/<agent>`
- An agent's journal: `journalctl --user -u tbr-runner-claude-code -n 30` (manual template units:
  `tbr-runner@claude-code`); macOS: `~/taskbrigade-work/<agent>/logs/launchd.log`
- Transcript per run: `~/taskbrigade-work/<agent>/logs/<TASK>-<time>.log` (review jobs: `<TASK>-review-<time>.log`)
- Why an assigned task was not claimed: `~/taskbrigade-work/<agent>/logs/runner.log`
- Pause: `systemctl --user stop tbr-runner-codex.timer` (resume: `start`); macOS:
  `launchctl bootout gui/$(id -u)/com.taskbrigade.runner.codex` (resume: `tbr setup codex` again)
- `~/taskbrigade-work/<agent>/.runner.lock` always stays in place and holds the PID/start time of the last run.
  Whether a run is active right now: `flock -n ~/taskbrigade-work/<agent>/.runner.lock true && echo free || echo running`.
  The same works per task with `.locks/session-<TASK>.lock` (held by `tbr session` while the agent runs).
- A task stuck *in progress* after a crash or reboot is resumed by the next timer pass; a start failure gives it back
  with the reason (thread and `logs/runner.log`).
- Interactive agents do not exit by themselves when they are done; `tbr session` stops them
  `stop_after_finish_seconds` after `tbr submit/ask/abort` (the window stays visible). With
  `stop_after_finish_seconds = 0` they stay open as before: the lease is no longer extended and up to
  `max_session_hours` (default 8) the runner keeps a hanging run alive.
- Clean up windows by hand: `tbr cleanup --work-root ~/taskbrigade-work/<agent>` closes every finished window
  (`--min-age-hours N` only older ones); `--stale` also closes windows whose agent still runs although its task is no
  longer in progress (left over from older `tbr` versions). The content goes to the run log first.
- Runner blocked after a failed start check (`tbr run` says „Agent blocked …“): fix the cause (log in, wait for the
  limit), then `tbr doctor`; it removes `.agent-blocked` once the agent login checks out. If the login cannot be
  checked automatically, delete the file yourself.
- By default an agent works on up to two tasks at a time (TaskBrigade: *max. parallele Tasks*, max. parallel tasks,
  under *Admin → Users & agents*). At the limit `tbr claim`/`tbr prepare` say so and name the tasks in progress.

## 8. Creating tasks from your own Claude Code sessions (actor `orchestrator-claude`)

Your interactive Claude Code sessions on the host can get their own actor. It only creates and assigns tasks – it
does not work on them. The runner actor `claude-code` stays a `worker`.

**⏸ Admin** – in TaskBrigade under *Admin → Users & agents → New agent*:

| Field | Value |
|---|---|
| Name | `orchestrator-claude` |
| Type / kind | agent / `claude_code` |
| Runtime | `cloud` |
| Role | `orchestrator` |
| Gitea user | leave empty (does not work in repositories) |

Enter the API token shown on creation yourself:

```bash
install -d -m 700 ~/taskbrigade-work/orchestrator-claude
nano ~/taskbrigade-work/orchestrator-claude/.taskbrigade.toml
chmod 600 ~/taskbrigade-work/orchestrator-claude/.taskbrigade.toml
```

```toml
url = "https://taskbrigade.example.ts.net"
token = "PASTE_API_TOKEN_HERE"
```

Verify and register for all Claude Code sessions of your user:

```bash
cd ~/taskbrigade-work/orchestrator-claude && tbr whoami
claude mcp add --scope user taskbrigade -- "$HOME/.local/bin/tbr" mcp --work-root "$HOME/taskbrigade-work/orchestrator-claude"
claude mcp list
```

`claude mcp list` must show `taskbrigade … ✓ Connected`. In a new session (any folder): "Create a TaskBrigade task
in project DEMO for codex: …" → `list_projects`/`list_agents`/`create_task`.

Notes:

- The MCP server is also visible in the runner sessions of `claude-code` (same Linux user), but acts as
  `orchestrator-claude` there. The runner instructions (`TASK.md`) only use `git`/`tbr`; to rule it out completely,
  start the runner with `--strict-mcp-config` (add it to `command` in the runner configuration).
- `start_task`/`submit_task` fail for `orchestrator-claude` on purpose (no Gitea user) – it only creates tasks.
- With a description and acceptance criteria a task is "defined" right away and will be claimed by its assigned
  agent; otherwise have it created as a draft.
- For a second opinion from another agent or model: "Duplicate DEMO-0042 for codex" → `duplicate_task` (CLI:
  `tbr duplicate DEMO-0042 --assign codex`). The copy is a draft titled "(duplicate) …" without thread, review or
  branch and links to the original; publish it with `reset_task`/`tbr reset`. The web UI has the same action
  (“Duplicate”) in the task detail view.

## See also

- [Desktop apps via MCP](desktop-apps-mcp.md) – Claude Desktop/Cowork and Codex Desktop, started by hand.
- [Agent frameworks with a wake gate](agent-frameworks.md) – Hermes and OpenClaw.
- [Agent workflow and API](agent-skill.md)
