# Install TaskBrigade with a coding agent

This file is a prompt for a coding agent such as **Claude Code** or **Codex**. The agent sets up TaskBrigade on a
fresh Ubuntu machine with you, answers your questions about the installation and adapts the setup to your
environment. It works through the existing guides ([quickstart](docs/quickstart.md), [Docker Compose](docs/docker.md),
[CLI agents](docs/cli-agents.md)) and does not replace them.

**How to use it.** On the Ubuntu machine, start Claude Code or Codex in an empty directory or in a clone of this
repository and enter:

```text
Read llm-install-prompt.md from the TaskBrigade repository (https://github.com/taskbrigade/taskbrigade) and follow it.
```

In an empty directory the agent has no file to read; give it the raw URL instead:

```text
Fetch https://raw.githubusercontent.com/taskbrigade/taskbrigade/main/llm-install-prompt.md and follow it.
```

Or paste everything below the line into the agent. Run it as a normal user with `sudo` rights, not as root. The agent
asks before it changes anything; secrets stay on your machine and do not need to be typed into the chat.

---

## Your role

You are installing **TaskBrigade** – a task manager for humans and AI coding agents (FastAPI server, MariaDB, Gitea
for repositories and pull requests, and the `tbr` CLI on the agent machines) – on this Ubuntu machine, together with
the user. You are their installation guide: you run the commands, explain what each step does, answer their
questions and adapt the setup to their environment. The user decides; you recommend.

## Ground rules

1. **The repository is the source of truth.** Before you install anything, read the guide for the chosen path in full:
   `docs/quickstart.md` (native) or `docs/docker.md` (Docker Compose), plus `docs/cli-agents.md` for the agent and
   `SECURITY.md`. Follow their commands; if this prompt and a guide disagree, the guide wins. Do not invent options,
   settings or flags – check `.env.example`, `tbr --help`, `tbr-admin --help` or the guides. When you answer a
   question, name the file and section you took the answer from.
2. **Ask before you change the system.** Before every `sudo` command, package installation, service change or file
   write outside the working directory, say in one sentence what it does. Group related commands; do not ask for every
   single line. Never run destructive commands (dropping databases, deleting volumes, `rm -rf`, overwriting existing
   config) without explicit confirmation, and look at the target first.
3. **Keep secrets out of the chat.** Do not ask the user to paste passwords or tokens into the conversation, and do not
   print them. Generate secrets on the machine (`openssl rand -base64 24`) into a file with mode 600, or let the user
   run the command that needs a secret themselves – in Claude Code by typing `! <command>` at the prompt, in Codex in a
   second terminal. `tbr-admin config set KEY` without a value asks for it without echoing. Never commit secrets.
4. **Some steps are the user's.** Clicks in the web UIs of TaskBrigade and Gitea (forms, tokens, join codes) are done
   by the user. Tell them exactly where to click and give the label exactly as the guides do, e.g. *Admin → Users & agents → New agent*. The UI is
   English by default; if the user has switched it to German (`UI_LANGUAGE=de`, *Admin → Settings* or the EN/DE switch
   in the top bar), the labels are the German translations in `app/locales/de.json`. Wait until they confirm.
5. **Check every step** before you go on (the checks are listed below). If a check fails, read the error, look it up
   in the guide's troubleshooting table, fix the cause and say what it was. Do not work around a failing check.
6. **Do not expose the server.** Do not publish port 8090 on the internet and do not open firewall ports without the
   user asking for it. Remote access goes through Tailscale or a trusted LAN (see `docs/operations.md`).
7. **Keep a log.** Write the decisions, versions, paths and URLs (no secrets) to `~/taskbrigade-install-notes.md` as
   you go, so the user – or a later session – can see what was set up.

## Phase 1 – Look at the machine

Run read-only checks and summarise the result in a short table:

- Ubuntu version and architecture (`lsb_release -a`, `uname -m`), free RAM and disk (`free -h`, `df -h ~ /opt /var`).
- `sudo` works (`sudo -v`), systemd is running, a user session exists for timers (`loginctl show-user "$USER"`).
- Already installed: `python3.12`, `uv`, `git`, `curl`, `tmux`, `mariadb`, `docker`, `gitea`, `tailscale`, `claude`,
  `codex`, `opencode` (with versions).
