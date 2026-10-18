"""Docker variant of the data zones without a Docker daemon (docs/docker.md → Data zones): the job container
template of the launcher, its control socket, the runner's Docker launcher, the runner image entry point, the SFTP
spool and compose.yaml. The real isolation runs in deploy/docker/data-zones-test.sh."""
import importlib.util
import json
import os
import socket
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.runner import config, container, docker, selftest
from app.runner.sandbox import JobDir, SandboxError
from app.runner.service import make_launcher
from tests.test_compose_files import _load

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = docker.LauncherSettings(image="taskbrigade-runner:local", jobs_volume="taskbrigade-jobs")


# --- Job container template ----------------------------------------------------------------------------------

def test_job_spec_has_no_network_no_privileges_and_only_its_own_directories():
    spec = docker.job_spec(SETTINGS, "demo-0001-ab12cd34", {"memory_mb": 512, "cpu_percent": 150})
    host = spec["HostConfig"]
    assert host["NetworkMode"] == "none" and spec["NetworkDisabled"] is True
    assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and "CapAdd" not in host
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert spec["User"] == "10003:10003"
    assert {(m["Target"], m["ReadOnly"], m["VolumeOptions"]["Subpath"]) for m in host["Mounts"]} == {
        ("/job/in", True, "demo-0001-ab12cd34/in"), ("/job/script", True, "demo-0001-ab12cd34/script"),
        ("/job/out", False, "demo-0001-ab12cd34/out"), ("/job/log", False, "demo-0001-ab12cd34/log")}
    assert all(m["Source"] == "taskbrigade-jobs" and m["Type"] == "volume" for m in host["Mounts"])
    assert "Binds" not in host and "Devices" not in host
    # stdout/stderr may hold data rows: no log driver, the job writes them into /job/log itself.
    assert host["LogConfig"]["Type"] == "none"
    assert ">>/job/log/stdout.log 2>>/job/log/stderr.log" in spec["Cmd"][0]
    assert "-I -B /job/script/entry.py" in spec["Cmd"][0] and spec["Cmd"][0].startswith("umask 0007;")
    assert host["Memory"] == host["MemorySwap"] == 512 * docker.MiB
    assert host["NanoCpus"] == 1_500_000_000 and host["PidsLimit"] == 64
    assert {u["Name"]: u["Hard"] for u in host["Ulimits"]} == {"fsize": 256 * docker.MiB, "nofile": 256, "core": 0}
    for path in docker.HIDDEN_PATHS:
        assert "mode=000" in host["Tmpfs"][path]
    assert "TBR_INPUT=/job/in" in spec["Env"] and "TBR_OUTPUT_DIR=/job/out" in spec["Env"]


@pytest.mark.parametrize("job_id", ["", "../etc", "a/b", "UPPER", "x" * 70, "-lead"])
def test_job_spec_refuses_bad_ids(job_id):
    with pytest.raises(SandboxError):
        docker.job_spec(SETTINGS, job_id, {})


def test_fixperms_container_only_sees_out_and_log():
    spec = docker.fixperms_spec(SETTINGS, "demo-0001-ab12cd34")
    assert [m["Target"] for m in spec["HostConfig"]["Mounts"]] == ["/job/out", "/job/log"]
    assert spec["HostConfig"]["NetworkMode"] == "none" and spec["User"] == "10003:10003"
    assert spec["Cmd"][0].startswith("chmod -R g+rwX /job/out /job/log")


def test_limits_are_clamped_by_the_launcher():
    tight = docker.LauncherSettings(image="i", jobs_volume="v", max_memory_mb=1024, max_cpu_percent=100,
                                    max_timeout_s=60)
    assert docker.clamp_limits(tight, {"memory_mb": 99999, "cpu_percent": 900, "timeout_s": 10**6}) == {
        "memory_mb": 1024, "cpu_percent": 100, "timeout_s": 60}
    assert docker.clamp_limits(tight, {"memory_mb": "x"}) == {"memory_mb": 1024, "cpu_percent": 100,
                                                              "timeout_s": 60}
    assert docker.clamp_limits(SETTINGS, {})["memory_mb"] == 4096


