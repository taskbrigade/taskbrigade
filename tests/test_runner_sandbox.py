"""Data-zone runner without DB (research/konzept-datenzonen.md §5): static pre-check, job directory, status file,
configuration, limits drop-in, units and the script contract in TASK.md. The real sandbox is tested in a throwaway
systemd container (deploy/sandbox-test/run.sh) and on a host with `tbr-admin runner selftest`."""
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest

from app.runner import config, precheck, selftest
from app.runner.service import Runner
from app.runner.sandbox import JobDir, SandboxError, SystemdLauncher, outcome_from_status, read_regular
from cli.prepare import script_contract_note
from cli.runner_admin import dropin_text
from cli.client import ApiError

ROOT = Path(__file__).resolve().parent.parent
T0, T1 = datetime(2026, 10, 16, 8, 0, 0), datetime(2026, 10, 16, 8, 0, 42)


@pytest.mark.parametrize("code,stopped", [("invalid_transition", True), ("forbidden", True),
                                          ("not_found", True), ("bad_gateway", False)])
def test_heartbeat_stops_only_jobs_that_lost_their_task(monkeypatch, code, stopped):
    from types import SimpleNamespace

    monkeypatch.setattr("app.runner.service.HEARTBEAT_S", 0.001)
    observed = threading.Event()
    jobs = []
    job = object()

    def request(*args, **kwargs):
        if not stopped:
            observed.set()
        raise ApiError(409, code, "Task changed")

    def stop(current):
        jobs.append(current)
        observed.set()

    runner = Runner(None, SimpleNamespace(request=request), SimpleNamespace(stop=stop))
    with runner._heartbeat("DEMO-0001", job):
        assert observed.wait(timeout=2)
    assert bool(jobs) == stopped
    assert all(current is job for current in jobs)


