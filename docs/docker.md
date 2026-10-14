<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# Docker Compose

TaskBrigade and MariaDB as containers, optionally with a Gitea server (profile `gitea`). This is an alternative to
the native installation ([quickstart](quickstart.md), `deploy/install.sh`), which stays supported and unchanged.
Agents (`tbr`, the runner, `tbr mcp`) do not run in a container; install them on the agent machines as described in
[CLI agents](cli-agents.md) and [desktop apps via MCP](desktop-apps-mcp.md).

The UI labels below are the English ones (the default). With `UI_LANGUAGE=de` in `.env`, *Admin → Settings* or the
EN/DE switch in the top bar the UI is German; the translations are in `app/locales/de.json`.

- [Quickstart: first agent task in 5 minutes](#quickstart-first-agent-task-in-5-minutes)
- [Variants](#variants) – with or without the Gitea profile, with or without Tailscale
- [Bundled Gitea and demo project](#bundled-gitea-and-demo-project-profile-gitea)
- [Backup and restore](#backup-and-restore), [updates](#updates)
- [Network access](#network-access), [hardware requirements](#hardware-requirements)
- [Changing settings](#changing-settings), [troubleshooting](#troubleshooting)
- [Test scenario: Mac mini (Apple Silicon)](#test-scenario-mac-mini-apple-silicon)
- [Appendix: Manual setup](#appendix-manual-setup) – agents without a Gitea admin token or without `/install.sh`

## Quickstart: first agent task in 5 minutes

One machine runs Docker and an agent (Claude Code). Gitea comes from the Compose profile `gitea`. Requirements:
Docker Engine with Compose v2.24 or newer (Docker Desktop or OrbStack on macOS work too), `git`, `openssl`, and for
the agent `uv`, `tmux` and [Claude Code](https://code.claude.com/docs/en/setup), signed in once.

### 1. Configure (1 minute)

```bash
git clone https://github.com/taskbrigade/taskbrigade.git
cd taskbrigade
cp .env.example .env
```

Set these values in `.env` (each key already exists in the file):

| Variable | Value |
| --- | --- |
| `TBR_DB_PASSWORD` | Password of the MariaDB user `TBR_DB_USER`, e.g. the output of `openssl rand -base64 24`. |
| `MARIADB_ROOT_PASSWORD` | Another random value (used only inside the database container and by the backup). |
| `TBR_INITIAL_ADMIN_PASSWORD` | Initial UI password (min. 10 characters) for the human `admin`; also the Gitea admin's password. |
| `TBR_IMAGE`, `TBR_PULL_POLICY` | Recommended: the public release image, `TBR_IMAGE=ghcr.io/taskbrigade/taskbrigade:0.13.3` (replace with the version you cloned; tags `<version>`, `<major>.<minor>` and `latest`, amd64 + arm64) and `TBR_PULL_POLICY=always`. Without them Compose builds the image from the checkout (`taskbrigade:local`), which you need only for your own changes. |

Without `SECRET_KEY` a random session key is created once in the state volume.

### 2. Start (1 minute)

```bash
docker compose --profile gitea up -d --wait   # pulls the image (builds it without TBR_IMAGE); ready after about a minute
docker compose logs gitea-init                # "TaskBrigade + Gitea are ready."
```

The profile starts Gitea on <http://localhost:3000> (admin `giteaadmin`), creates the Gitea service user
`taskbrigade` with its token and stores Gitea's addresses, token and owner in TaskBrigade. It also sets up a demo
agent, project and task ([details](#bundled-gitea-and-demo-project-profile-gitea)); the quickstart uses Claude Code
instead.

### 3. Agent (2 minutes)

The flow: create the agent in the UI → `curl …/install.sh | sh` → `tbr join … --setup …` → `tbr doctor`.

A fresh installation contains only the admin (plus the demo agent from `gitea-init`), so create the agent first.
Open <http://127.0.0.1:8090>, sign in as `admin` with `TBR_INITIAL_ADMIN_PASSWORD` and go to *Admin → Users &
agents → New agent*: name `claude-code`, kind `claude_code`, runtime `cloud` (Claude
Code sends code to Anthropic), role `worker`, then **Create agent and join code**.
`gitea-init` stored a Gitea admin token, so TaskBrigade creates the Gitea user `claude-code` and its token itself (no
admin rights). The next page shows the join code once and the commands to copy. On the same machine:

```bash
curl -fsSL http://127.0.0.1:8090/install.sh | sh     # tbr in the server's version, from the image (/download/)
export PATH="$HOME/.local/bin:$PATH"
tbr join http://127.0.0.1:8090 tmj_… --dir ~/taskbrigade-work/claude-code --setup claude-code
tbr doctor --work-root ~/taskbrigade-work/claude-code
```

`tbr join` fetches a fresh API token for `claude-code`, writes `~/taskbrigade-work/claude-code/.taskbrigade.toml`
(mode 600) and checks `tbr whoami` and the Git access; `--setup claude-code` adds the runner section, checks tmux and
`claude` and installs the timer (systemd user timer, macOS launchd). Without a systemd user session use
`--setup claude-code --no-timer` and start passes with `tbr run`. `tbr doctor` checks connection, token, Git access,
runner and timer and names the fix for anything missing. The code is valid once for 15 minutes. On the Docker host
the URL must be `127.0.0.1`: requests through the published port come from the Compose gateway, which TaskBrigade
trusts like loopback (see [network access](#network-access)); join codes, like Git credentials, are only redeemed
from trusted peers or over HTTPS. Without a Gitea admin token, or to write the identity file yourself, see the
[manual setup](#appendix-manual-setup).

### 4. First task (1 minute)

In the UI, **Projects → New project**: key `HELLO`, status `active`, privacy `cloud_ok` (the
`claude-code` agent has runtime `cloud`), Gitea **create a new Gitea repo** with
owner `taskbrigade`, repo `hello`. Then **New task**: project `HELLO`, status **Defined**,
assigned to `claude-code`, title `Add a README greeting`, description `Create README.md with the line "Hello from
TaskBrigade".`, acceptance criteria `README.md on the task branch contains that line.`

```bash
cd ~/taskbrigade-work/claude-code
tbr run --work-root ~/taskbrigade-work/claude-code     # claims the task and starts Claude Code in tmux
tmux attach -t taskbrigade
```

When Claude Code runs `tbr submit`, TaskBrigade opens the pull request in Gitea and the task moves to review.
**Approve** in the UI merges it. For a timer that polls every 10 minutes and for more agents see
[CLI agents](cli-agents.md); for Claude Desktop see [desktop apps via MCP](desktop-apps-mcp.md).

## Variants

### With or without the Gitea profile

| | Profile `gitea` | Your own Gitea |
| --- | --- | --- |
| Start | `docker compose --profile gitea up -d` (or `COMPOSE_PROFILES=gitea` in `.env`) | `docker compose up -d` |
| Gitea | `gitea/gitea:1.24` (`TBR_GITEA_IMAGE`), SQLite, volume `taskbrigade-gitea` | as it is |
| Service user, token, settings | created by `gitea-init` | in Gitea's web UI and *Admin → Settings*, as in the [native quickstart](quickstart.md#2-gitea) |
| Agent users and tokens | created by *New agent* with the Gitea admin token from `gitea-init` (quickstart step 3) | in Gitea's web UI, or with a “Gitea admin token” under *Admin → Settings* |
| Backup | `deploy/docker/backup.sh` includes the Gitea volume | back up your Gitea yourself |

TaskBrigade calls Gitea's API; Gitea never calls TaskBrigade, so no webhook setup is needed. With your own Gitea,
check that the container reaches it (put in your URL):

```bash
docker compose exec taskbrigade python -c "import urllib.request; print(urllib.request.urlopen('http://gitea.example.internal:3001/api/v1/version').read())"
```

### With or without Tailscale

Without Tailscale, the UI and API are published on `127.0.0.1:8090` only; agents on the Docker host use
`http://127.0.0.1:8090`. With Tailscale, either add the sidecar `compose.tailscale.yaml` (no Tailscale on the host)
or run `tailscale serve` on the Docker host; agents on other machines then use the tailnet HTTPS address. Both, and
LAN access without Tailscale, are described under [network access](#network-access). With the Gitea profile, agents
on other machines also need a reachable Gitea address (`GITEA_PUBLIC_URL`, see
[below](#internal-and-public-gitea-address)).

## Bundled Gitea and demo project (profile `gitea`)

Without an existing Gitea, start the stack with the profile `gitea`:

```bash
docker compose --profile gitea up -d
docker compose logs -f gitea-init      # ends with "TaskBrigade + Gitea are ready." and the demo agent's token
```

No further values are needed in `.env`. The profile adds three services:

| Service | Purpose |
| --- | --- |
| `gitea` | Official Gitea image with SQLite in the volume `taskbrigade-gitea`. HTTP on `127.0.0.1:3000` (`TBR_GITEA_PUBLISH`), SSH on `127.0.0.1:2222` (`TBR_GITEA_SSH_PUBLISH`, never port 22 of the host). Registration is disabled. |
| `gitea-admin` | One-shot: creates the Gitea admin `giteaadmin` (`TBR_GITEA_ADMIN_USER`) with the Gitea CLI. Password: `TBR_GITEA_ADMIN_PASSWORD`, default `TBR_INITIAL_ADMIN_PASSWORD`. |
| `gitea-init` | One-shot (`python -m app.gitea_init`): creates the Gitea user `taskbrigade` and its token, stores token, owner and both Gitea addresses in TaskBrigade (*Admin → Settings*), plus a token of the Gitea admin (`taskbrigade-admin`, scopes `write:admin`, `read:user`) as “Gitea admin token” so *New agent* creates Gitea users for agents, and – unless `TBR_GITEA_DEMO=0` – sets up the demo. |

The demo consists of the agent `demo-agent` (with its own Gitea user and token, runtime `local`), the project
`DEMO` owned by `admin` with the repository `taskbrigade/demo`, and the task `DEMO-0001` assigned to
`demo-agent`. Every step checks what exists, so `gitea-init` runs again on each `up` without creating duplicates;
an invalid service token is replaced. The demo agent's TaskBrigade API token is printed only on the run that
creates the agent – rotate it under *Admin → Users & agents* if the log is gone.

Try the demo on the Docker host (`tbr` installed as in [CLI agents](cli-agents.md)):

```bash
mkdir demo-agent && cd demo-agent
printf 'url = "http://127.0.0.1:8090"\ntoken = "tm_…"\n' > .taskbrigade.toml
tbr prepare --task DEMO-0001      # clones http://localhost:3000/taskbrigade/demo.git into a worktree
```

Then commit in the worktree, `git push -u origin HEAD` and `tbr submit DEMO-0001 --branch … --sha … --summary …`;
the pull request link points to `http://localhost:3000`. Sign in to Gitea as `giteaadmin` to look at it.

### Internal and public Gitea address

Inside the Compose network the app reaches Gitea as `http://gitea:3000`; agents on the host use
`http://localhost:3000`. TaskBrigade therefore has two settings:

| Setting | Used for |
| --- | --- |
| `GITEA_URL` (*Gitea URL*) | API calls of the app. The profile stores `http://gitea:3000`. |
| `GITEA_PUBLIC_URL` (*Public Gitea URL*) | Clone URLs, `tbr git-credential` (matches scheme, host and port exactly) and repository/PR links. Empty = `GITEA_URL`. |

`GITEA_PUBLIC_URL` in `.env` (default `http://localhost:3000`) also becomes Gitea's `ROOT_URL`; keep it in line with
`TBR_GITEA_PUBLISH`. For agents on other machines, publish Gitea on a reachable address and set `GITEA_PUBLIC_URL`
to it, e.g. `TBR_GITEA_PUBLISH=0.0.0.0:3000` and `GITEA_PUBLIC_URL=http://docker-host.example.internal:3000`, then
`docker compose --profile gitea up -d` again. The native installation needs neither: there both are the same
address and `GITEA_PUBLIC_URL` stays empty.

### Existing Gitea instead

Without the profile nothing changes: set `GITEA_URL` (and the token under *Admin → Settings*) as in the
[quickstart](quickstart.md). If that Gitea is reachable under different names for the container and for agents,
set `GITEA_PUBLIC_URL` as well. After using the profile, its values are stored in the database and override `.env`;
change them under *Admin → Settings* (or use *reset* there) when you switch to another Gitea.

## What happens on start

`python -m app.container` (the image entrypoint):

1. creates `master.key` and, without `SECRET_KEY`, `secret.key` in the state volume;
2. reads the database connection from `TBR_DB_*` – there is no setup mode and `db.json` is not used;
3. waits for MariaDB (`TBR_DB_WAIT_SECONDS`, default 120) and exits with an error after that;
4. runs the Alembic migrations (`upgrade head`) – updates migrate the schema automatically;
5. sets `TBR_INITIAL_ADMIN_PASSWORD` if needed and starts Uvicorn (one worker, `--no-proxy-headers`).

`TBR_INITIAL_ADMIN_PASSWORD` is applied only while the admin has no password yet; it never overwrites a password
changed in the UI. Remove it from `.env` after the first login. Instead of it you can set
`BOOTSTRAP_ADMIN_PASSWORD_HASH` (create the hash with
`docker compose run --rm --no-deps --entrypoint tbr-admin taskbrigade hash-password`). In `.env` the hash must be in
single quotes, `BOOTSTRAP_ADMIN_PASSWORD_HASH='$argon2id$…'`, otherwise Compose treats the `$` parts as variables.

The container runs as user `taskbrigade` (uid 10001). All services have health checks; `taskbrigade` starts only
once MariaDB is healthy, and its own check calls `/healthz`, which answers 200 only with a reachable database and a
current schema. Logs: `docker compose logs -f taskbrigade` (JSON lines).

## Data

| Volume | Content |
| --- | --- |
| `taskbrigade-db` | MariaDB data directory. |
| `taskbrigade-state` | `master.key` (encrypts Gitea tokens and secret settings) and `secret.key` (session signing). |
| `taskbrigade-gitea` | Profile `gitea` only: Gitea data (repositories, SQLite database, `app.ini`). |

`docker compose down` and `up` keep the volumes; `docker compose down -v` deletes them. With the profile, pass
`--profile gitea` to `down` as well, otherwise the Gitea container keeps running. Back up the database and the state
volume together: the database alone is useless for encrypted settings without `master.key` – use the scripts below.

## Backup and restore

```bash
deploy/docker/backup.sh                 # → backups/taskbrigade-docker-<UTC timestamp>.tar
deploy/docker/backup.sh /mnt/nas/tm     # other target directory (or TBR_BACKUP_DIR)
```

The archive contains `db.sql.gz` (`mariadb-dump --single-transaction` from the MariaDB container, taken while the
app keeps running), `state.tar.gz` (the state volume with `master.key`), `gitea.tar.gz` (only if the volume
`taskbrigade-gitea` exists; Gitea is stopped for those seconds) and `manifest.txt`. It is written with mode 0600
and holds `master.key`: store it like a password, and off the Docker host. `.env` is not included – keep a copy of
it with your passwords. `TBR_BACKUP_KEEP_DAYS=14` deletes older archives in the target directory. Daily, for
example with cron: `30 2 * * * cd /path/to/taskbrigade && deploy/docker/backup.sh >>backups/backup.log 2>&1`.
The scripts include the services of the profile `gitea` by themselves once its volume exists, whether you started
it with `--profile gitea` or `COMPOSE_PROFILES=gitea`.

Restore, into the same installation or a fresh one (new machine: clone, write `.env`, then restore – `up` is not
needed first):

```bash
deploy/docker/restore.sh backups/taskbrigade-docker-20260101-023000.tar   # asks for confirmation; --yes skips it
```

`restore.sh` stops `taskbrigade` (and `gitea`), starts MariaDB, recreates the database empty and loads the dump,
replaces the state volume and, if present in the archive, the Gitea volume, then starts everything with
`docker compose up -d --wait` (with the profile, `gitea-admin` and `gitea-init` run again and find everything set
up). The MariaDB user and its password come from the local `.env`; logins, API tokens and Gitea tokens are those of
the backup. A backup of an older version is migrated on that start.

## Updates

```bash
git pull                     # new source (scripts, compose.yaml); with the registry image also set the new TBR_IMAGE tag in .env
deploy/docker/update.sh      # backup, pull MariaDB/Gitea (and the TaskBrigade image), recreate; --no-backup skips the backup
```

`update.sh` runs `backup.sh`, then `docker compose pull` for the registry images, `docker compose build --pull`
for TaskBrigade and `docker compose up -d --wait`. The new `taskbrigade` container runs the migrations before it
serves requests; `--wait` returns once it is healthy. With the registry image (`TBR_IMAGE=ghcr.io/taskbrigade/taskbrigade:<version>`
and `TBR_PULL_POLICY=always`, as in the quickstart) the script pulls instead of building – the manual equivalent is
`docker compose pull && docker compose up -d`. A fixed `<version>` tag changes only when you edit it in `.env`; the
tags `<major>.<minor>` and `latest` move with each release. Without `TBR_IMAGE` the image is built locally from the
checkout. Remove old images with `docker image prune`. An installation from
before 0.8 is moved to the new names first ([below](#from-the-taskmeister-names-taskbrigade-08)). Going back to an older
version: restore the backup taken before the update with the older checkout or image.

### From the `taskmeister` names (TaskBrigade 0.8)

Before 0.8 the Compose project, its volumes and network were called `taskmeister` (`taskmeister-db`,
`taskmeister-state`, `taskmeister-gitea`, `taskmeister-tailscale-state`), the service and image `taskmeister`, and
`.env` used `TM_*` keys. The first `deploy/docker/update.sh` after `git pull` moves the installation over by
[`deploy/docker/migrate-names.sh`](../deploy/docker/migrate-names.sh) – when the volume `taskmeister-db` exists and
`taskbrigade-db` does not:

1. `.env`: `TM_<X>=` → `TBR_<X>=` (previous version `.env.bak-<time>`; an existing `TBR_<X>` wins). Defaults that
   changed are written explicitly so nothing changes for the existing data: `TBR_DB_NAME=taskmeister`,
   `TBR_DB_USER=taskmeister` (the database and its user in the MariaDB volume) and, with the Tailscale sidecar,
   `TBR_TS_HOSTNAME=taskmeister` (keeps the tailnet address the agents use).
2. A database dump of the running old stack: `backups/taskmeister-docker-migration-<time>.sql.gz`.
3. Stop and remove the old containers and the network `taskmeister` (the new one reuses its subnet).
4. Copy every old volume into the new one (`taskmeister-db` → `taskbrigade-db` and so on). **The old volumes stay
   untouched** – they are the way back.

Then `update.sh` builds and starts the stack under the new names as usual (the regular backup is skipped this once:
the migration took its own dump, and the old volumes still hold everything). Check with `docker compose ps` and
`curl -s http://127.0.0.1:8090/healthz`, then switch the agent machines
([cli-agents.md](cli-agents.md#from-the-names-taskmeister-to-taskbrigade-taskbrigade-08)). Once everything runs,
remove the old volumes: `docker volume rm taskmeister-db taskmeister-state taskmeister-gitea taskmeister-tailscale-state`
(only those that exist).

Way back while the old volumes exist: `docker compose down`, `cp .env.bak-<time> .env`, check out the previous
version (`git checkout <tag>`) and `docker compose up -d` – the old project starts on its unchanged volumes. Changes
made after the migration are then lost. A Compose file of your own that names the volumes needs the new names (or
`external: true` with the old ones). Backup archives from before 0.8 (`taskmeister-docker-*.tar`) restore as before.

## Network access

The app decides by the **direct connection peer** (Uvicorn runs with `--no-proxy-headers`). In a container that
peer is rarely `127.0.0.1`, so there are three ways to run it:

| Variant | Who reaches what | Set up |
| --- | --- | --- |
| **Tailscale sidecar** (recommended) | Tailnet: HTTPS, login by Tailscale identity, API. Docker host: UI and API on `127.0.0.1:8090`. | `compose.tailscale.yaml`, below |
| **Docker host only** (default) | Processes on the Docker host: UI and API on `127.0.0.1:8090`. Everything else: nothing. | nothing |
| **LAN without Tailscale** | LAN: UI and – only after an explicit opt-in – API over plain HTTP. | `TBR_PUBLISH`, `LAN_UI_NETWORKS`, `API_NETWORKS` |

Peers and their rights, as in the native installation ([operations](operations.md#access-rules)):

| Peer | UI | API (bearer token) | `Tailscale-User-Login`, `/agents/me/git-credentials` |
| --- | --- | --- | --- |
| loopback, `TRUSTED_PEER_NETWORKS` | yes | yes | yes |
| `API_NETWORKS` | yes | yes | no |
| `LAN_UI_NETWORKS` | yes | no (403 `lan_api_forbidden`) | no |
| anything else | 403 | 403 | no |

### Docker host only (default)

The UI and API are published on `127.0.0.1:8090` (`TBR_PUBLISH`). Connections through a published port do not
arrive from loopback inside the container but from the gateway of the Compose network. `TRUSTED_PEER_NETWORKS`
(default `172.31.250.1/32`, the gateway of the fixed subnet `172.31.250.0/24`) lets that gateway in like loopback,
so agents on the Docker host use `url = "http://127.0.0.1:8090"` exactly as on a native server. Other containers on
the network are not trusted. If the subnet collides with an existing network, change `TBR_DOCKER_SUBNET`,
`TBR_DOCKER_GATEWAY` and `TRUSTED_PEER_NETWORKS` together. If the UI answers 403, `docker compose logs taskbrigade`
shows the peer address Uvicorn sees.

You can also run `tailscale serve --bg --https=443 http://127.0.0.1:8090` on the Docker host itself, as with the
native installation. The sidecar below needs no Tailscale on the host.

### Tailscale sidecar

`compose.tailscale.yaml` adds a `tailscale/tailscale` container and moves TaskBrigade into its network namespace
(`network_mode: service:tailscale`). `tailscale serve` (config `deploy/docker/tailscale-serve.json`) terminates
HTTPS on the tailnet and forwards to `127.0.0.1:8090` inside that namespace, so `Tailscale-User-Login` arrives from
loopback and is trusted exactly as on a native server. Funnel (public internet) stays off.

1. In the Tailscale admin console enable *DNS → HTTPS Certificates* and create an auth key under
   *Settings → Keys* (reusable is not needed; a tagged key keeps the node from expiring with a user).
2. In `.env`:

   ```bash
   COMPOSE_FILE=compose.yaml:compose.tailscale.yaml
   TS_AUTHKEY=tskey-auth-…
   TBR_TS_HOSTNAME=taskbrigade          # tailnet name → https://taskbrigade.example.ts.net
   UI_ALLOWED_LOGINS=admin@example.org
   # TS_EXTRA_ARGS=--advertise-tags=tag:taskbrigade
   ```

3. `docker compose up -d` (or `docker compose -f compose.yaml -f compose.tailscale.yaml up -d` without
   `COMPOSE_FILE`; on Windows the separator in `COMPOSE_FILE` is `;`). The node state lives in the volume `taskbrigade-tailscale-state`; the auth key is only
   needed for the first login and can be removed from `.env` afterwards.
4. Sign in at `https://taskbrigade.example.ts.net`, map the Tailscale login to the actor `admin` in
   *Admin → Users & agents* and set the TaskBrigade URL under *Settings*, as in
   [operations](operations.md#tailscale). Agents on other tailnet machines use that URL in `.taskbrigade.toml`.

The sidecar uses userspace networking (`TS_USERSPACE=true`): no `/dev/net/tun` and no extra capabilities, which
also works on Docker Desktop. The port `TBR_PUBLISH` moves to the sidecar and keeps serving agents on the Docker
host. Pin a version with `TBR_TAILSCALE_IMAGE=tailscale/tailscale:v1.x.y` if you do not want `stable`.

### LAN without Tailscale

Publish the port on the LAN with `TBR_PUBLISH=0.0.0.0:8090`. Nothing is allowed until you name the networks:

- `LAN_UI_NETWORKS=192.168.1.0/24` – the UI over HTTP with password login. The API stays blocked.
- `API_NETWORKS=192.168.1.0/24` – also the API, for agents on other machines. Empty by default. **Bearer tokens
  then cross the network in cleartext**: anyone who can watch the traffic can take over that agent. Set it only for
  a network you trust, keep it as narrow as possible (e.g. single agent hosts `192.168.1.20/32,192.168.1.21/32`)
  and prefer Tailscale or TLS. The server logs `api_networks_cleartext` at startup as a reminder.

`API_NETWORKS` peers are not trusted like loopback: they cannot sign in with `Tailscale-User-Login`, and
`tbr git-credential` gets 403 `insecure_channel` because the Gitea token would travel in cleartext as well. Give
those agents their own Git credentials for Gitea (e.g. a Git credential store with their Gitea token), or use TLS.

On Linux with Docker Engine, LAN clients keep their own address through the published port. Docker Desktop (macOS,
Windows) and the userland proxy (e.g. IPv6 to an IPv4 container) forward connections from the gateway address
instead – there every LAN client would be trusted like loopback, regardless of `LAN_UI_NETWORKS` and
`API_NETWORKS`. Check the peer address in the logs before publishing beyond `127.0.0.1`, and keep `127.0.0.1` with
the sidecar on Docker Desktop. Never publish port 8090 to the internet.

### TLS reverse proxy

Without Tailscale, the recommended way to serve the LAN is a TLS reverse proxy (Caddy, nginx, Traefik) on the Docker
host that forwards to `127.0.0.1:8090` – keep `TBR_PUBLISH` at its default. The proxy arrives from the trusted
gateway, so its clients get UI, API and git credentials over HTTPS. The proxy **must remove the
`Tailscale-User-Login` request header**, otherwise any client could claim an allowed login:

```text
# Caddy
taskbrigade.example.internal {
    reverse_proxy 127.0.0.1:8090 {
        header_up -Tailscale-User-Login
    }
}

# nginx (inside the location block)
proxy_set_header Tailscale-User-Login "";
proxy_pass http://127.0.0.1:8090;
```

Login rate limits then see the proxy as one peer, and the network policy no longer distinguishes clients – restrict
access at the proxy or firewall. See [SECURITY.md](../SECURITY.md) for the trust model.

## Hardware requirements

Measured idle on amd64 after the quickstart: TaskBrigade about 85 MB RAM, MariaDB about 95 MB, Gitea about 110 MB.
Images take about 1.1 GB of disk (TaskBrigade 340 MB, MariaDB 460 MB, Gitea 260 MB), the data starts below 200 MB.

| | Minimum | Recommended |
| --- | --- | --- |
| CPU | 64-bit, 2 cores (`linux/amd64` or `linux/arm64`) | 4 cores |
| RAM | 1 GB free without Gitea, 1.5 GB with Gitea | 2 GB or more |
| Disk | 3 GB free (images, data, backups) | SSD, plus space for backups and Git repositories |

- **Raspberry Pi:** Pi 4 or Pi 5 with a 64-bit OS (Raspberry Pi OS 64-bit, Ubuntu arm64) and 2 GB RAM or more;
  32-bit (`armv7`) is not supported. Use an SSD over USB rather than the SD card for the MariaDB volume. Building the
  image on the Pi takes a few minutes; alternatively build it elsewhere for `linux/arm64` (below).
- **Mac with Apple Silicon:** Docker Desktop or OrbStack; the default VM memory is enough.
- The agents (Claude Code, Codex, …) need their own resources on the agent machines; they are not part of this.

## Building for other platforms

The image is based on `python:3.12-slim` and builds for `linux/amd64` and `linux/arm64` (Apple Silicon,
Raspberry Pi 4/5 with a 64-bit OS):

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t registry.example.internal/taskbrigade:0.4.0 --push .
```

Cross-building needs QEMU on the build host (`docker run --privileged --rm tonistiigi/binfmt --install arm64`,
included in Docker Desktop). To run a pushed image (your own, or the public `ghcr.io/taskbrigade/taskbrigade`) set `TBR_IMAGE` and `TBR_PULL_POLICY=always` in `.env`;
`docker compose up -d` then pulls instead of building.

## Changing settings

In Docker the settings come from `.env` next to `compose.yaml` on the Docker host. Edit it there and apply it with
`docker compose up -d` (Compose recreates the containers whose environment changed). To see what the container
actually got, with secrets masked:

```bash
docker compose exec taskbrigade tbr-admin config list
docker compose exec taskbrigade tbr-admin config get GITEA_PUBLIC_URL
```

`tbr-admin config set` refuses inside the container and points to `.env` – a file written there would be lost on the
next `up`. Settings stored in the UI (*Admin → Settings*) override `.env` for the keys they cover. The native
installation edits its env file with `sudo tbr-admin config set KEY VALUE` instead ([quickstart](quickstart.md#4-bootstrap-password)).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `required variable TBR_DB_PASSWORD is missing` | `.env` is missing or not next to `compose.yaml`. |
| `taskbrigade` restarts, log `startup_failed` … `not reachable` | MariaDB did not come up: `docker compose logs mariadb`. After changing `TBR_DB_PASSWORD` on an existing volume the user keeps the old password – change it in MariaDB too or start with a new volume. |
| `Pool overlaps with other one on this address space` | The subnet `172.31.250.0/24` is taken: set `TBR_DOCKER_SUBNET`, `TBR_DOCKER_GATEWAY` and `TRUSTED_PEER_NETWORKS` together. |
| UI or `tbr` gets 403 | The peer is not trusted. `docker compose logs taskbrigade` shows the address; OrbStack and other runtimes may use a different one than the gateway – add exactly that address to `TRUSTED_PEER_NETWORKS`. |
| `port is already allocated` | Port 8090, 3000 or 2222 is in use: `TBR_PUBLISH=127.0.0.1:8091`, or for the profile `TBR_GITEA_PUBLISH=127.0.0.1:3005` with `GITEA_PUBLIC_URL=http://localhost:3005`, `TBR_GITEA_SSH_PUBLISH=127.0.0.1:2223`. |
| `gitea-init` exits with an error | `docker compose logs gitea-init`; it needs `TBR_INITIAL_ADMIN_PASSWORD` (or `TBR_GITEA_ADMIN_PASSWORD`) and runs again on the next `docker compose --profile gitea up -d`. |
| *Test Gitea connection* fails | The container does not reach `GITEA_URL` (profile: `http://gitea:3000`). With your own Gitea test with the `urllib` command under [variants](#with-or-without-the-gitea-profile); `localhost` is the container itself. |
| Clone fails on the agent, `tbr git-credential` returns nothing | The agent reaches Gitea under a different scheme, host or port than `GITEA_PUBLIC_URL` (or `GITEA_URL` if that is empty) – they must match exactly. |
| Gitea links or clone URLs point to the wrong host | Gitea's `ROOT_URL` comes from `GITEA_PUBLIC_URL` in `.env`: set it and run `docker compose --profile gitea up -d` again ([details](#internal-and-public-gitea-address)). |
| Login fails after a restore | Logins, API tokens and Gitea tokens come from the backup – use the passwords of the backed-up installation. |
| `backup.sh`: `the mariadb service is not running` | `docker compose up -d`, then repeat. |
| Build on arm64 is slow or fails with `exec format error` | Wrong platform image; build natively or with buildx for `linux/arm64`. |

## Test scenario: Mac mini (Apple Silicon)

A complete acceptance run on one Mac with Apple Silicon: TaskBrigade, MariaDB and Gitea in Docker, Claude Code
(via `tbr run`) and optionally Claude Desktop (via `tbr mcp`) as agents on the Mac itself. Plan about 30 minutes.

### 1. Docker runtime

Install **one** of them and start it:

- [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) for Apple Silicon, or
- [OrbStack](https://orbstack.dev/) (`brew install orbstack`).

```bash
docker version --format '{{.Server.Arch}}'   # arm64
docker compose version                       # v2.24 or newer
xcode-select --install                        # git, if missing
brew install uv tmux                          # uv for tm, tmux for the runner
```

### 2. Compose with the Gitea profile

Follow [quickstart](#quickstart-first-agent-task-in-5-minutes) steps 1 and 2. Then:

```bash
docker compose --profile gitea ps -a   # taskbrigade, mariadb, gitea: healthy; gitea-admin, gitea-init: Exited (0)
docker image inspect taskbrigade:local --format '{{.Architecture}}'   # arm64 (native build)
curl -fsS http://127.0.0.1:8090/healthz              # "db":"ok"
```

Everything runs on the Mac, so Gitea stays on `http://localhost:3000` (`GITEA_PUBLIC_URL`) and nothing is published
beyond `127.0.0.1`.

### 3. Agents on the Mac

- **Claude Code via `tbr run`:** quickstart step 3 (`install.sh`, `tbr join … --setup claude-code`, `tbr doctor`;
  on macOS the timer is a launchd agent). `tbr whoami` in `~/taskbrigade-work/claude-code` must answer `claude-code`, and
  `printf 'protocol=http\nhost=localhost:3000\n\n' | tbr git-credential get` must print `username=claude-code`.
- **Claude Desktop via `tbr mcp` (optional):** create a second agent `claude-desktop` (*New agent*, runtime
  `cloud`; TaskBrigade creates its Gitea user), then on the Mac
  `tbr join http://127.0.0.1:8090 tmj_… --dir ~/taskbrigade-work/claude-desktop --setup claude-desktop` and
  `tbr doctor --work-root ~/taskbrigade-work/claude-desktop` ([desktop apps via MCP](desktop-apps-mcp.md)).
- **Demo agent (optional, no AI):** the `tbr prepare --task DEMO-0001` round trip from
  [the profile section](#bundled-gitea-and-demo-project-profile-gitea), with the token from `docker compose logs gitea-init`.

### 4. Acceptance checklist

Create the project `HELLO` as in quickstart step 4. Tick each line:

- [ ] **Create task:** task for `claude-code`, status *Defined*. Description: `Create README.md with the line
      "Hello from TaskBrigade". Add brain-changes/<TASK-NR>.md with a section "## add: Test notes" and
      the line "- Docker test run on a Mac mini".` – acceptance criteria accordingly.
- [ ] **Prerequisites:** `command -v tmux claude` finds both. If not, `tbr run` stops **before** claiming with
      `Prerequisites missing – no task claimed` and an install hint (`brew install tmux`; Claude Code: `npm install -g
      @anthropic-ai/claude-code`), exit code 1; the task stays *Defined*.
- [ ] **Claim:** `tbr run --work-root ~/taskbrigade-work/claude-code` claims it; the task shows *in_progress*,
      `tmux attach -t taskbrigade` shows Claude Code working in `~/taskbrigade-work/claude-code/HELLO/…`.
      Should the start fail after the claim, `tbr run` prints the reason (no traceback) and gives the task back.
- [ ] **Resume (optional):** close the task's tmux window while Claude Code works (`Ctrl-b &`), then run `tbr run`
      again: it resumes the same task (`… resumed (own task in progress without a running session)`). While the
      window is still running, a second `tbr run` starts nothing for it.
- [ ] **Pull request:** after `tbr submit` the task is in review, and the pull request exists in Gitea
      (`http://localhost:3000/taskbrigade/hello/pulls`, signed in as `giteaadmin`) with the task branch.
- [ ] **Approve:** **Approve** in the UI; the task is *done*, the pull request merged.
- [ ] **brain-changes applied:** on `main` in Gitea, `brain.md` has a new section *Test notes* with that line, a separate
      commit by the service user applied it, and `brain-changes/<TASK-NR>.md` is gone.
- [ ] **MCP (optional):** in Claude Desktop "Check whether there is a TaskBrigade task for you" for a task assigned
      to `claude-desktop`; `submit_task` opens a pull request.
- [ ] **Backup:** `deploy/docker/backup.sh` writes `backups/taskbrigade-docker-….tar` with `db.sql.gz`,
      `state.tar.gz` and `gitea.tar.gz` (`tar tf backups/*.tar`).
- [ ] **Restore on a fresh installation:** `docker compose --profile gitea down -v` (deletes all data!;
      `docker volume ls` shows no `taskbrigade-*` volume), then
      `deploy/docker/restore.sh backups/taskbrigade-docker-….tar`: project `HELLO` and the done task are back,
      *Test Gitea connection* succeeds and `docker compose logs gitea-init` says *Nothing to do* (the service token
      decrypts, so `master.key` was restored), the `hello` repository with the merged commit is back in Gitea, and
      `tbr whoami` still works with the old API token.
- [ ] **Update:** `deploy/docker/update.sh` takes a backup and ends with all services healthy.

## Appendix: Manual setup

The quickstart's agent step without the automation – for your own Gitea without an admin token, for agent machines
that cannot fetch `/install.sh`, or to see every piece.

**Gitea user and token for the agent.** With the profile `gitea`, using the Gitea CLI in the container:

```bash
docker compose exec -u git gitea gitea admin user create --username claude-code --password 'change-me-agent-1' \
  --email claude-code@example.org --must-change-password=false
docker compose exec -u git gitea gitea admin user generate-access-token --username claude-code \
  --token-name taskbrigade-agent --scopes write:repository,read:user --raw
```

With your own Gitea, create the user and a token with the same scopes in its web UI. Then on the agent's setup page
in TaskBrigade (“Link a Gitea user manually”) enter username, token and email and
**Check and save**. `tbr join` works as in the quickstart afterwards.

**`tbr` without `/install.sh`.** From the checkout: `uv tool install --python 3.12 --from . taskbrigade`.

**Identity file by hand.** Instead of `tbr join`: on the agent's page **Rotate token**, copy the API
token (shown once) and write `~/taskbrigade-work/claude-code/.taskbrigade.toml` with mode 600:

```toml
url = "http://127.0.0.1:8090"
token = "PASTE_THE_ROTATED_TOKEN"

[runner]
command = ["claude", "--permission-mode", "auto", "{prompt}"]
mode = "tmux"
tmux_session = "taskbrigade"
```

Then `tbr setup claude-code --work-root ~/taskbrigade-work/claude-code` installs the timer (or start passes with
`tbr run`), and `tbr doctor --work-root ~/taskbrigade-work/claude-code` checks the result. More detail:
[CLI agents](cli-agents.md#manual-alternative).