@pytest.mark.parametrize("state,timed_out,expected", [
    ({"ExitCode": 0}, False, ("success", 0)),
    ({"ExitCode": 3}, False, ("exit-code", 3)),
    ({"ExitCode": 137, "OOMKilled": True}, False, ("oom-kill", None)),
    ({"ExitCode": 137}, False, ("signal", None)),
    ({"ExitCode": 137}, True, ("timeout", None)),
    ({}, False, ("resources", None)),
])
def test_outcome_fields(state, timed_out, expected):
    fields = docker.outcome_fields(state, timed_out)
    assert (fields["result"], fields["exit_code"]) == expected


def test_api_version_check():
    assert docker.api_supported("1.45") and docker.api_supported("1.52") and docker.api_supported("2.0")
    assert not docker.api_supported("1.44") and not docker.api_supported("")


class FakeAPI:
    def __init__(self, state=None, wait_timeout=False):
        self.calls, self.state, self.wait_timeout, self.waits = [], state or {"ExitCode": 0}, wait_timeout, 0

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path.split("?")[0], body))
        if path.startswith("/containers/create"):
            return {"Id": f"c{len(self.calls)}"}
        if path.endswith("/wait") and self.wait_timeout and self.waits == 0:
            self.waits += 1
            raise TimeoutError
        if path.endswith("/json"):
            return {"State": self.state}
        return {}


def test_launcher_runs_waits_inspects_and_removes():
    api = FakeAPI({"ExitCode": 3})
    fields = docker.Launcher(SETTINGS, api).run("demo-0001-ab12cd34", {"timeout_s": 30})
    assert fields == {"result": "exit-code", "exit_code": 3}
    ops = [(m, p) for m, p, _b in api.calls]
    assert ops[:4] == [("POST", "/containers/create"), ("POST", "/containers/c1/start"),
                       ("POST", "/containers/c1/wait"), ("GET", "/containers/c1/json")]
    assert ("DELETE", "/containers/c1") in ops
    created = [b for m, p, b in api.calls if p == "/containers/create"]
    assert created[0]["HostConfig"]["NetworkMode"] == "none"
    assert created[1]["Cmd"][0].startswith("chmod")  # fixperms after the job


def test_launcher_kills_a_job_past_its_time_limit():
    api = FakeAPI(wait_timeout=True)
    fields = docker.Launcher(SETTINGS, api).run("demo-0001-ab12cd34", {"timeout_s": 1})
    assert fields["result"] == "timeout"
    assert ("POST", "/containers/c1/kill") in [(m, p) for m, p, _b in api.calls]


def test_launcher_refuses_unknown_requests():
    launcher = docker.Launcher(SETTINGS, FakeAPI())
    with pytest.raises(SandboxError):
        launcher.handle({"op": "exec", "cmd": "sh"})
    with pytest.raises(SandboxError):
        launcher.handle({"op": "stop", "job_id": "../x"})
    assert launcher.handle({"op": "ping"})["network"] == "none"


# --- Control socket and the runner side ----------------------------------------------------------------------

def _serve(tmp_path, runner_uid):
    settings = docker.LauncherSettings(image="i", jobs_volume="v", runner_uid=runner_uid)
    stop = threading.Event()
    path = tmp_path / "launcher.sock"
    # Bind in this thread: `listen` changes the process umask briefly, which must not race the test's own files.
    server = docker.listen(docker.Launcher(settings, FakeAPI()), path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=lambda: (stop.wait(), server.shutdown(), server.server_close()), daemon=True).start()
    return path, stop