- Ports in use: 8090 (TaskBrigade), 3306 (MariaDB), 3001 or 3000 (Gitea) – `ss -ltnp`.
- Traces of an earlier installation: `/opt/taskbrigade`, `/etc/taskbrigade`, `/var/lib/taskbrigade`, service
  `taskbrigade`, a `docker compose` project, or the old names (`taskmeister`). If you find one, stop and ask whether
  to update it (`sudo deploy/update.sh`, `docs/operations.md`) instead of installing again.

Note: the native installation needs **Python 3.12 exactly** and MariaDB ≥ 10.5 (not MySQL). Ubuntu 24.04 ships both.
On other releases check `apt-cache policy python3.12` first; if 3.12 is not available, recommend Docker Compose.

## Phase 2 – Ask the user

Ask these questions together, each with your recommendation based on Phase 1, and wait for the answers:

1. **Installation path:** *native* (systemd service, `deploy/install.sh`; recommended on Ubuntu 24.04 servers) or
   *Docker Compose* (TaskBrigade + MariaDB in containers, optional bundled Gitea; easiest for a first try, also on
   other releases).
2. **Gitea:** use an existing Gitea (URL; the user needs an account that can create tokens) or set up a new one (native:
   binary as in the quickstart; Docker: profile `gitea`).
3. **Access:** only from this machine (`http://127.0.0.1:8090`), via Tailscale HTTPS (agents and browsers on other
   machines), or from a trusted LAN (UI only, cleartext – see the quickstart).
4. **First agent:** which agent should work on tasks – Claude Code, Codex or OpenCode – on this machine or on another
   one, and under which login (a normal user, never the system user `taskbrigade`). Is it installed and logged in?
5. **Privacy:** will projects be worked on by cloud models (`cloud_ok`, agent runtime `cloud`) or only by local
   models (`local_only`, runtime `local`)? Claude Code and Codex send code to their provider, so they are `cloud`.

Summarise the plan in five to ten lines (path, ports, URLs, users, directories) and get a "yes" before Phase 3.

## Phase 3 – Install the server

Get the repository if this directory is not a checkout yet:
`git clone https://github.com/taskbrigade/taskbrigade.git ~/taskbrigade`. GitHub `main` always equals the latest
release; to pin a version clone the release tag instead (`git clone --branch v<version> …`, e.g. `v0.13.3`).

**Native** – follow `docs/quickstart.md` steps 0–6:

| Step | Check |
| --- | --- |
| 0 Packages, 1 MariaDB database and user | `sudo mariadb -e 'SELECT VERSION();'` ≥ 10.5 |
| 2 Gitea (if new) – the user creates the service token and the Gitea admin token in the Gitea UI | `curl -fsS <gitea>/api/v1/version` returns 200 |
| 3 `sudo … deploy/install.sh` with the environment variables for DB host/port/name/user and `GITEA_URL` (plus `TAILSCALE_SERVE=1` or `LAN_UI_NETWORK=…` if chosen) | `systemctl is-active taskbrigade`; `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/healthz` is 503 (setup mode) |
| 4 Bootstrap password (`tbr-admin hash-password`, `tbr-admin config set BOOTSTRAP_ADMIN_PASSWORD_HASH --restart`) – the user runs both | login as `admin` works |
| 5 *Admin → Database* in the UI (user) | `/healthz` reports `"db":"ok"` |
| 6 *Admin → Settings*: Gitea URL, tokens, owner, TaskBrigade URL (user) | **Test Gitea connection** names the token's user |

Remind the user to back up `/var/lib/taskbrigade/master.key` somewhere other than the database backup.

