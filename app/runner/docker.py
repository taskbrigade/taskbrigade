"""Docker variant of the sandbox (docs/docker.md → Data zones; design: "Container runner").

The systemd sandbox has two trust steps: polkit lets `tbr-runner` start nothing but `taskbrigade-job@<job>.service`,
and the unit template – not the runner – decides what a job may see. The Docker variant keeps that split:

- **Launcher** (`python -m app.runner.docker`, compose service `runner-launcher`): the only container with the
  Docker socket. It listens on a unix socket shared with the runner container alone and accepts exactly three
  requests: `run` and `stop` for a job id, and `ping`. It builds every job container itself from `job_spec` – a
  fixed template: `network_mode: none`, read-only root, no capabilities, `no-new-privileges`, user `tbr-job`, PID,
  memory, CPU, file-size and open-file limits, no log driver, and only the four sub-directories of this one job
  from the jobs volume (`in`/`script` read-only). The runner can name a job, never an image, a mount, a network or
  a privilege. Requests from any other peer uid than the runner's are refused (`SO_PEERCRED`).
- **Job container**: the same image as the runner (`/opt/taskbrigade/runner-venv`), started as
  `python -I -B /job/script/entry.py` with stdout/stderr redirected into `/job/log` – nothing reaches
  `docker logs`. App code and state directories of the image are hidden behind empty read-only tmpfs mounts.
- **`DockerLauncher`**: the runner side, a drop-in for `sandbox.SystemdLauncher` (`run(job, timeout_s)`,
  `stop(job)`).

After the job, the launcher runs a second throwaway container as `tbr-job` that only does `chmod -R g+rwX` on
`out/` and `log/` (the systemd unit's `ExecStopPost` does the same), so a job cannot lock the runner out of the
files it has to encrypt and delete.

Needs Docker Engine ≥ 26 (API 1.45: volume `Subpath` mounts).
"""
import http.client
import json
import logging
import os
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from app.runner.sandbox import JOB_ID_RE, JobDir, Outcome, SandboxError, utcnow

log = logging.getLogger("taskbrigade.runner.launcher")

API_VERSION = "1.45"
DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_SOCKET = Path("/run/taskbrigade-launcher/launcher.sock")
JOB_LABEL = "org.taskbrigade.job"
NAME_PREFIX = "tbr-job-"
JOB_DIRS = (("in", True), ("script", True), ("out", False), ("log", False))
RUNNER_VENV_PYTHON = "/opt/taskbrigade/runner-venv/bin/python"
# Image paths a job must not see (the native unit's InaccessiblePaths, ProtectHome); covered by empty tmpfs mounts
# without access.
HIDDEN_PATHS = ("/app", "/var/lib/taskbrigade", "/var/lib/taskbrigade-runner", "/run/taskbrigade-runner", "/home",
                "/root")
MAX_REQUEST = 4096
MiB = 1024 * 1024


class DockerError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Docker API {status}: {message}")
        self.status = status


@dataclass
class LauncherSettings:
    image: str
    jobs_volume: str
    job_user: str = "10003:10003"
    runner_uid: int | None = 10002  # Only this peer may talk to the control socket; None = any (tests)
    max_memory_mb: int = 65536
    max_cpu_percent: int = 3200
    max_timeout_s: int = 24 * 3600
    pids_limit: int = 64
    file_size_mb: int = 256
    open_files: int = 256
    tmp_mb: int = 64

    @classmethod
    def from_env(cls, env=os.environ) -> "LauncherSettings":
        image, volume = env.get("TBR_JOB_IMAGE", ""), env.get("TBR_JOBS_VOLUME", "")
        if not image or not volume:
            raise SystemExit("TBR_JOB_IMAGE and TBR_JOBS_VOLUME must be set (compose.yaml, profile data)")
        uid = env.get("TBR_RUNNER_UID", "10002")
        return cls(image=image, jobs_volume=volume, job_user=env.get("TBR_JOB_USER", "10003:10003"),
                   runner_uid=int(uid) if uid else None,
                   max_memory_mb=int(env.get("TBR_JOB_MAX_MEMORY_MB", 65536)),
                   max_cpu_percent=int(env.get("TBR_JOB_MAX_CPU_PERCENT", 3200)))


