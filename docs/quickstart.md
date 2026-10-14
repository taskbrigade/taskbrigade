<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# TaskBrigade quickstart

From a fresh Ubuntu 24.04 server to the first pull request opened by an agent (Claude Code, via the runner). In
short: install the server → create the agent in the UI → `curl …/install.sh | sh` → `tbr join … --setup …` →
`tbr doctor`. No Docker – for containers (TaskBrigade, MariaDB and optionally Gitea with Docker Compose, also on macOS and Raspberry Pi) see [Docker Compose](docker.md). About 15 minutes if you already have Gitea and Claude Code; a few minutes more if you install them in the steps below.

You do not edit `deploy/install.sh`. Paths and hosts are environment variables. The script refuses to run as a normal user, and it stops with a concrete install command when Python 3.12, uv, git, systemd, or a MariaDB client older than 10.5 is missing.

The UI labels below are German; that is the current interface. This guide is the English map.

## What you are installing

| Piece | Where it runs | Default |
| --- | --- | --- |
| TaskBrigade (uvicorn) | systemd service `taskbrigade`, user `taskbrigade` | `http://127.0.0.1:8090` |
| MariaDB ≥ 10.5 | this host or another host | local socket port **3306** |
| Gitea | this host or another host | `http://127.0.0.1:3001` |
| Claude Code runner | a normal login (not the `taskbrigade` system user) | systemd user timer, every 10 minutes |

Tailscale is optional. Without it, open the UI from the server itself and point the agent at `http://127.0.0.1:8090`. Do not publish port 8090 on the internet. See [SECURITY.md](../SECURITY.md).

## 0. Packages

```bash
sudo apt-get update
sudo apt-get install -y python3.12 git curl ca-certificates mariadb-server mariadb-client tmux
python3.12 --version    # must print 3.12.x
mariadb --version       # must say MariaDB and a version >= 10.5
```

`install.sh` installs `python3.12`, `git`, `curl`, and `mariadb-client` itself when they are missing. It does **not** install the MariaDB server, tmux, Gitea, or Claude Code. Those are here because the rest of this guide needs them.

## 1. MariaDB (about a minute)

Ubuntu's server listens on port 3306 as user `root` via the unix socket.

```bash
sudo mariadb <<'SQL'
CREATE DATABASE IF NOT EXISTS taskbrigade CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'taskadmin'@'localhost' IDENTIFIED BY 'replace-with-a-long-password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES, LOCK TABLES
  ON taskbrigade.* TO 'taskadmin'@'localhost';
FLUSH PRIVILEGES;
SQL
```

Use a real password. You will type it into the UI later; it does not belong in `.env` or in git. A remote MariaDB is fine too: create the same grants for the app server's address (see the comments in `deploy/mariadb-setup.sql`) and pass `TBR_DB_HOST` / `TBR_DB_PORT` in the next step.

Check the server version:

```bash
sudo mariadb -e 'SELECT VERSION();'
```

Anything below 10.5 will not work. MySQL will not work.

## 2. Gitea

