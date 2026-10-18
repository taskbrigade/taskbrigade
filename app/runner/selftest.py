"""Isolation self-test of the sandbox (`tbr-admin runner selftest`, run as `tbr-runner`).

Each probe is a small script started through the real path – job directory, `taskbrigade-job@<job>.service`, status
file (or, in the runner container, the launcher and a job container with `network_mode: none`) – without the server
and without data. A probe passes when the sandbox stops what it tries: open a socket,
read the data key or the store, write outside `/job/out`, exceed memory, tasks or run time. The scripts exit 0 when
everything they try is refused and 10 when anything got through; limit probes expect systemd to end them.
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.runner.sandbox import JobDir, Outcome, SystemdLauncher, utcnow

SIBLING = "selftest-sibling"

NETWORK = '''
import socket
hits = []
for family, addr in ((socket.AF_INET, ("1.1.1.1", 53)), (socket.AF_INET, ("127.0.0.1", 8090)),
                     (socket.AF_INET6, ("::1", 8090))):
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(addr)
        hits.append(str(addr))
    except OSError:
        pass
# systemd: RestrictAddressFamilies=AF_UNIX refuses both. Docker (`network_mode: none`, default seccomp profile):
# AF_PACKET needs CAP_NET_RAW (dropped); AF_NETLINK only reaches the job's own empty network namespace, so there
# the probe checks instead that loopback is the only interface.
for family in ("AF_PACKET",) if DOCKER else ("AF_NETLINK", "AF_PACKET"):
    try:
        socket.socket(getattr(socket, family), socket.SOCK_RAW)
        hits.append(family)
    except (OSError, AttributeError):
        pass
try:
    socket.getaddrinfo("example.org", 443)
    hits.append("dns")
except OSError:
    pass
if DOCKER:
    names = sorted(name for _i, name in socket.if_nameindex())
    if names != ["lo"]:
        hits.append("interfaces=" + ",".join(names))
print("hits", hits)
raise SystemExit(10 if hits else 0)
'''

READ = '''
import os, sys
hits = []
for path in sys.argv[1:] if len(sys.argv) > 1 else PATHS:
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            open(path, "rb").read(1)
        hits.append(path)
    except OSError:
        pass
print("hits", hits)
raise SystemExit(10 if hits else 0)
'''

WRITE = '''
import os
hits = []
# /tmp, /var/tmp and /dev/shm are writable but private to the job (PrivateTmp, PrivateDevices) and vanish with it.
for path in ("/job/in/x", "/job/script/x", "/etc/x", "/opt/x", "/usr/x", "/srv/x", "/var/lib/x", "/home/x",
             "/run/x", "/run/taskbrigade-runner/x", "/var/log/x", "/x"):
    try:
        with open(path, "w") as f:
            f.write("x")
        hits.append(path)
    except OSError:
        pass
try:
    os.setuid(0)
    hits.append("setuid")
except OSError:
    pass
caps = [l for l in open("/proc/self/status") if l.startswith("CapEff:")]
if caps and int(caps[0].split()[1], 16) != 0:
    hits.append("capabilities")
with open("/job/out/result.json", "w") as f:
    f.write('{"format": "tbr-aggregate/1", "tables": [], "metrics": {}}')
print("SELFTEST-STDOUT-MARKER")
print("hits", hits)
raise SystemExit(10 if hits else 0)
'''

MEMORY = '''
blocks = []
while True:
    blocks.append(bytearray(64 * 1024 * 1024))
'''

TASKS = '''
import threading, time
started = 0
try:
    for _i in range(1000):
        threading.Thread(target=time.sleep, args=(30,), daemon=True).start()
        started += 1
except RuntimeError:
    print("stopped at", started)
    raise SystemExit(0)
raise SystemExit(10)
'''

TIME = '''
import time
time.sleep(100000)
'''

SYMLINK = '''
import os
for name, target in (("key", KEY), ("store", STORE), ("passwd", "/etc/passwd")):
    os.symlink(target, "/job/out/" + name)
raise SystemExit(0)
'''

FSIZE = '''
with open("/job/out/big.bin", "wb") as f:
    try:
        for _i in range(2048):
            f.write(b"x" * (1024 * 1024))
    except OSError:
        raise SystemExit(0)
raise SystemExit(10)
'''


VIEW = '''
import os
hits = []
# Only this job's four directories; its own input is empty, so the other job's data.csv must not show up anywhere.
entries = sorted(os.listdir("/job"))
if entries != ["in", "log", "out", "script"]:
    hits.append("job=" + ",".join(entries))
for sub in ("in", "out"):
    if os.listdir("/job/" + sub):
        hits.append(sub + "=" + ",".join(os.listdir("/job/" + sub)))
for line in open("/proc/self/mounts"):
    target = line.split()[1]
    if target.startswith("/job") and target not in ("/job", "/job/in", "/job/script", "/job/out", "/job/log"):
        hits.append("mount=" + target)
print("hits", hits)
raise SystemExit(10 if hits else 0)
'''


@dataclass
class Probe:
    name: str
    source: str
    expect: str  # "exit0" | "killed" | "timeout"
    slow: bool = False


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str


def probes(data_dir: Path, key_file: Path, docker: bool = False) -> list[Probe]:
    paths = [str(key_file), str(key_file.parent), str(data_dir), str(data_dir / "store"),
             str(data_dir / "quarantine"), "/var/lib/taskbrigade", "/var/lib/taskbrigade-runner",
             "/run/taskbrigade-runner/jobs", "/root", "/home", "/var/log/taskbrigade", "/opt/taskbrigade/app",
             # Docker variant: control sockets, app code and the directories of other jobs
             "/var/run/docker.sock", "/run/docker.sock", "/run/taskbrigade-launcher", "/app"]
    return [
        Probe("network", f"DOCKER = {docker!r}\n" + NETWORK, "exit0"),
        Probe("filesystem-read", f"PATHS = {paths!r}\n" + READ, "exit0"),
        Probe("filesystem-write", WRITE, "exit0"),
        Probe("job-view", VIEW, "exit0"),
        Probe("symlink-out", f"KEY = {str(key_file)!r}\nSTORE = {str(data_dir / 'store')!r}\n" + SYMLINK, "exit0"),
        Probe("limit-tasks", TASKS, "exit0"),
        Probe("limit-file-size", FSIZE, "exit0"),
        Probe("limit-memory", MEMORY, "killed"),
        Probe("limit-time", TIME, "timeout", slow=True),
    ]


def effective_limits(systemctl: str = "systemctl", launcher=None) -> dict[str, str]:
    """Limits of the template as systemd sees them (incl. the drop-in), or the launcher's fixed job limits."""
    if launcher is not None and hasattr(launcher, "ping"):
        info = launcher.ping()
        return {k: str(v) for k, v in info.items() if k != "ok"}
    done = subprocess.run([systemctl, "show", "taskbrigade-job@selftest.service", "-p", "MemoryMax", "-p",
                           "TimeoutStartUSec", "-p", "TasksMax", "-p", "CPUQuotaPerSecUSec"],
                          capture_output=True, text=True)
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def _verdict(probe: Probe, outcome: Outcome, job: JobDir) -> tuple[bool, str]:
    detail = f"result={outcome.service_result} exit={outcome.exit_code}"
    if probe.expect == "exit0":
        ok = outcome.exit_code == 0 and outcome.service_result == "success"
    elif probe.expect == "timeout":
        ok = outcome.service_result == "timeout"
    else:
        ok = outcome.service_result in ("oom-kill", "signal") or (outcome.exit_code not in (0, None)
                                                                  and outcome.service_result != "success")
    if probe.name == "symlink-out":
        files, refused = job.outputs()
        ok = ok and not files and sorted(refused) == ["key", "passwd", "store"]
        detail += f" taken={[n for n, _p in files]} refused={sorted(refused)}"
    if probe.name == "filesystem-write":
        out = job.path("log") / "stdout.log"
        logged = out.is_file() and "SELFTEST-STDOUT-MARKER" in out.read_text(errors="replace")
        ok = ok and logged and (job.path("out") / "result.json").is_file()
        detail += f" stdout-to-file={logged}"
    if outcome.peak_memory_mb is not None:
        detail += f" peak={outcome.peak_memory_mb}MB"
    return ok, detail