def test_launcher_enforces_shortened_workflow_timeout(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    launcher = SystemdLauncher()
    calls = []

    def systemctl(*args, timeout=None):
        calls.append((args, timeout))
        if args[0] == "start":
            raise subprocess.TimeoutExpired("systemctl", timeout)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(launcher, "_systemctl", systemctl)
    job = SimpleNamespace(job_id="workflow-test", status=lambda: {})
    with pytest.raises(SandboxError):
        launcher.run(job, 60)
    assert calls == [(("start", "taskbrigade-job@workflow-test.service"), 60),
                     (("stop", "taskbrigade-job@workflow-test.service"), 60)]


# --- Pre-check ---------------------------------------------------------------------------------------------------

GOOD = """
import json, os
import pandas as pd
from collections import Counter
df = pd.read_csv(os.path.join(os.environ["TBR_INPUT"], "data.csv"))
with open(os.path.join(os.environ["TBR_OUTPUT_DIR"], "result.json"), "w") as f:
    json.dump({"format": "tbr-aggregate/1", "tables": [], "metrics": {"n": len(df)}}, f)
"""


def test_precheck_accepts_a_contract_script():
    assert precheck.check(GOOD) == []


@pytest.mark.parametrize("source,hit", [
    ("import socket", "import socket"),
    ("import urllib.request", "import urllib.request"),
    ("from http import client", "from http import …"),
    ("import requests as r", "import requests"),
    ("import subprocess", "import subprocess"),
    ("from ctypes import CDLL", "from ctypes import …"),
    ("import importlib", "import importlib"),
    ("import os\nos.system('id')", "os.system"),
    ("import os as o\no.popen('id')", "os.popen"),
    ("from os import system", "from os import system"),
    ("exec('1')", "exec()"),
    ("eval('1')", "eval()"),
    ("__import__('socket')", "__import__()"),
    ("x = ().__class__.__bases__", "dunder attribute __class__"),
    ("from . import helper", "from . import …"),
    ("def f(:", "the script does not parse"),
])
def test_precheck_refuses(source, hit):
    findings = precheck.check(source)
    assert hit in [f.what for f in findings], findings
    assert all(str(f).startswith(f"line {f.line}: ") for f in findings)


# --- Job directory -----------------------------------------------------------------------------------------------

def test_job_dir_layout_and_modes(tmp_path):
    job = JobDir.create(tmp_path / "jobs", "demo-0001-ab12cd34", None)
    mode = lambda p: oct(os.stat(p).st_mode & 0o777)  # noqa: E731
    assert mode(job.root) == "0o750"
    assert (mode(job.path("in")), mode(job.path("script"))) == ("0o750", "0o750")
    assert (mode(job.path("out")), mode(job.path("log"))) == ("0o770", "0o770")
    assert [mode(job.path("log") / n) for n in ("stdout.log", "stderr.log")] == ["0o660", "0o660"]
    path = job.write_file("in", "data.csv", [b"a,b\n", b"1,2\n"])
    assert path.read_bytes() == b"a,b\n1,2\n" and mode(path) == "0o640"
    job.drop_input()
    assert not job.path("in").exists()
    assert job.remove() == [] and not job.root.exists()


@pytest.mark.parametrize("job_id", ["../x", "Demo", "", "a/b", "x" * 70])
def test_job_dir_refuses_bad_ids(tmp_path, job_id):
    with pytest.raises(SandboxError):
        JobDir.create(tmp_path, job_id, None)


def test_job_dir_refuses_bad_file_names(tmp_path):
    job = JobDir.create(tmp_path, "j1", None)
    for sub, name in (("out", "x"), ("in", "../x"), ("script", "a b")):
        with pytest.raises(SandboxError):
            job.write_file(sub, name, [b""])


def test_outputs_take_regular_files_only(tmp_path):
    """A job may plant symlinks to the KEK or the store in out/: the runner never follows them."""
    secret = tmp_path / "data.key"
    secret.write_text("KEK")
    job = JobDir.create(tmp_path / "jobs", "j2", None)
    out = job.path("out")
    (out / "result.json").write_text("{}")
    (out / "key").symlink_to(secret)
    (out / "sub").mkdir()
    (out / "bad name").write_text("x")
    files, refused = job.outputs()
    assert [n for n, _p in files] == ["result.json"]
    assert sorted(refused) == ["bad name", "key", "sub"]
    assert read_regular(out / "key", 100) is None and read_regular(out / "result.json", 100) == b"{}"


def test_status_file_is_read_without_following_links(tmp_path):
    job = JobDir.create(tmp_path, "j3", None)
    status = job.path("log") / "status"
    status.write_text("result=success\nexit_code=exited\nexit_status=0\nmemory_peak=10485760\n")
    assert job.status() == {"result": "success", "exit_code": "exited", "exit_status": "0",
                            "memory_peak": "10485760"}
    status.unlink()
    status.symlink_to(job.path("out") / "forged")
    (job.path("out") / "forged").write_text("result=success\n")
    assert job.status() == {}


@pytest.mark.parametrize("status,unit_ok,expected", [
    ({"result": "success", "exit_code": "exited", "exit_status": "0", "memory_peak": "52428800"}, True,
     ("success", 0, 50)),
    ({"result": "exit-code", "exit_code": "exited", "exit_status": "3"}, False, ("exit-code", 3, None)),
    ({"result": "timeout", "exit_code": "killed", "exit_status": "TERM"}, False, ("timeout", None, None)),
    ({"result": "oom-kill", "exit_code": "killed", "exit_status": "KILL"}, False, ("oom-kill", None, None)),
    # A job cannot report success that `systemctl start` did not see.
    ({"result": "success", "exit_code": "exited", "exit_status": "0"}, False, ("exit-code", 0, None)),
    ({}, True, ("success", 0, None)),
])
def test_outcome_from_status(status, unit_ok, expected):
    o = outcome_from_status(status, T0, T1, unit_ok)
    assert (o.service_result, o.exit_code, o.peak_memory_mb) == expected


# --- Configuration, limits, units --------------------------------------------------------------------------------

def test_runner_config_round_trip(tmp_path, monkeypatch):
    cfg = config.RunnerConfig(url="http://127.0.0.1:8090", token_file=tmp_path / "token",
                              data_dir=Path("/srv/x-data"), poll_seconds=12)
    path = tmp_path / "runner.toml"
    path.write_text(config.render(cfg))
    monkeypatch.setenv("TBR_RUNNER_CONFIG", str(path))
    loaded = config.load()
    assert (loaded.url, loaded.data_dir, loaded.poll_seconds, loaded.job_group) == (
        "http://127.0.0.1:8090", Path("/srv/x-data"), 12, "tbr-job")
    with pytest.raises(config.RunnerConfigError):
        loaded.token()
    (tmp_path / "token").write_text("tbr_secret\n")
    assert loaded.token() == "tbr_secret"
    path.write_text("poll_seconds = 3\n")
    with pytest.raises(config.RunnerConfigError, match="url"):
        config.load()


def test_limits_dropin_uses_timeout_start():
    text = dropin_text({"memory_mb": 2048, "cpu_percent": 150, "timeout_s": 600})
    assert "MemoryMax=2048M" in text and "CPUQuota=150%" in text and "TimeoutStartSec=600" in text
    assert "RuntimeMaxSec" not in text  # No effect on Type=oneshot


def test_job_unit_matches_the_concept():
    unit = (ROOT / "deploy/systemd/taskbrigade-job@.service").read_text()
    for line in ("Type=oneshot", "User=tbr-job", "PrivateNetwork=yes", "IPAddressDeny=any",
                 "RestrictAddressFamilies=AF_UNIX", "ProtectSystem=strict", "ProtectHome=yes", "NoNewPrivileges=yes",
                 "CapabilityBoundingSet=", "SystemCallFilter=@system-service", "TasksMax=64", "LimitFSIZE=256M",
                 "TimeoutStartSec=900", "BindReadOnlyPaths=/run/taskbrigade-runner/jobs/%i/in:/job/in",
                 "BindPaths=/run/taskbrigade-runner/jobs/%i/out:/job/out",
                 "StandardOutput=append:/run/taskbrigade-runner/jobs/%i/log/stdout.log",
                 "ExecStart=/opt/taskbrigade/runner-venv/bin/python -I -B /job/script/entry.py"):
        assert line in unit, line
    assert "InaccessiblePaths=-/srv/taskbrigade-data -/etc/taskbrigade" in unit
    runner = (ROOT / "deploy/systemd/taskbrigade-runner.service").read_text()
    for line in ("User=tbr-runner", "SupplementaryGroups=tbr-data tbr-job", "RuntimeDirectory=taskbrigade-runner",
                 "ExecStart=/opt/taskbrigade/app/.venv/bin/tbr-admin runner serve",
                 "ReadWritePaths=-/srv/taskbrigade-data/quarantine"):
        assert line in runner.splitlines(), line


def test_install_scripts_install_the_units():
    prereq = (ROOT / "deploy/prereq.sh").read_text()
    assert "tbr_install_runner_units()" in prereq
    for script in ("install.sh", "update.sh"):
        text = (ROOT / "deploy" / script).read_text()
        assert "tbr_install_runner_units" in text and "deploy/runner-requirements.txt" in text
    pins = [line for line in (ROOT / "deploy/runner-requirements.txt").read_text().splitlines()
            if line and not line.startswith("#")]
    assert pins and all("==" in line for line in pins)
    assert {line.split("==")[0] for line in pins} >= {"pandas", "numpy", "openpyxl", "pyarrow", "defusedxml"}


def test_selftest_probes_cover_the_acceptance_criteria():
    names = {p.name for p in selftest.probes(Path("/srv/taskbrigade-data"), Path("/etc/taskbrigade/data.key"))}
    assert {"network", "filesystem-read", "filesystem-write", "limit-memory", "limit-time", "limit-tasks"} <= names


# --- TASK.md -----------------------------------------------------------------------------------------------------

def test_script_contract_names_exit_code_3():
    text = script_contract_note()
    for phrase in ("TBR_INPUT", "TBR_OUTPUT_DIR/result.json", "tbr-aggregate/1", "No network", "Aggregates only",
                   "3 = the input does not match the schema"):
        assert phrase in text, phrase