def test_runner_starts_jobs_through_the_socket(tmp_path):
    path, stop = _serve(tmp_path, os.getuid())
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        job = JobDir.create(tmp_path / "jobs", "demo-0001-ab12cd34", None)
        job.limits = {"memory_mb": 256}
        outcome = docker.DockerLauncher(path).run(job, 30)
        assert (outcome.service_result, outcome.exit_code) == ("success", 0)
        assert docker.DockerLauncher(path).ping()["ok"] is True
    finally:
        stop.set()


def test_socket_refuses_other_peers(tmp_path):
    path, stop = _serve(tmp_path, os.getuid() + 1)
    try:
        with pytest.raises(SandboxError, match="forbidden"):
            docker.DockerLauncher(path).ping()
    finally:
        stop.set()


def test_docker_launcher_reports_an_unreachable_launcher(tmp_path):
    with pytest.raises(SandboxError, match="not reachable"):
        docker.DockerLauncher(tmp_path / "missing.sock").ping()


def test_runner_config_selects_the_docker_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("TBR_RUNNER_LAUNCHER", "docker")
    assert config.default_launcher() == "docker"
    cfg = config.RunnerConfig(url="http://taskbrigade:8090", launcher="docker")
    path = tmp_path / "runner.toml"
    path.write_text(config.render(cfg))
    loaded = config.load(path)
    assert loaded.launcher == "docker" and loaded.launcher_socket == config.DEFAULT_LAUNCHER_SOCKET
    assert isinstance(make_launcher(loaded), docker.DockerLauncher)
    path.write_text('url = "http://x"\nlauncher = "chroot"\n')
    with pytest.raises(config.RunnerConfigError):
        config.load(path)


def test_native_config_keeps_systemd(tmp_path, monkeypatch):
    monkeypatch.delenv("TBR_RUNNER_LAUNCHER", raising=False)
    cfg = config.RunnerConfig(url="http://127.0.0.1:8090")
    assert "launcher" not in config.render(cfg)
    path = tmp_path / "runner.toml"
    path.write_text(config.render(cfg))
    assert config.load(path).launcher == "systemd"


def test_selftest_adapts_the_network_probe_and_checks_the_job_view():
    native = {p.name: p for p in selftest.probes(Path("/srv/d"), Path("/etc/k"))}
    dock = {p.name: p for p in selftest.probes(Path("/srv/d"), Path("/etc/k"), docker=True)}
    assert native["network"].source.startswith("DOCKER = False") and dock["network"].source.startswith("DOCKER = True")
    assert "job-view" in dock and "/var/run/docker.sock" in dock["filesystem-read"].source
    assert selftest.effective_limits(launcher=SimpleNamespace(ping=lambda: {"ok": True, "network": "none"})) == {
        "network": "none"}


# --- Runner image entry point and settings ---------------------------------------------------------------------

def test_layout_shares_files_through_the_data_group():
    entries = {e.path.name: e for e in container.layout(Path("/srv/d"), Path("/etc/tbr/data.key"),
                                                         Path("/var/lib/r"), Path("/var/lib/s"))}
    for name in ("d", "store", "incoming", "quarantine"):
        assert entries[name].gid == container.DATA_GID and entries[name].mode & stat.S_ISGID
    assert entries["quarantine"].uid == container.RUNNER_UID and entries["store"].uid == container.APP_UID
    assert (entries["tbr"].uid, entries["tbr"].mode) == (0, 0o750)
    assert (entries["r"].uid, entries["r"].mode) == (container.RUNNER_UID, 0o700)


def test_init_refuses_without_the_switch(capsys):
    assert container.init({}) == 1
    assert "TBR_DATA_ZONES=1" in capsys.readouterr().err


def test_runner_network_may_use_the_api_but_is_not_trusted():
    s = Settings(_env_file=None, tbr_runner_networks="172.31.252.0/24")
    assert s.is_api_peer("172.31.252.5") and s.is_runner_peer("172.31.252.5")
    assert not s.is_trusted_peer("172.31.252.5")
    assert not Settings(_env_file=None).is_runner_peer("172.31.252.5")


