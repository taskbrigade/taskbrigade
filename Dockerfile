# syntax=docker/dockerfile:1
# TaskBrigade web app. Multi-arch: docker buildx build --platform linux/amd64,linux/arm64 .
# Agents (tm, runner, MCP) run on the agent machines, not in this image; the image ships their tbr wheel in
# /app/dist, served under /download/ and by /install.sh.

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

FROM ${PYTHON_IMAGE}
ARG TBR_UID=10001
RUN groupadd --system --gid ${TBR_UID} taskbrigade \
    && useradd --system --uid ${TBR_UID} --gid taskbrigade --home-dir /var/lib/taskbrigade \
       --no-create-home --shell /usr/sbin/nologin taskbrigade \
    && install -d -o taskbrigade -g taskbrigade -m 0700 /var/lib/taskbrigade
WORKDIR /app
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TBR_STATE_DIR=/var/lib/taskbrigade \
    TBR_BIND_HOST=0.0.0.0 \
    TBR_PORT=8090 \
    TBR_DB_HOST=mariadb \
    TBR_DB_PORT=3306
USER taskbrigade
VOLUME ["/var/lib/taskbrigade"]
EXPOSE 8090
# /healthz answers 200 only when the database is reachable and the schema is current.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('TBR_PORT', '8090'), timeout=4)"]
ENTRYPOINT ["python", "-m", "app.container"]