def run(jobs_dir: Path, data_dir: Path, key_file: Path, group: str | None, launcher: SystemdLauncher,
        include_slow: bool = False, timeout_s: int = 120) -> list[ProbeResult]:
    results = []
    stamp = utcnow().strftime("%H%M%S")
    # Another job's directory with a marker file: no probe may reach it.
    sibling = JobDir.create(jobs_dir, f"{SIBLING}-{stamp}", group)
    sibling.write_file("in", "data.csv", [b"secret,row\n"])
    try:
        results.extend(_run_probes(jobs_dir, data_dir, key_file, group, launcher, include_slow, timeout_s, stamp))
    finally:
        sibling.remove()
    return results


def _run_probes(jobs_dir, data_dir, key_file, group, launcher, include_slow, timeout_s, stamp):
    results = []
    docker = getattr(launcher, "kind", "systemd") == "docker"
    for probe in probes(data_dir, key_file, docker=docker):
        if probe.slow and not include_slow:
            results.append(ProbeResult(probe.name, True, "skipped (use --all; waits for TimeoutStartSec)"))
            continue
        job = JobDir.create(jobs_dir, f"selftest-{probe.name}-{stamp}", group)
        try:
            job.write_file("script", "entry.py", [probe.source.encode()])
            outcome = launcher.run(job, timeout_s)
            ok, detail = _verdict(probe, outcome, job)
        except Exception as e:  # noqa: BLE001 – a probe that cannot run is a failed probe
            ok, detail = False, f"{type(e).__name__}: {e}"
        finally:
            job.remove()
        results.append(ProbeResult(probe.name, ok, detail))
    return results