# --- SFTP spool --------------------------------------------------------------------------------------------------

def test_spool_lists_accounts_with_fixed_uids_and_keeps_purges(tmp_path, monkeypatch):
    from app.services import data_sftp

    spool = tmp_path / "spool" / "accounts.json"
    monkeypatch.setattr(data_sftp, "get_settings", lambda: SimpleNamespace(
        tbr_sftp_provision="spool", tbr_sftp_spool=spool, tbr_sftp_dir=tmp_path / "sftp"))
    on = SimpleNamespace(id=3, username="demo-export", enabled=True)
    off = SimpleNamespace(id=4, username="paused", enabled=False)
    key = SimpleNamespace(key_type="ssh-ed25519", key_blob="AAAAC3Nza", fingerprint="SHA256:x")
    monkeypatch.setattr(data_sftp, "all_accounts", lambda session: [on, off])
    monkeypatch.setattr(data_sftp, "keys_of", lambda session, account: [key])
    data_sftp.write_spool(None, {"gone"})
    data_sftp.write_spool(None)  # A later write keeps the purge request
    state = json.loads(spool.read_text())
    assert state["format"] == "tbr-sftp-accounts/1" and state["purge"] == ["gone"]
    accounts = {a["username"]: a for a in state["accounts"]}
    assert accounts["demo-export"]["uid"] == 20003 and "ssh-ed25519 AAAAC3Nza" in accounts["demo-export"][
        "authorized_keys"]
    assert accounts["paused"]["authorized_keys"] == "" and accounts["paused"]["enabled"] is False
    assert stat.S_IMODE(spool.stat().st_mode) == 0o644
    assert data_sftp.system_available() is None
    assert data_sftp.purge_system("gone") == ["queued for the SFTP container"]


