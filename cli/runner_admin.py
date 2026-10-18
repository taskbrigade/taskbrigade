"""`tbr-admin runner …`: set up and run the data-zone runner on the TaskBrigade host (docs/operations.md →
Data-zone runner). `join` and `limits --write` run as root; `serve`/`once`/`check` as user `tbr-runner`.
In the runner container (compose profile `data`, `TBR_RUNNER_LAUNCHER=docker`) everything runs as `tbr-runner`:
`join` writes into the state volume and starts nothing – the container picks the configuration up itself."""
import grp
import os
import pwd
import subprocess
from pathlib import Path

import typer

from cli.i18n import _

DROPIN = Path("/etc/systemd/system/taskbrigade-job@.service.d/50-limits.conf")
SERVICE = "taskbrigade-runner.service"


def dropin_text(limits: dict) -> str:
    """Unit drop-in with the sandbox limits from Admin → Settings (concept §5: written per drop-in)."""
    return ("# Written by tbr-admin runner limits from Admin → Settings (runner_*). Do not edit by hand.\n"
            "[Service]\n"
            f"MemoryMax={int(limits['memory_mb'])}M\n"
            f"MemorySwapMax=0\n"
            f"CPUQuota={int(limits['cpu_percent'])}%\n"
            f"TimeoutStartSec={int(limits['timeout_s'])}\n")