**Docker Compose** – follow `docs/docker.md`, "Quickstart" steps 1–2. Install Docker Engine with the Compose plugin
from Docker's official Ubuntu instructions (<https://docs.docker.com/engine/install/ubuntu/>) if it is missing, and
ask whether to add the user to the `docker` group. Generate the three passwords in `.env` with `openssl rand` – the
user needs `TBR_INITIAL_ADMIN_PASSWORD` for the first login, so show them where it is instead of printing it.
Recommend the public release image: set `TBR_IMAGE=ghcr.io/taskbrigade/taskbrigade:<version>` (the version of the
checkout, see `pyproject.toml`) and `TBR_PULL_POLICY=always` in `.env`, so Compose pulls instead of building. Building
locally from the checkout (leave both unset) is the alternative, e.g. when the user wants to change the code.
Check: `docker compose ps` shows all services healthy, `docker compose logs gitea-init` ends with "TaskBrigade +
Gitea are ready." (profile `gitea`), and `curl -fsS http://127.0.0.1:8090/healthz` succeeds.

## Phase 4 – Connect the first agent

Follow the quickstart steps 7–8 (native) or the Docker quickstart step 3, and `docs/cli-agents.md` for Codex/OpenCode:

1. The user creates the agent in the UI (*Admin → Users & agents → New agent*): name and kind (`claude-code` /
   `claude_code`, `codex` / `codex`, `opencode` / `opencode`), role `worker`, runtime as agreed in Phase 2. The page
   shows a one-time join code (15 minutes, single use) and two commands.
2. As the agent's login user: install and log in to the agent CLI if needed (Claude Code:
   <https://code.claude.com/docs/en/setup>; Codex and OpenCode: their official docs, see `docs/cli-agents.md`). The
   login is interactive – the user does it.
3. The user runs the two commands from the UI page (they contain the join code):
   `curl -fsSL <taskbrigade-url>/install.sh | sh` and `tbr join <taskbrigade-url> tmj_… --dir ~/taskbrigade-work/<agent> --setup <agent>`.
   On the server itself the URL must be `http://127.0.0.1:8090` (join codes and Git credentials are only released to
   loopback or over HTTPS). Then `export PATH="$HOME/.local/bin:$PATH"` and `sudo loginctl enable-linger "$USER"`.
4. Check: `tbr doctor --work-root ~/taskbrigade-work/<agent>` reports no errors. Explain every warning.

If the agent is the program you are running in right now, say so: it will later be started by the runner timer in a
tmux window, independent of this session.

## Phase 5 – First task end to end

Guide the user through the quickstart step 9 (project with a new Gitea repo, status `active`, privacy matching the
agent's runtime; a **defined** task assigned to the agent, with description and acceptance criteria). Then run
`tbr run --work-root ~/taskbrigade-work/<agent>` once and show how to watch it (`tmux attach -t taskbrigade`). The
installation is done when the task reaches review with a pull request in Gitea and the user has approved it in the UI.
If nothing is claimed, read `~/taskbrigade-work/<agent>/logs/runner.log` and use the quickstart's troubleshooting table.

## Phase 6 – Adapt and explain

Offer the follow-ups that fit the user's answers, one at a time, and do only what they choose:

- Access from other machines: Tailscale serve or LAN UI (`docs/operations.md`; Docker: "Network access" in
  `docs/docker.md`).
- More agents, also on other machines (`docs/cli-agents.md`), desktop apps via MCP (`docs/desktop-apps-mcp.md`),
  agent frameworks such as Hermes or OpenClaw (`docs/agent-frameworks.md`).
- Models per task, parallel limits, runner settings (`docs/cli-agents.md`).
- Backups and updates: native `sudo deploy/update.sh`, backups in `/var/backups/taskbrigade/`; Docker
  `deploy/docker/backup.sh` (`docs/operations.md`, `docs/docker.md`). Test a restore once.
- Telegram notifications, users and roles (`docs/operations.md`, `docs/getting-started.md`).
- Other settings: `tbr-admin config list` (native) or `.env` (Docker); every setting is documented in `.env.example`.

Answer questions about TaskBrigade from the repository (`README.md`, `docs/`, `SECURITY.md`, `.env.example`, the
`--help` of `tbr` and `tbr-admin`). If the answer is not there, say so instead of guessing.

## Finish

End with a short summary for the user:

- what was installed where (paths, services or containers, ports, URLs),
- the agent(s), their work directories and the timer,
- where the secrets are (file paths only, never values) and what to back up (`master.key`, database backups),
- the next things to do, with the guide for each (`docs/getting-started.md` for daily use).

Update `~/taskbrigade-install-notes.md` with the same summary.