def main(argv: list[str] | None = None) -> int:
    """Stand-alone entry (`python3 -m app.runner.selftest` in the app checkout, stdlib only) for hosts or
    test containers without the app's virtualenv; `tbr-admin runner selftest` is the same with runner.toml."""
    import argparse

    parser = argparse.ArgumentParser(description="TaskBrigade sandbox isolation self-test (run as tbr-runner)")
    parser.add_argument("--jobs-dir", type=Path, default=Path("/run/taskbrigade-runner/jobs"))
    parser.add_argument("--data-dir", type=Path, default=Path("/srv/taskbrigade-data"))
    parser.add_argument("--data-key-file", type=Path, default=Path("/etc/taskbrigade/data.key"))
    parser.add_argument("--job-group", default="tbr-job")
    parser.add_argument("--all", action="store_true", help="also wait for the time limit probe")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--launcher", choices=("systemd", "docker"), default="systemd")
    parser.add_argument("--launcher-socket", type=Path, default=Path("/run/taskbrigade-launcher/launcher.sock"))
    args = parser.parse_args(argv)
    if args.launcher == "docker":
        from app.runner.docker import DockerLauncher

        launcher = DockerLauncher(args.launcher_socket)
    else:
        launcher = SystemdLauncher()
    for key, value in effective_limits(launcher=launcher if args.launcher == "docker" else None).items():
        print(f"  {key}={value}")
    failed = False
    for result in run(args.jobs_dir, args.data_dir, args.data_key_file, args.job_group or None, launcher,
                      include_slow=args.all, timeout_s=args.timeout):
        print(f"{'PASS' if result.ok else 'FAIL'} {result.name}: {result.detail}")
        failed |= not result.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
