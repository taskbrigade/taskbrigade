"""Job directory and sandbox unit of the runner (research/konzept-datenzonen.md §5).

Layout below `jobs_dir` (the runner's `RuntimeDirectory`, a tmpfs): `<job>/in` and `<job>/script` 0750 and files
0640 (owner `tbr-runner`, group `tbr-job`: the job reads, cannot write), `<job>/out` and `<job>/log` 0770 (the job
writes). The unit `taskbrigade-job@<job>.service` binds only these four directories into `/job` and writes
`log/status` from its `ExecStopPost` (systemd's `$SERVICE_RESULT`, `$EXIT_CODE`, `$EXIT_STATUS`, cgroup
`memory.peak`).

Files written by the job are opened with `O_NOFOLLOW` and must be regular files: a symlink planted in `out/` must
never make the runner (group `tbr-data`) copy the KEK or a dataset into quarantine.
"""
import grp
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_OUTPUT_FILES = 20
READ_DIRS, WRITE_DIRS = ("in", "script"), ("out", "log")
STATUS_FILE = "status"
LOG_FILES = ("stdout.log", "stderr.log")
UNIT_PREFIX = "taskbrigade-job@"


class SandboxError(RuntimeError):
    """The sandbox job could not be prepared, started or read back."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass
class Outcome:
    started_at: datetime
    finished_at: datetime
    exit_code: int | None
    service_result: str | None
    peak_memory_mb: int | None = None


@dataclass
class JobDir:
    root: Path
    job_id: str
    gid: int | None = None  # Group of the sandbox user (tbr-job); None = keep (tests)
    # Limits of the job spec (`memory_mb`, `cpu_percent`, `timeout_s`); the Docker launcher applies them per job,
    # the systemd unit takes them from its drop-in.
    limits: dict = field(default_factory=dict)

    @classmethod
    def create(cls, jobs_dir: Path, job_id: str, group: str | None) -> "JobDir":
        if not JOB_ID_RE.match(job_id):
            raise SandboxError(f"invalid job id {job_id!r}")
        gid = grp.getgrnam(group).gr_gid if group else None
        jobs_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        root = jobs_dir / job_id
        root.mkdir(mode=0o750)
        job = cls(root, job_id, gid)
        job._own(root, 0o750)
        for name in READ_DIRS:
            (root / name).mkdir()
            job._own(root / name, 0o750)
        for name in WRITE_DIRS:
            (root / name).mkdir()
            job._own(root / name, 0o770)
        for name in LOG_FILES:  # `append:` opens the existing file: owner and mode stay the runner's
            path = root / "log" / name
            path.touch(mode=0o660)
            job._own(path, 0o660)
        return job

    def _own(self, path: Path, mode: int) -> None:
        if self.gid is not None:
            os.chown(path, -1, self.gid)
        os.chmod(path, mode)

    def path(self, name: str) -> Path:
        return self.root / name

    def write_file(self, sub: str, name: str, chunks) -> Path:
        """Write a file the job may read (`in`/`script`): 0640, group of the job user."""
        if sub not in READ_DIRS or not NAME_RE.match(name):
            raise SandboxError(f"invalid job file {sub}/{name}")
        path = self.root / sub / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        self._own(path, 0o640)
        return path

    def drop_input(self) -> None:
        """Remove the plaintext input right after the job (concept §5 step 6)."""
        shutil.rmtree(self.root / "in", ignore_errors=True)

    def status(self) -> dict[str, str]:
        """`log/status` from the unit's ExecStopPost (key=value lines); {} if missing or not a regular file."""
        data = read_regular(self.root / "log" / STATUS_FILE, 4096)
        if data is None:
            return {}
        out = {}
        for line in data.decode("utf-8", "replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key.strip()] = value.strip()
        return out

    def outputs(self) -> tuple[list[tuple[str, Path]], list[str]]:
        """Regular files directly in `out/` with valid names; plus the names of everything refused."""
        files, refused = [], []
        for entry in sorted(os.scandir(self.root / "out"), key=lambda e: e.name):
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or not NAME_RE.match(entry.name) or len(files) >= MAX_OUTPUT_FILES:
                refused.append(entry.name[:100])
                continue
            files.append((entry.name, Path(entry.path)))
        return files, refused

    def logs(self) -> list[tuple[str, Path]]:
        return [(name, self.root / "log" / name) for name in LOG_FILES]

    def remove(self) -> list[str]:
        """Delete the job directory; return paths that could not be removed (a job may chmod its own subdirs)."""
        left: list[str] = []
        shutil.rmtree(self.root, onexc=lambda _f, path, _e: left.append(path))
        return left


def open_regular(path: Path) -> int | None:
    """File descriptor of a regular file, never following a symlink; None if missing or anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def read_regular(path: Path, limit: int) -> bytes | None:
    fd = open_regular(path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read(limit)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def outcome_from_status(status: dict[str, str], started: datetime, finished: datetime,
                        unit_ok: bool) -> Outcome:
    """Combine `log/status` with the result of `systemctl start` (exit 0 only for a successful unit): a job cannot
    claim success the unit manager did not see."""
    result = status.get("result") or None
    exit_code = _int(status.get("exit_status")) if status.get("exit_code") == "exited" else None
    if result == "success" and not unit_ok:
        result = "exit-code"
    if unit_ok and result is None:
        result = "success"
    if exit_code is None and not status and unit_ok:
        exit_code = 0
    peak = _int(status.get("memory_peak"))
    return Outcome(started, finished, exit_code, result, peak // (1024 * 1024) if peak is not None else None)


@dataclass
class SystemdLauncher:
    """Start `taskbrigade-job@<job>.service` (allowed for `tbr-runner` by the polkit rule) and wait for it."""
    systemctl: str = "systemctl"
    unit_prefix: str = UNIT_PREFIX
    extra_args: list[str] = field(default_factory=list)

    def unit(self, job: JobDir) -> str:
        return f"{self.unit_prefix}{job.job_id}.service"

    def _systemctl(self, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.systemctl, *self.extra_args, *args], capture_output=True, text=True,
                              timeout=timeout)

    def run(self, job: JobDir, timeout_s: int) -> Outcome:
        unit = self.unit(job)
        started = utcnow()
        try:
            # Type=oneshot: `start` returns when the job has ended (exit 0 only for a successful unit).
            done = self._systemctl("start", unit, timeout=timeout_s)
            unit_ok = done.returncode == 0
            error = (done.stderr or "").strip()
        except subprocess.TimeoutExpired:
            self._systemctl("stop", unit, timeout=60)
            unit_ok, error = False, "runner timeout"
        finished = utcnow()
        status = job.status()
        if not status and not unit_ok:
            raise SandboxError(f"{unit} did not run: {error[:300] or 'no status'}")
        return outcome_from_status(status, started, finished, unit_ok)

    def stop(self, job: JobDir) -> None:
        """Stop only this sandbox unit when its task was cancelled, restarted or reassigned."""
        self._systemctl("stop", self.unit(job), timeout=60)
