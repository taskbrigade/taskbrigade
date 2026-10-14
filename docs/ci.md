<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main`, on pull requests, on tags `v*` and manually
(`workflow_dispatch`). It is written for GitHub Actions and also runs locally with [act](https://nektosact.com/) or
on Gitea Actions.

| Job | What it does |
| --- | --- |
| `lint` | `uvx ruff@0.14.0 check .` (rules: Ruff defaults, settings in `pyproject.toml`). Formatting is not enforced. |
| `test` | `uv run pytest -q` – the complete suite, including the database tests, against a throwaway `mariadb:10.5` service container (database `tm_test`, host port 33306). The job never sees another database: `tests/conftest.py` refuses any name not ending in `_test`. |
| `image` | `docker buildx build` for `linux/amd64` and `linux/arm64` (arm64 through QEMU), no push. |
| `smoke` | `deploy/docker/smoke-test.sh`: `docker compose --profile gitea up` with a freshly built image, `GET /healthz`, then one task through the agent workflow – `tbr create` → `tbr prepare` (claim, clone, worktree) → commit and `git push` with `tbr git-credential` → `tbr submit` → status `for_review` and an open pull request in the bundled Gitea. |
| `release` | Tags `v*` only, after all other jobs: builds both platforms and pushes `ghcr.io/taskbrigade/taskbrigade:<version>` and `:<major>.<minor>` with the workflow's `GITHUB_TOKEN`. |

The image name follows the public repository `taskbrigade/taskbrigade` (as the URLs in `pyproject.toml`); `IMAGE`
at the top of the workflow sets it (lowercase, as GHCR requires). Branch pushes and pull requests never push an
image.

## Smoke test on your machine

```bash
deploy/docker/smoke-test.sh
```

Needs Docker with Compose v2.24+, git, curl, python3 and uv. It runs as its own Compose project `tm-smoke` with its
own network (`172.31.251.0/24`), volumes (`tm-smoke-*`) and ports (`127.0.0.1:18090` app, `127.0.0.1:13300`
Gitea), so a TaskBrigade stack on the same host is not touched; change them with `TBR_SMOKE_*` variables (see the top
of the script) if they are taken. Passwords are random per run. Everything is removed at the end;
`TBR_SMOKE_KEEP=1` keeps the stack and the agent directory for a look. A `.env` in the repository root is read by
the containers (`env_file`) and can change the result – the script warns about it.

## Running the workflow with act

```bash
git clone --branch <your-branch> <repo> /tmp/tm-ci && cd /tmp/tm-ci
act -j lint  -P ubuntu-24.04=catthehacker/ubuntu:act-24.04
act -j test  -P ubuntu-24.04=catthehacker/ubuntu:act-24.04
act -j smoke -P ubuntu-24.04=catthehacker/ubuntu:act-24.04
act -j image -P ubuntu-24.04=catthehacker/ubuntu:act-24.04
```

- Run act from a regular clone, not from a `git worktree`: act copies the directory into the job container, and a
  worktree's `.git` file points to a path that does not exist there – the Git tests then fail with
  `fatal: not a git repository: (null)`.
- act starts the job container on the host network and mounts the Docker socket, so the service port 33306 and
  the smoke test's published ports are reachable on `127.0.0.1` exactly as on GitHub. Port 33306 must be free.
- `act -j release` is skipped unless the event is a tag; `act push -e <event.json>` with
  `{"ref": "refs/tags/v0.0.0"}` shows the release job in the plan (`-n` for a dry run – do not run it for real
  without a registry login).
