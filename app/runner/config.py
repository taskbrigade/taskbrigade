"""Runner configuration: `/etc/taskbrigade/runner.toml` (`root:tbr-runner 0640`, written by `tbr-admin runner
join`), path overridable with `TBR_RUNNER_CONFIG`. The API token lives in its own file below the runner's state
directory (`/var/lib/taskbrigade-runner/token`, mode 0600, owner `tbr-runner`).

`launcher` picks the sandbox: `systemd` (native, `taskbrigade-job@.service`) or `docker` (runner container of the
compose profile `data`, jobs through the launcher socket – `app/runner/docker.py`). The runner image sets
`TBR_RUNNER_LAUNCHER=docker`, which is also the default for `tbr-admin runner join` there."""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path("/etc/taskbrigade/runner.toml")
DEFAULT_TOKEN_FILE = Path("/var/lib/taskbrigade-runner/token")
DEFAULT_JOBS_DIR = Path("/run/taskbrigade-runner/jobs")
DEFAULT_LAUNCHER_SOCKET = Path("/run/taskbrigade-launcher/launcher.sock")
LAUNCHERS = ("systemd", "docker")


class RunnerConfigError(RuntimeError):
    pass


@dataclass
class RunnerConfig:
    url: str
    token_file: Path = DEFAULT_TOKEN_FILE
    data_dir: Path = Path("/srv/taskbrigade-data")
    data_key_file: Path = Path("/etc/taskbrigade/data.key")
    jobs_dir: Path = DEFAULT_JOBS_DIR
    job_group: str | None = "tbr-job"
    poll_seconds: int = 30
    systemctl: str = "systemctl"
    launcher: str = "systemd"
    launcher_socket: Path = DEFAULT_LAUNCHER_SOCKET

    def token(self) -> str:
        try:
            token = self.token_file.read_text().strip()
        except OSError as e:
            raise RunnerConfigError(f"{self.token_file} cannot be read: {e.strerror or e}") from e
        if not token:
            raise RunnerConfigError(f"{self.token_file} is empty")
        return token


def config_path() -> Path:
    return Path(os.environ.get("TBR_RUNNER_CONFIG") or DEFAULT_CONFIG)


def default_launcher() -> str:
    value = os.environ.get("TBR_RUNNER_LAUNCHER", "").strip() or "systemd"
    return value if value in LAUNCHERS else "systemd"


def load(path: Path | None = None) -> RunnerConfig:
    path = path or config_path()
    try:
        raw = tomllib.loads(path.read_text())
    except OSError as e:
        raise RunnerConfigError(f"{path} cannot be read: {e.strerror or e} – run `tbr-admin runner join`") from e
    except tomllib.TOMLDecodeError as e:
        raise RunnerConfigError(f"{path}: {e}") from e
    if not raw.get("url"):
        raise RunnerConfigError(f"{path}: 'url' is missing")
    paths = {k: Path(raw[k]) for k in ("token_file", "data_dir", "data_key_file", "jobs_dir", "launcher_socket")
             if raw.get(k)}
    group = raw.get("job_group", "tbr-job")
    launcher = str(raw.get("launcher") or default_launcher())
    if launcher not in LAUNCHERS:
        raise RunnerConfigError(f"{path}: launcher must be one of {', '.join(LAUNCHERS)}")
    return RunnerConfig(url=str(raw["url"]).rstrip("/"), job_group=group or None,
                        poll_seconds=max(5, int(raw.get("poll_seconds", 30))),
                        systemctl=str(raw.get("systemctl", "systemctl")), launcher=launcher, **paths)


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(cfg: RunnerConfig) -> str:
    return "\n".join([
        "# TaskBrigade data-zone runner (docs/operations.md → Data-zone runner). Written by tbr-admin runner join.",
        f"url = {_toml_str(cfg.url)}",
        f"token_file = {_toml_str(str(cfg.token_file))}",
        f"data_dir = {_toml_str(str(cfg.data_dir))}",
        f"data_key_file = {_toml_str(str(cfg.data_key_file))}",
        f"jobs_dir = {_toml_str(str(cfg.jobs_dir))}",
        f"job_group = {_toml_str(cfg.job_group or '')}",
        f"poll_seconds = {cfg.poll_seconds}",
    ] + ([f"launcher = {_toml_str(cfg.launcher)}",
          f"launcher_socket = {_toml_str(str(cfg.launcher_socket))}"] if cfg.launcher != "systemd" else [])) + "\n"