def _sftp_sync():
    spec = importlib.util.spec_from_file_location("sftp_sync", ROOT / "deploy/docker/sftp/sftp_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sftp_sync_validates_the_spool(tmp_path, monkeypatch):
    sync = _sftp_sync()
    assert sync.valid({"username": "demo-export", "uid": 20003})
    for bad in ({"username": "root", "uid": 0}, {"username": "../x", "uid": 20003}, {"username": "a", "uid": "1"},
                "x"):
        assert not sync.valid(bad)
    monkeypatch.setattr(sync, "SPOOL", tmp_path / "accounts.json")
    assert sync.load_spool() == {"format": sync.FORMAT, "accounts": [], "purge": []}
    (tmp_path / "accounts.json").write_text('{"format": "other"}')
    assert sync.load_spool() is None  # Unknown format: keep the current users
    (tmp_path / "accounts.json").write_text("{broken")
    assert sync.load_spool() is None


# --- compose.yaml and the scripts --------------------------------------------------------------------------------

def test_compose_profiles_and_isolation():
    compose = _load("compose.yaml")
    services, volumes, networks = compose["services"], compose["volumes"], compose["networks"]
    for name in ("data-init", "runner-launcher", "runner"):
        assert services[name]["profiles"] == ["data"]
        assert services[name]["build"]["target"] == "runner"
    assert services["sftp"]["profiles"] == ["data-sftp"]
    launcher, runner = services["runner-launcher"], services["runner"]
    assert launcher["network_mode"] == "none" and launcher["read_only"] and launcher["cap_drop"] == ["ALL"]
    assert any(v.endswith(":/var/run/docker.sock") for v in launcher["volumes"])
    # Only the launcher holds the Docker socket; the runner sits on the internal network alone.
    for name, svc in services.items():
        if name != "runner-launcher":
            assert not any("docker.sock" in str(v) for v in svc.get("volumes", [])), name
    assert runner["networks"] == ["taskbrigade-runner"] and networks["taskbrigade-runner"]["internal"] is True
    assert runner["read_only"] and runner["cap_drop"] == ["ALL"]
    mounts = {v["target"]: v for v in runner["volumes"] if isinstance(v, dict)}
    assert mounts["/srv/taskbrigade-data/store"]["read_only"] is True
    assert mounts["/srv/taskbrigade-data/quarantine"]["volume"]["subpath"] == "quarantine"
    assert volumes["taskbrigade-jobs"]["driver_opts"]["type"] == "tmpfs"
    assert "uid=10002,gid=10003,mode=0750" in volumes["taskbrigade-jobs"]["driver_opts"]["o"]
    assert launcher["environment"]["TBR_JOBS_VOLUME"] == volumes["taskbrigade-jobs"]["name"]
    app = services["taskbrigade"]
    assert app["environment"]["TBR_RUNNER_NETWORKS"] == "${TBR_RUNNER_SUBNET:-172.31.252.0/24}"
    assert "${TBR_RUNNER_SUBNET:-172.31.252.0/24}" == networks["taskbrigade-runner"]["ipam"]["config"][0]["subnet"]
    assert "taskbrigade-runner" in app["networks"] and app["group_add"] == ["10004"]
    assert services["data-init"]["environment"]["TBR_DATA_ZONES"] == "${TBR_DATA_ZONES:-}"


def test_test_stacks_rename_every_app_volume():
    """`down -v` of the smoke test or the screenshot stack must never remove a volume of the real stack."""
    compose = _load("compose.yaml")
    mounted = {v.split(":")[0] for v in compose["services"]["taskbrigade"]["volumes"]}
    smoke = (ROOT / "deploy/docker/smoke-test.sh").read_text()
    from deploy.release import screenshots

    for volume in mounted:
        assert f"  {volume}:\n    name: $PROJECT-" in smoke, volume
        short = volume.removeprefix("taskbrigade-")
        assert volume in ("taskbrigade-state",) or short in screenshots.DATA_VOLUMES, volume


def _echo_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    docker_bin = bin_dir / "docker"
    docker_bin.write_text('#!/bin/sh\n[ "$1" = volume ] && exit 1\necho "$@"\n')
    docker_bin.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize("env,profiles", [
    ("", []), ("TBR_DATA_ZONES=1\n", ["data"]), ('TBR_DATA_ZONES="1"\nTBR_DATA_SFTP=1\n', ["data", "data-sftp"]),
    ("TBR_DATA_SFTP=1\n", []), ("# TBR_DATA_ZONES=1\n", []),
])
def test_dc_adds_the_data_profiles_from_env(tmp_path, env, profiles):
    (tmp_path / ".env").write_text(env)
    run_env = dict(os.environ, PATH=f"{_echo_docker(tmp_path)}{os.pathsep}{os.environ['PATH']}",
                   TBR_COMPOSE_DIR=str(tmp_path))
    out = subprocess.run(["bash", "-c", "set -euo pipefail\nsource deploy/docker/common.sh\ndc ps"], cwd=ROOT,
                         capture_output=True, text=True, env=run_env).stdout.split()
    assert [out[i + 1] for i, word in enumerate(out) if word == "--profile"] == profiles


def test_sftp_image_uses_the_native_drop_in():
    dockerfile = (ROOT / "deploy/docker/sftp/Dockerfile").read_text()
    assert "COPY deploy/ssh/60-taskbrigade-sftp.conf /etc/ssh/sshd_config.d/" in dockerfile
    ignore = (ROOT / "deploy/docker/sftp/Dockerfile.dockerignore").read_text()
    for line in dockerfile.splitlines():
        if line.startswith("COPY "):
            assert "!" + line.split()[1] in ignore
    assert "Include /etc/ssh/sshd_config.d/*.conf" in (ROOT / "deploy/docker/sftp/sshd_config").read_text()


def test_unix_socket_peer_uid_is_read():
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        assert docker.peer_uid(a) == os.getuid()