Skip this if you already have a Gitea you can reach. Otherwise follow [Install from binary](https://docs.gitea.com/installation/install-from-binary) and substitute the current stable version for `VER`. SQLite is enough for this walkthrough; Gitea does not have to share MariaDB with TaskBrigade.

```bash
VER=1.22.6   # replace with the current stable version from the Gitea docs
curl -fsSL -o /tmp/gitea "https://dl.gitea.com/gitea/${VER}/gitea-${VER}-linux-amd64"
sudo install -m 0755 /tmp/gitea /usr/local/bin/gitea
id git &>/dev/null || sudo useradd --system --create-home --shell /bin/bash git
sudo tee /etc/systemd/system/gitea.service >/dev/null <<'EOF'
[Unit]
Description=Gitea
After=network.target

[Service]
Type=simple
User=git
WorkingDirectory=/home/git
ExecStart=/usr/local/bin/gitea web --port 3001
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now gitea
```

Open `http://127.0.0.1:3001`, finish the installer (database: SQLite3, it also creates the first Gitea admin), and
create:

1. User `taskbrigade`. Personal access token with scopes `write:repository`, `write:issue`, and `write:user` (repos in that user's namespace). This is the **service** token. It does not need to be a Gitea admin.
2. As the Gitea admin: a personal access token with scopes `write:admin` and `read:user`. This is the
   **Gitea admin token**; with it TaskBrigade creates a Gitea user and token for every new agent (step 7). It is
   only used for `POST /admin/users`; the agents get no admin rights. Without it, create the agent users yourself
   ([manual setup](#a1-gitea-user-for-the-agent)).

Confirm the probe TaskBrigade will use:

```bash
curl -fsS http://127.0.0.1:3001/api/v1/version
```

HTTP 200 is required. If this fails, `install.sh` aborts when you pass this URL. Leaving `GITEA_URL` on the example host (`gitea.example.internal`) only prints a warning, and agents cannot open pull requests until you set a real URL.

## 3. Install TaskBrigade

Clone this repository, then:

```bash
cd /path/to/taskbrigade
sudo TBR_DB_HOST=127.0.0.1 TBR_DB_PORT=3306 TBR_DB_NAME=taskbrigade TBR_DB_USER=taskadmin \
  GITEA_URL=http://127.0.0.1:3001 \
  deploy/install.sh
```

What that does, in order, on a fresh Ubuntu 24.04:

1. **Prerequisites.** Installs missing apt packages (`python3.12`, `git`, `curl`, `mariadb-client`). If `uv` is not on `PATH`, downloads it to `/usr/local/bin` (`TBR_INSTALL_UV=0` refuses the download). Then checks Python **3.12** (not 3.13), `uv`, `git`, `systemctl`, and MariaDB client **≥ 10.5**. A MySQL client, or MariaDB 10.4, stops the script with the install command.
2. **User and directories.** System user `taskbrigade`, checkout `/opt/taskbrigade/app`, state `/var/lib/taskbrigade` (mode 0700), config `/etc/taskbrigade`.
3. **Checkout.** Clones or fast-forwards branch `main`. `REPO_URL` defaults to the checkout you launched the script from. `TBR_BASE` moves the prefix (default `/opt/taskbrigade`); the systemd units are rewritten to match.
4. **Dependencies.** `uv sync --frozen --no-dev` as user `taskbrigade`.
5. **Configuration.** Copies `.env.example` to `/etc/taskbrigade/taskbrigade.env` if that file is absent, generates `SECRET_KEY`, and applies the variables you passed (`GITEA_URL`, `TBR_DB_*`, `UI_ALLOWED_LOGINS`, `BOOTSTRAP_ADMIN_ACTOR`, `TZ_DISPLAY`). `LAN_UI_NETWORK=192.168.1.0/24` also sets `TBR_BIND_HOST=0.0.0.0` and `LAN_UI_NETWORKS`. Creates `master.key` if missing.
6. **Gitea probe and Tailscale note.** A real `GITEA_URL` must return HTTP 200 from `/api/v1/version`. `TAILSCALE_SERVE=1` runs `tailscale serve` and fails clearly if Tailscale is absent. Otherwise Tailscale is reported as optional.
7. **systemd.** Enables `taskbrigade.service` and a daily backup timer (02:30). The backup fails until a database password exists; that first failure is expected.
8. **Health.** `GET /healthz` should be **HTTP 503** (setup mode) until step 5 below.

Re-running the script is safe. It will not overwrite an existing env file's `SECRET_KEY`, but it will apply any of the variables you pass.

Optional LAN UI (passwords and cookies travel in cleartext; the API stays blocked for that network):

```bash
sudo LAN_UI_NETWORK=192.168.1.0/24 deploy/install.sh
```

Optional Tailscale HTTPS, after `tailscale up` and enabling HTTPS certificates in the Tailscale admin console:

```bash
sudo TAILSCALE_SERVE=1 deploy/install.sh
```

## 4. Bootstrap password

The UI login name for the first password is `admin`.

```bash
sudo -u taskbrigade /opt/taskbrigade/app/.venv/bin/tbr-admin hash-password
sudo /opt/taskbrigade/app/.venv/bin/tbr-admin config set BOOTSTRAP_ADMIN_PASSWORD_HASH --restart
```

The second command asks for the value without echoing it: paste the printed hash (only the hash, no quotes). It
writes the env file the service reads, refuses anything that is not an Argon2 hash and restarts `taskbrigade`.

`tbr-admin config` works the same for every other setting, without knowing where the file lives:
`config set KEY VALUE` (unknown keys and invalid values are refused, a restart is offered), `config get KEY`,
`config list` (all settings, file value or default, secrets masked) and `config unset KEY`.

`BOOTSTRAP_ADMIN_ACTOR` defaults to `admin`. The first migration creates a human admin with that name (and no agents), and the bootstrap login is mapped onto it after the schema exists. An existing installation that already has a different admin actor must set `BOOTSTRAP_ADMIN_ACTOR` to that name, or the bootstrap login will not map onto the existing user.

## 5. Database in the UI

Open `http://127.0.0.1:8090` and sign in as `admin` with the password you just hashed.

**Admin → Datenbank:** host `127.0.0.1`, port `3306`, database `taskbrigade`, user `taskadmin`, the password from step 1. Then **Verbindung testen**, **Speichern**, **Schema aktualisieren**. No restart. `/healthz` should switch from 503 to a healthy response (`"db":"ok"`).

Back up `/var/lib/taskbrigade/master.key` somewhere that is not the database backup. Without it the saved password and tokens cannot be decrypted.

## 6. Gitea in the UI

**Admin → Einstellungen:**

- Gitea-URL: `http://127.0.0.1:3001`
- Gitea-Service-Token: the `taskbrigade` token from step 2
- Standard-Owner/Org: `taskbrigade`
- Gitea-Admin-Token: the Gitea admin token from step 2
- TaskBrigade-URL: `http://127.0.0.1:8090` (or your `https://<name>.ts.net` if you enabled Tailscale). This is what shows up in pull-request links and in the install command for agents.

Save, then **Gitea-Verbindung testen**. It should name the Gitea user that owns the token.

## 7. Create the Claude Code agent in the UI

A fresh installation contains only the admin – create the agent yourself.

**Admin → Benutzer & Agenten → Neuer Agent** (users & agents → new agent): name `claude-code`, kind `claude_code`,
role `worker`. **Runtime** `local`: a Claude Code process on this server counts as local, and a `local_only` project
refuses a `cloud` actor. (Choose `cloud` only if the project privacy will be `cloud_ok`.) Then **Agent und
Einmal-Code anlegen** (create agent and join code).

With the Gitea admin token from step 6, TaskBrigade creates the Gitea user `claude-code` and its token itself. The
next page shows a one-time join code (valid for 15 minutes, usable once) and the two commands for step 8:

```bash
curl -fsSL http://127.0.0.1:8090/install.sh | sh
tbr join http://127.0.0.1:8090 tmj_… --dir ~/taskbrigade-work/claude-code --setup claude-code
```

Copy them; the code is not shown again (a new code can be created on the agent's page, „Einmal-Code für tbr join /
Gitea-Einrichtung“). No API token is copied by hand: `tbr join` fetches it.

## 8. Agent account: one command

Use a normal user who will run Claude Code (`$USER` below). Do not use the system user `taskbrigade`.

Install Claude Code and sign in once, so later unattended runs are not stuck on a browser login ([Claude Code setup](https://code.claude.com/docs/en/setup)):

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude
```

Then run the two commands from step 7:

```bash
curl -fsSL http://127.0.0.1:8090/install.sh | sh     # tbr in the server's version (uv, ~/.local/bin)
export PATH="$HOME/.local/bin:$PATH"
tbr join http://127.0.0.1:8090 tmj_… --dir ~/taskbrigade-work/claude-code --setup claude-code
sudo loginctl enable-linger "$USER"                   # the timer also fires while you are logged out
tbr doctor --work-root ~/taskbrigade-work/claude-code
```

- `tbr join` exchanges the code for a fresh API token, writes `~/taskbrigade-work/claude-code/.taskbrigade.toml`
  (mode 600) and checks `tbr whoami` and the Git access. On the same host as TaskBrigade the URL **must** be loopback,
  because join codes and Git credentials are only released to a loopback peer (or over HTTPS through
  `tailscale serve`).
- `--setup claude-code` adds the `[runner]` section, checks tmux and `claude` and installs a systemd user timer
  `tbr-runner-claude-code.timer` (every 10 minutes).
- `tbr doctor` checks configuration, connection, tbr version, token, Git access, runner and timer, one line each with
  a hint when something is missing. It exits 1 only on errors.

An agent on another machine uses `https://<your-tailnet-name>.ts.net` instead of `http://127.0.0.1:8090` in both
commands ([CLI agents on a second machine](cli-agents.md)). While you are still at the keyboard you can run one pass
immediately:

```bash
tbr run --work-root ~/taskbrigade-work/claude-code
```

No task assigned yet: the command exits without starting Claude. That is the cheap poll.

## 9. First project and first task

In the UI, **Projekte → Neues Projekt**:

- Key `DEMO` (2–8 letters or digits), name `Demo`
- Status `active` (an idea or a paused project is never claimed)
- Privacy `nur lokale Agenten` (`local_only`), matching the runtime you set in step 7
- Gitea: **neues Gitea-Repo anlegen**, owner `taskbrigade`, repo name `demo`

Then **Neuer Task**:

- Projekt `DEMO`
- Status **Definiert – Agent darf abholen** (the UI default is a draft, and drafts are never claimed)
- Zuweisen an `claude-code`
- Titel `Add a README greeting`
- Beschreibung: `Create README.md with a single line: Hello from TaskBrigade.`
- Akzeptanzkriterien: `README.md exists on the task branch and contains that line. Submit the result as a pull request.`

Defined tasks need both a description and acceptance criteria. Prio does not matter. A due date does not matter; the runner does not look at it.

Assigning the agent adds them as a project member and asks Gitea to add `claude-code` as a collaborator.

## 10. Watch the pull request

```bash
tbr run --work-root ~/taskbrigade-work/claude-code
```

Or wait for the 10-minute timer. The runner claims the task, clones `demo` under `~/taskbrigade-work/claude-code/DEMO`, and starts Claude Code in a tmux window named `taskbrigade`:

```bash
tmux attach -t taskbrigade
```

When Claude finishes it runs `tbr submit`. TaskBrigade opens the pull request on Gitea and moves the task to review. You merge it in the UI (or on Gitea); agents never push to `main`.

If the runner claims nothing, read `~/taskbrigade-work/claude-code/logs/runner.log`. Typical reasons: the task is still a draft, the project is not `active`, the agent is `cloud` on a `local_only` project, or Gitea credentials were rejected while cloning.

## Afterwards

- Update the app: `sudo deploy/update.sh` (backup, then schema upgrade, then restart). It checks the same Python, uv, and MariaDB client versions. A Gitea outage is only a warning.
- Logs: `journalctl -u taskbrigade -f`
- Backups: `/var/backups/taskbrigade/`, kept 14 days (`TBR_BACKUP_KEEP_DAYS`). They start working once the database password is saved.
- Settings: `sudo /opt/taskbrigade/app/.venv/bin/tbr-admin config set KEY VALUE` (or `get`, `list`, `unset`).
- Emergency, as user `taskbrigade`: `tbr-admin set-db-password`, `tbr-admin db-status`, `tbr-admin upgrade`, `tbr-admin reset-admin-password`.
- Every setting the service reads is documented in [`.env.example`](../.env.example).

## When a check fails

| Message | What to do |
| --- | --- |
| `run this script as root` | `sudo deploy/install.sh` |
| `Python 3.12 is required` | `sudo apt-get install -y python3.12`. 3.13 is not supported. |
| `uv was not found` | Re-run online, or install uv yourself and pass `UV=/path/to/uv`. |
| `MariaDB client … older than 10.5` or `not a MariaDB client` | `sudo apt-get install -y mariadb-client`. Do not point TaskBrigade at MySQL. |
| `Gitea check failed` | `curl -fsS "$GITEA_URL/api/v1/version"` from the server. Fix the URL or start Gitea. To install the app first, drop `GITEA_URL` so the example host is only a warning. |
| `TAILSCALE_SERVE=1 but the tailscale command was not found` | Install Tailscale, or omit `TAILSCALE_SERVE`. |
| `Unknown setting …` or `invalid value` from `tbr-admin config set` | `tbr-admin config list` shows the valid keys; the message names what the value must look like. |
| `No permission to change /etc/taskbrigade/taskbrigade.env` | Run `tbr-admin config set` with `sudo`. |
| `tbr join`: `join_code_expired` / `join_code_used` | Create a new code on the agent's page and run `tbr join` again. |
| `tbr doctor`: Git access error | The agent has no Gitea user: add the Gitea admin token (step 6) and create a new join code, or [link one manually](#a1-gitea-user-for-the-agent). |
| `/healthz` is 503 after the UI save | The password or the schema step did not stick. **Admin → Datenbank** again, or `sudo -u taskbrigade tbr-admin db-status`. |
| Runner log: skipped, Entwurf | Set the task to **Definiert** and assign `claude-code`. |
| Runner log: privacy / cloud | Set the actor runtime to `local`, or the project to `cloud_ok`. |

## Next steps

- Daily use – writing tasks, inbox, reviews, sprints, Telegram: [getting-started.md](getting-started.md).
- More agents, on other machines: [CLI agents on a second machine](cli-agents.md),
  [desktop apps via MCP](desktop-apps-mcp.md), [agent frameworks with a wake gate](agent-frameworks.md).
- Run it in Docker instead, with backup/restore and updates: [docker.md](docker.md).
- Tailscale, LAN access, backups and several users: [operations.md](operations.md).
- How tasks move, leases, conflicts and privacy: [architecture.md](architecture.md).

## Appendix: Manual setup

The steps the quickstart automates, for setups without a Gitea admin token, without access to `/install.sh`, or when
you want to see every piece.

### A.1 Gitea user for the agent

In Gitea, create the user `claude-code` and a personal access token with scopes `write:repository` and `read:user`
(the **agent** token). On the agent's setup page in TaskBrigade („Gitea-User manuell verknüpfen“, link a Gitea user
manually) enter the username, that token and the user's email, then **Prüfen und speichern** (check and save). The
form reports whether Gitea accepted the token. `tbr join` works the same afterwards.

### A.2 API token and identity file by hand

Instead of `tbr join`: on the agent's page **Token rotieren** (rotate token) and copy the TaskBrigade API token – it
is shown once. Write `~/taskbrigade-work/claude-code/.taskbrigade.toml` and `chmod 600` it:

```toml
url = "http://127.0.0.1:8090"
token = "PASTE_THE_ROTATED_TOKEN"

[runner]
command = ["claude", "--permission-mode", "auto", "{prompt}"]
mode = "tmux"
tmux_session = "taskbrigade"
```

`tbr whoami` in that directory should report `claude-code`.

### A.3 `tbr` from the checkout

Instead of `curl …/install.sh | sh`, install the CLI from the deployed checkout:

```bash
uv tool install --python 3.12 --from /opt/taskbrigade/app taskbrigade
install -d -m 700 ~/taskbrigade-work/claude-code
```

### A.4 Runner timer from the template

Instead of `--setup claude-code` (or `tbr setup claude-code --work-root ~/taskbrigade-work/claude-code` later):

```bash
mkdir -p ~/.config/systemd/user
cp /opt/taskbrigade/app/deploy/agents/runner/tbr-runner@.service ~/.config/systemd/user/
cp /opt/taskbrigade/app/deploy/agents/runner/tbr-runner@.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tbr-runner@claude-code.timer
sudo loginctl enable-linger "$USER"
```

The template expects the work directory `~/taskbrigade-work/<agent>`; its `PATH` includes `~/.local/bin` and
`~/.npm-global/bin` (`deploy/agents/runner/tbr-runner@.service`). `tbr doctor` accepts this timer too.

### A.5 Editing the env file directly

`tbr-admin config set` only edits `/etc/taskbrigade/taskbrigade.env` (mode 0640, `root:taskbrigade`). You can edit it
with `sudoedit` instead: one `KEY=value` per line, the Argon2 hash without quotes, then
`sudo systemctl restart taskbrigade`. Typos in key names are ignored silently by the service – `tbr-admin config list`
shows what it will read.
