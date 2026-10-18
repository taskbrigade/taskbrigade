# syntax=docker/dockerfile:1
# TaskBrigade web app. Multi-arch: docker buildx build --platform linux/amd64,linux/arm64 .
# Agents (tm, runner, MCP) run on the agent machines, not in this image; the image ships their tbr wheel in
# /app/dist, served under /download/ and by /install.sh.
# Target `runner` (compose profile `data`, docs/docker.md → Data zones): the same app plus the pinned analysis
# virtualenv of the data-zone runner; used for the runner, its job launcher and every sandbox job container.
# The default target (last stage) stays the web app.

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.21

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first so source changes do not invalidate this layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY app ./app
COPY cli ./cli
COPY migrations ./migrations
# Shipped in the tbr wheel as cli/agent_files (pyproject.toml, force-include).
COPY deploy/agents ./deploy/agents
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    && uv build --wheel --out-dir /app/dist

# Pinned analysis packages for sandbox jobs (deploy/runner-requirements.txt, like TBR_DATA_ZONES=1 install.sh).
FROM build AS runner-venv
COPY deploy/runner-requirements.txt /tmp/runner-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv --python /usr/local/bin/python3 /opt/taskbrigade/runner-venv \
    && uv pip install --python /opt/taskbrigade/runner-venv/bin/python -r /tmp/runner-requirements.txt

FROM ${PYTHON_IMAGE} AS base
ARG TBR_UID=10001
# Data-zone ids are fixed: volumes shared between the app, runner, launcher, job and SFTP containers rely on them.
ARG TBR_DATA_GID=10004
RUN groupadd --system --gid ${TBR_UID} taskbrigade \
    && useradd --system --uid ${TBR_UID} --gid taskbrigade --home-dir /var/lib/taskbrigade \
       --no-create-home --shell /usr/sbin/nologin taskbrigade \
    && groupadd --system --gid ${TBR_DATA_GID} tbr-data \
    && install -d -o taskbrigade -g taskbrigade -m 0700 /var/lib/taskbrigade
WORKDIR /app
COPY --from=build /app /app
ENV TBR_WORKFLOW_TICK=app \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TBR_STATE_DIR=/var/lib/taskbrigade \
    TBR_BIND_HOST=0.0.0.0 \
    TBR_PORT=8090 \
    TBR_DB_HOST=mariadb \
    TBR_DB_PORT=3306

FROM base AS runner
ARG TBR_RUNNER_UID=10002
ARG TBR_JOB_UID=10003
# tbr-runner: runner service (reads the KEK and the store through tbr-data, shares job directories through tbr-job);
# tbr-job: user of every sandbox job container, no other group.
RUN groupadd --system --gid ${TBR_RUNNER_UID} tbr-runner \
    && groupadd --system --gid ${TBR_JOB_UID} tbr-job \
    && useradd --system --uid ${TBR_RUNNER_UID} --gid tbr-runner --groups tbr-data,tbr-job \
       --home-dir /var/lib/taskbrigade-runner --no-create-home --shell /usr/sbin/nologin tbr-runner \
    && useradd --system --uid ${TBR_JOB_UID} --gid tbr-job --home-dir /nonexistent --no-create-home \
       --shell /usr/sbin/nologin tbr-job \
    && install -d -o tbr-runner -g tbr-runner -m 0700 /var/lib/taskbrigade-runner \
    && install -d -o tbr-runner -g tbr-job -m 0750 /run/taskbrigade-runner /run/taskbrigade-runner/jobs
COPY --from=runner-venv /opt/taskbrigade/runner-venv /opt/taskbrigade/runner-venv
ENV TBR_RUNNER_CONFIG=/var/lib/taskbrigade-runner/runner.toml \
    TBR_RUNNER_LAUNCHER=docker
USER tbr-runner
HEALTHCHECK NONE
ENTRYPOINT ["python", "-m", "app.runner.container"]
CMD ["serve"]

FROM base AS app
USER taskbrigade
VOLUME ["/var/lib/taskbrigade"]
EXPOSE 8090
# /healthz answers 200 only when the database is reachable and the schema is current.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('TBR_PORT', '8090'), timeout=4)"]
ENTRYPOINT ["python", "-m", "app.container"]