def _write_private(path: Path, text: str, mode: int, owner: str | None, group: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    uid = pwd.getpwnam(owner).pw_uid if owner else -1
    gid = grp.getgrnam(group).gr_gid if group else -1
    if os.geteuid() != 0:  # Runner container: the file belongs to the user writing it (tbr-runner)
        uid = gid = -1
    if uid != -1 or gid != -1:
        os.chown(tmp, uid, gid)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _service_env() -> dict[str, str]:
    """Values of the app's env file (data paths), so `join` as root sees what the service uses."""
    from cli import admin_config

    try:
        path = admin_config.env_file()
        return admin_config.read_values(path) if path else {}
    except admin_config.ConfigError:
        return {}


def register(app: typer.Typer, data_db) -> None:
    runner_app = typer.Typer(help=_("Data-zone runner: join, run and check the sandbox runner on this host"),
                             no_args_is_help=True)
    app.add_typer(runner_app, name="runner")

    def _load():
        from app.runner import config

        try:
            return config.load()
        except config.RunnerConfigError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e

    @runner_app.command("join", help=_("Redeem a runner join code (Admin → New agent, kind runner), write the "
                                       "runner configuration and token, and enable taskbrigade-runner.service. "
                                       "Run as root."))
    def join(code: str = typer.Argument(..., help=_("Join code tmj_…")),
             url: str = typer.Option(None, help=_("TaskBrigade address as seen from this host (default "
                                                  "TBR_RUNNER_URL or http://127.0.0.1:8090)")),
             enable: bool = typer.Option(True, help=_("Enable and start taskbrigade-runner.service"))):
        from app.config import get_settings
        from app.runner import config

        url = url or os.environ.get("TBR_RUNNER_URL") or "http://127.0.0.1:8090"
        docker = config.default_launcher() == "docker"
        from cli.join import JoinError, redeem

        try:
            answer = redeem(url.rstrip("/"), code)
        except JoinError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        actor = answer.get("actor") or {}
        if actor.get("agent_kind") != "runner":
            typer.echo(_("%(name)s is not a runner agent (kind %(kind)s) – create the agent with kind 'runner'",
                         name=actor.get("name"), kind=actor.get("agent_kind")), err=True)
            raise typer.Exit(1)
        settings, env = get_settings(), _service_env()
        cfg = config.RunnerConfig(url=url.rstrip("/"),
                                  data_dir=Path(env.get("TBR_DATA_DIR") or settings.tbr_data_dir),
                                  data_key_file=Path(env.get("TBR_DATA_KEY_FILE") or settings.tbr_data_key_file),
                                  launcher="docker" if docker else "systemd")
        _write_private(cfg.token_file, answer["token"] + "\n", 0o600, "tbr-runner", "tbr-runner")
        _write_private(config.config_path(), config.render(cfg), 0o640, "root", "tbr-runner")
        typer.echo(_("Runner %(name)s joined; configuration %(path)s", name=actor.get("name"),
                     path=config.config_path()))
        if docker:
            typer.echo(_("The runner container starts within 10 seconds (docker compose logs -f runner)"))
            return
        if enable:
            done = subprocess.run(["systemctl", "enable", "--now", SERVICE], capture_output=True, text=True)
            if done.returncode != 0:
                typer.echo(done.stderr.strip(), err=True)
                raise typer.Exit(1)
            typer.echo(_("%(service)s enabled and started", service=SERVICE))

    @runner_app.command("serve", help=_("Run the runner loop (the ExecStart of taskbrigade-runner.service)"))
    def serve():
        from app.runner.service import serve_forever

        serve_forever(_load())

    @runner_app.command("once", help=_("Claim and run at most one job, then exit"))
    def once():
        import logging

        from app.runner.service import Runner

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        nr = Runner.from_config(_load()).run_once()
        typer.echo(nr or _("No runner job available"))

    @runner_app.command("check", help=_("Check the runner setup on this host: configuration, token, data key, "
                                        "job directory, sandbox unit; exit code 1 on errors"))
    def check():
        from app import data_crypto
        from cli.client import ApiError, Client, Config

        cfg = _load()
        problems = []
        try:
            me = Client(Config(cfg.url, cfg.token())).get("/agents/me")
            if me.get("agent_kind") != "runner":
                problems.append(_("the token belongs to %(name)s, which is no runner", name=me.get("name")))
        except (ApiError, Exception) as e:  # noqa: BLE001 – report every kind of failure as a finding
            problems.append(_("server/token: %(error)s", error=e))
        try:
            data_crypto.load_kek(cfg.data_key_file)
        except data_crypto.DataKeyError as e:
            problems.append(str(e))
        if not os.access(cfg.data_dir / "quarantine", os.W_OK | os.X_OK):
            problems.append(_("%(path)s is not writable", path=cfg.data_dir / "quarantine"))
        # systemd: the jobs directory lives in the service's RuntimeDirectory; Docker: it is the jobs volume itself.
        jobs_root = cfg.jobs_dir if cfg.launcher == "docker" else cfg.jobs_dir.parent
        if not os.access(jobs_root, os.W_OK | os.X_OK):
            problems.append(_("%(path)s is not writable (RuntimeDirectory of the service)", path=jobs_root))
        if cfg.launcher == "docker":
            from app.runner.docker import DockerLauncher
            from app.runner.sandbox import SandboxError

            try:
                DockerLauncher(cfg.launcher_socket).ping()
            except SandboxError as e:
                problems.append(str(e))
        else:
            unit = subprocess.run([cfg.systemctl, "cat", "taskbrigade-job@.service"], capture_output=True,
                                  text=True)
            if unit.returncode != 0:
                problems.append(_("taskbrigade-job@.service is not installed"))
        for line in problems:
            typer.echo(f"✗ {line}", err=True)
        if problems:
            raise typer.Exit(1)
        typer.echo(_("Runner setup ok"))

    @runner_app.command("selftest", help=_("Start probe jobs in the real sandbox and check that network, files "
                                           "outside the job and the limits are blocked (run as tbr-runner); exit "
                                           "code 1 if a probe fails"))
    def selftest(all_probes: bool = typer.Option(False, "--all", help=_("Also wait for the time limit probe")),
                 timeout: int = typer.Option(120, help=_("Seconds to wait for each probe job"))):
        from app.runner import selftest as probes
        from app.runner.service import make_launcher

        cfg = _load()
        launcher = make_launcher(cfg)
        for key, value in probes.effective_limits(cfg.systemctl, launcher).items():
            typer.echo(f"  {key}={value}")
        failed = False
        for result in probes.run(cfg.jobs_dir, cfg.data_dir, cfg.data_key_file, cfg.job_group,
                                 launcher, include_slow=all_probes, timeout_s=timeout):
            typer.echo(f"{'✓' if result.ok else '✗'} {result.name}: {result.detail}")
            failed |= not result.ok
        if failed:
            raise typer.Exit(1)

    @runner_app.command("limits", help=_("Print the sandbox limits from Admin → Settings as a unit drop-in, or "
                                         "write it (root) and reload systemd"))
    def limits(write: bool = typer.Option(False, help=_("Write %(path)s and run systemctl daemon-reload",
                                                        path=DROPIN)),
               defaults: bool = typer.Option(False, help=_("Use the default limits without reading the database"))):
        from app.services import runner_jobs

        if defaults:
            text = dropin_text({k.removeprefix("runner_"): v for k, v in runner_jobs.LIMIT_DEFAULTS.items()})
        else:
            with data_db().session() as session:
                text = dropin_text(runner_jobs.limits(session))
        if not write:
            typer.echo(text, nl=False)
            return
        _write_private(DROPIN, text, 0o644, None, None)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        typer.echo(_("%(path)s written", path=DROPIN))