def clamp_limits(settings: LauncherSettings, limits: dict) -> dict:
    """Limits from the job spec (Admin → Settings → Data runner), bounded by the launcher's maxima."""
    def num(key, default, low, high):
        try:
            value = int(limits.get(key) or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    return {"memory_mb": num("memory_mb", 4096, 64, settings.max_memory_mb),
            "cpu_percent": num("cpu_percent", 200, 10, settings.max_cpu_percent),
            "timeout_s": num("timeout_s", 900, 1, settings.max_timeout_s)}


def _mounts(settings: LauncherSettings, job_id: str, dirs) -> list[dict]:
    return [{"Type": "volume", "Source": settings.jobs_volume, "Target": f"/job/{name}", "ReadOnly": ro,
             "VolumeOptions": {"NoCopy": True, "Subpath": f"{job_id}/{name}"}} for name, ro in dirs]


def _host_config(settings: LauncherSettings, job_id: str, dirs, limits: dict) -> dict:
    memory = limits["memory_mb"] * MiB
    tmpfs = {"/tmp": f"rw,nosuid,nodev,noexec,size={settings.tmp_mb}m"}
    # mode=000: listing them fails with EACCES for the job user, like InaccessiblePaths= of the unit.
    tmpfs.update({path: "ro,nosuid,nodev,noexec,size=4k,mode=000" for path in HIDDEN_PATHS})
    return {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "Privileged": False,
        "IpcMode": "private",
        "PidsLimit": settings.pids_limit,
        "Memory": memory,
        "MemorySwap": memory,  # = Memory: no swap (MemorySwapMax=0)
        "NanoCpus": limits["cpu_percent"] * 10_000_000,
        "Ulimits": [{"Name": "fsize", "Soft": settings.file_size_mb * MiB, "Hard": settings.file_size_mb * MiB},
                    {"Name": "nofile", "Soft": settings.open_files, "Hard": settings.open_files},
                    {"Name": "core", "Soft": 0, "Hard": 0}],
        "Tmpfs": tmpfs,
        "Mounts": _mounts(settings, job_id, dirs),
        # stdout/stderr can hold data rows: never into a log driver (the job writes them into /job/log).
        "LogConfig": {"Type": "none", "Config": {}},
        "RestartPolicy": {"Name": "no"},
        "AutoRemove": False,
        "Init": False,
    }


def job_spec(settings: LauncherSettings, job_id: str, limits: dict) -> dict:
    """Body of `POST /containers/create` for one sandbox job – the Docker counterpart of
    `taskbrigade-job@.service`. Everything here is fixed by the launcher; the runner only supplies the id and the
    limits (clamped)."""
    if not JOB_ID_RE.match(job_id or ""):
        raise SandboxError(f"invalid job id {job_id!r}")
    limits = clamp_limits(settings, limits)
    command = (f"umask 0007; exec {RUNNER_VENV_PYTHON} -I -B /job/script/entry.py "
               ">>/job/log/stdout.log 2>>/job/log/stderr.log")
    return {
        "Image": settings.image,
        "User": settings.job_user,
        "Entrypoint": ["/bin/sh", "-c"],
        "Cmd": [command],
        "Env": ["TBR_INPUT=/job/in", "TBR_OUTPUT_DIR=/job/out", "PYTHONHASHSEED=0", "HOME=/nonexistent",
                "LANG=C.UTF-8", "PATH=/usr/local/bin:/usr/bin:/bin"],
        "WorkingDir": "/job/script",
        "Hostname": "job",
        "NetworkDisabled": True,
        "AttachStdin": False, "AttachStdout": False, "AttachStderr": False, "Tty": False, "OpenStdin": False,
        "Labels": {JOB_LABEL: job_id},
        "HostConfig": _host_config(settings, job_id, JOB_DIRS, limits),
    }


def fixperms_spec(settings: LauncherSettings, job_id: str) -> dict:
    """Give the runner (group tbr-job) back access to everything the job created in `out/` and `log/`."""
    spec = job_spec(settings, job_id, {"memory_mb": 128, "cpu_percent": 100, "timeout_s": 60})
    spec["Cmd"] = ["chmod -R g+rwX /job/out /job/log 2>/dev/null; exit 0"]
    spec["Labels"] = {JOB_LABEL: job_id, f"{JOB_LABEL}.step": "fixperms"}
    spec["HostConfig"]["Mounts"] = _mounts(settings, job_id, JOB_DIRS[2:])
    return spec


def outcome_fields(state: dict, timed_out: bool) -> dict:
    """systemd-style result of a finished container (`State` of `GET /containers/<id>/json`).

    `oom-kill` and `timeout` come from Docker/the launcher, never from the job. Exit codes above 128 are treated as
    a signal (the shell `exec`s Python, so a kill shows up as 128 + signal number)."""
    if timed_out:
        return {"result": "timeout", "exit_code": None}
    if state.get("OOMKilled"):
        return {"result": "oom-kill", "exit_code": None}
    code = state.get("ExitCode")
    if not isinstance(code, int):
        return {"result": "resources", "exit_code": None}
    if code == 0:
        return {"result": "success", "exit_code": 0}
    if code > 128:
        return {"result": "signal", "exit_code": None}
    return {"result": "exit-code", "exit_code": code}


# --- Docker Engine API over the unix socket -------------------------------------------------------------------

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class DockerAPI:
    """The few Engine API calls the launcher needs (stdlib only, no docker SDK in the image)."""

    def __init__(self, socket_path: str = DOCKER_SOCKET):
        self.socket_path = socket_path

    def call(self, method: str, path: str, body: dict | None = None, timeout: float | None = 60):
        conn = _UnixConnection(self.socket_path, timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, f"/v{API_VERSION}{path}", body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        parsed = json.loads(data) if data and data[:1] in (b"{", b"[") else {}
        if resp.status >= 400:
            message = parsed.get("message") if isinstance(parsed, dict) else ""
            raise DockerError(resp.status, message or data.decode(errors="replace")[:300])
        return parsed

    def version(self) -> dict:
        conn = _UnixConnection(self.socket_path, 10)
        try:
            conn.request("GET", "/version")
            resp = conn.getresponse()
            return json.loads(resp.read() or b"{}")
        finally:
            conn.close()


def api_supported(version: str) -> bool:
    try:
        major, minor = (int(x) for x in version.split(".")[:2])
    except ValueError:
        return False
    want = tuple(int(x) for x in API_VERSION.split("."))
    return (major, minor) >= want


# --- Launcher (holds the Docker socket) -----------------------------------------------------------------------

class Launcher:
    def __init__(self, settings: LauncherSettings, api: DockerAPI):
        self.settings, self.api = settings, api

    def _create(self, name: str, spec: dict) -> str:
        try:
            return self.api.call("POST", f"/containers/create?name={name}", spec)["Id"]
        except DockerError as e:
            if e.status != 409:
                raise
            self.api.call("DELETE", f"/containers/{name}?force=1")  # Left over from a crashed launcher
            return self.api.call("POST", f"/containers/create?name={name}", spec)["Id"]

    def _remove(self, cid: str) -> None:
        try:
            self.api.call("DELETE", f"/containers/{cid}?force=1")
        except DockerError as e:
            log.warning("container %s not removed: %s", cid[:12], e)

    def run(self, job_id: str, limits: dict) -> dict:
        spec = job_spec(self.settings, job_id, limits)
        timeout = clamp_limits(self.settings, limits)["timeout_s"]
        cid = self._create(NAME_PREFIX + job_id, spec)
        timed_out = False
        try:
            self.api.call("POST", f"/containers/{cid}/start")
            log.info("job %s started", job_id)
            try:
                self.api.call("POST", f"/containers/{cid}/wait", timeout=timeout)
            except (TimeoutError, socket.timeout):
                timed_out = True
                self.api.call("POST", f"/containers/{cid}/kill")
                self.api.call("POST", f"/containers/{cid}/wait", timeout=60)
            state = self.api.call("GET", f"/containers/{cid}/json").get("State") or {}
        finally:
            self._remove(cid)
        self._fixperms(job_id)
        fields = outcome_fields(state, timed_out)
        log.info("job %s ended: result=%s exit=%s", job_id, fields["result"], fields["exit_code"])
        return fields

    def _fixperms(self, job_id: str) -> None:
        cid = self._create(f"{NAME_PREFIX}{job_id}-fixperms", fixperms_spec(self.settings, job_id))
        try:
            self.api.call("POST", f"/containers/{cid}/start")
            self.api.call("POST", f"/containers/{cid}/wait", timeout=60)
        except (DockerError, OSError) as e:
            log.warning("job %s: permissions not reset: %s", job_id, e)
        finally:
            self._remove(cid)

    def stop(self, job_id: str) -> dict:
        if not JOB_ID_RE.match(job_id or ""):
            raise SandboxError(f"invalid job id {job_id!r}")
        try:
            self.api.call("POST", f"/containers/{NAME_PREFIX}{job_id}/kill")
        except DockerError as e:
            if e.status not in (404, 409):
                raise
        return {"stopped": job_id}

    def cleanup(self) -> int:
        """Remove job containers a previous launcher left behind (e.g. after a crash)."""
        filters = json.dumps({"label": [JOB_LABEL]})
        stale = self.api.call("GET", f"/containers/json?all=1&filters={_quote(filters)}") or []
        for item in stale:
            self._remove(item["Id"])
        return len(stale)

    def handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "ping":
            return {"ok": True, "image": self.settings.image, "volume": self.settings.jobs_volume,
                    "network": "none", "pids_limit": self.settings.pids_limit,
                    "file_size_mb": self.settings.file_size_mb, "open_files": self.settings.open_files,
                    "max_memory_mb": self.settings.max_memory_mb,
                    "max_cpu_percent": self.settings.max_cpu_percent}
        if op == "run":
            limits = request.get("limits") if isinstance(request.get("limits"), dict) else {}
            return self.run(str(request.get("job_id") or ""), limits)
        if op == "stop":
            return self.stop(str(request.get("job_id") or ""))
        raise SandboxError(f"unknown request {op!r}")


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except (OSError, AttributeError):
        return None
    return struct.unpack("3i", creds)[1]


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        launcher: Launcher = self.server.launcher
        allowed = launcher.settings.runner_uid
        if allowed is not None and peer_uid(self.connection) != allowed:
            log.warning("refused a request from uid %s", peer_uid(self.connection))
            self._reply({"error": "forbidden"})
            return
        line = self.rfile.readline(MAX_REQUEST + 1)
        try:
            if len(line) > MAX_REQUEST:
                raise SandboxError("request too large")
            request = json.loads(line or b"{}")
            if not isinstance(request, dict):
                raise SandboxError("request must be an object")
            self._reply(launcher.handle(request))
        except (SandboxError, ValueError) as e:
            self._reply({"error": str(e)[:300]})
        except (DockerError, OSError) as e:
            log.warning("request failed: %s", e)
            self._reply({"error": str(e)[:300]})

    def _reply(self, body: dict) -> None:
        try:
            self.wfile.write(json.dumps(body).encode() + b"\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(launcher: Launcher, path: Path, stop: threading.Event | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    old = os.umask(0o117)  # Socket 0660: owner (launcher) and its group (tbr-runner) only
    try:
        server = _Server(str(path), _Handler)
    finally:
        os.umask(old)
    server.launcher = launcher
    log.info("launcher listening on %s (image %s, volume %s)", path, launcher.settings.image,
             launcher.settings.jobs_volume)
    if stop is not None:
        threading.Thread(target=lambda: (stop.wait(), server.shutdown()), daemon=True).start()
    with server:
        server.serve_forever()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = LauncherSettings.from_env()
    api = DockerAPI(os.environ.get("TBR_DOCKER_SOCKET", DOCKER_SOCKET))
    try:
        version = api.version()
    except OSError as e:
        log.error("Docker socket %s not reachable: %s", api.socket_path, e)
        return 1
    if not api_supported(str(version.get("ApiVersion", ""))):
        log.error("Docker Engine API %s is too old – the data-zone launcher needs %s (Docker Engine >= 26)",
                  version.get("ApiVersion"), API_VERSION)
        return 1
    launcher = Launcher(settings, api)
    removed = launcher.cleanup()
    if removed:
        log.info("removed %d stale job container(s)", removed)
    serve(launcher, Path(os.environ.get("TBR_LAUNCHER_SOCKET") or DEFAULT_SOCKET))
    return 0


# --- Runner side ----------------------------------------------------------------------------------------------

@dataclass
class DockerLauncher:
    """Start sandbox jobs through the launcher's control socket (runner container)."""
    socket_path: Path = DEFAULT_SOCKET
    kind = "docker"

    def request(self, body: dict, timeout: float) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(str(self.socket_path))
                sock.sendall(json.dumps(body).encode() + b"\n")
                data = b""
                while not data.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except OSError as e:
            raise SandboxError(f"launcher {self.socket_path} not reachable: {e}") from e
        try:
            answer = json.loads(data or b"{}")
        except ValueError as e:
            raise SandboxError("launcher sent no valid answer") from e
        if not isinstance(answer, dict) or answer.get("error"):
            raise SandboxError(f"launcher: {answer.get('error') if isinstance(answer, dict) else answer}")
        return answer

    def run(self, job: JobDir, timeout_s: int) -> Outcome:
        limits = {**(job.limits or {}), "timeout_s": timeout_s}
        started = utcnow()
        answer = self.request({"op": "run", "job_id": job.job_id, "limits": limits}, timeout_s + 300)
        exit_code = answer.get("exit_code")
        return Outcome(started, utcnow(), exit_code if isinstance(exit_code, int) else None,
                       str(answer.get("result") or "") or None)

    def stop(self, job: JobDir) -> None:
        try:
            self.request({"op": "stop", "job_id": job.job_id}, 60)
        except SandboxError as e:
            log.warning("job %s not stopped: %s", job.job_id, e)

    def ping(self) -> dict:
        return self.request({"op": "ping"}, 10)


if __name__ == "__main__":
    raise SystemExit(main())
