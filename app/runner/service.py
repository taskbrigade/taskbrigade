"""Claim loop and job flow of the runner (research/konzept-datenzonen.md §5 "Ablauf eines Run-Jobs").

1. Claim (`POST /agents/me/claim`), fetch the job (`GET /runner/jobs/<nr>`) and the merged script.
2. Static pre-check (`precheck`); a hit aborts before any data is decrypted.
3. Job directory, input decrypted into `in/` (checked against the dataset's SHA-256), script as `script/entry.py`.
4. Sandbox unit, heartbeats meanwhile.
5. `out/` and the logs encrypted into `TBR_DATA_DIR/quarantine/<job>/` with a new DEK.
6. Gate probe (output gate checks 4 and 5, `gate_probe`): a second sandbox job `<job>-gate` runs our own probe
   against the input (moved over, not copied) and the script's `result.json`; it reports locations, never values.
   Then the plaintext input is gone.
7. Result reported (`POST /runner/jobs/<nr>/result`); the server runs the output gate and moves the task on. Job
   directories removed.

The runner logs to the journal: task, job, exit code, runtime – never stdout/stderr or output contents.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from app import data_crypto
from app.runner import gate_probe, precheck
from app.runner.config import RunnerConfig
from app.runner.sandbox import JobDir, Outcome, SandboxError, SystemdLauncher, open_regular, read_regular
from cli.client import ApiError, Client, Config

log = logging.getLogger("taskbrigade.runner")

QUARANTINE = "quarantine"
HEARTBEAT_S = 60
SCRIPT_NAME = "entry.py"
RESULT_NAME = "result.json"
PROBE_NAME = "probe.json"
PROBE_SUFFIX = "-gate"
MAX_RESULT_BYTES = 16 * 1024 * 1024  # Larger results fail gate check 1 anyway; the probe does not read them
FAIL_FROM_API = {"script_not_merged": "script_not_merged", "script_missing": "script_missing",
                 "dataset_unavailable": "dataset_unavailable", "workflow_schema_changed": "schema_mismatch"}


class JobFailed(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass
class Runner:
    cfg: RunnerConfig
    client: Client
    launcher: object  # SystemdLauncher, DockerLauncher or a test double with `run(job, timeout_s) -> Outcome`

    @classmethod
    def from_config(cls, cfg: RunnerConfig) -> "Runner":
        return cls(cfg, Client(Config(cfg.url, cfg.token())), make_launcher(cfg))

    # --- Loop ----------------------------------------------------------------------------------------------------

    def run_once(self) -> str | None:
        """Claim and run one job; return the task number or None when there was nothing to do."""
        r = self.client.request("POST", "/agents/me/claim", json={})
        if r.status_code == 204:
            return None
        task = r.json()
        nr = task["task_nr"]
        log.info("claimed %s (%s)", nr, task.get("task_kind"))
        try:
            self.handle(nr)
        except JobFailed as e:
            log.warning("%s: job not run: %s", nr, e.code)
            self._fail(nr, e.code, e.detail)
        except Exception as e:  # noqa: BLE001 – never leave a claimed task hanging until its lease ends
            log.exception("%s: job failed", nr)
            self._fail(nr, "sandbox_error", type(e).__name__)
        return nr

    def serve(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("runner started (poll every %ss)", self.cfg.poll_seconds)
        while not stop.is_set():
            try:
                if self.run_once():
                    continue  # More work may be waiting.
            except ApiError as e:
                log.warning("server: %s", e)
            except Exception:  # noqa: BLE001 – the service must keep polling
                log.exception("runner pass failed")
            stop.wait(self.cfg.poll_seconds)

    def _fail(self, nr: str, code: str, detail: str = "") -> None:
        try:
            self.client.request("POST", f"/runner/jobs/{nr}/fail", json={"code": code, "detail": detail[:2000]})
        except ApiError as e:
            log.error("%s: could not report the failure: %s", nr, e)

    # --- Job -----------------------------------------------------------------------------------------------------

    def handle(self, nr: str) -> None:
        try:
            spec = self.client.get(f"/runner/jobs/{nr}")
        except ApiError as e:
            raise JobFailed(FAIL_FROM_API.get(e.code, "sandbox_error"), e.message) from e
        profiling = spec["task_kind"] == "profile"
        source = Path(__file__).with_name("profiler.py").read_bytes() if profiling else self._script(nr)
        findings = [] if profiling else precheck.check(source)
        if findings:
            raise JobFailed("precheck", "; ".join(str(f) for f in findings[:20]))
        job_id = f"{nr.lower()}-{secrets.token_hex(4)}"
        job = JobDir.create(self.cfg.jobs_dir, job_id, self.cfg.job_group)
        job.limits = dict(spec.get("limits") or {})
        try:
            self._prepare_input(job, spec["dataset"])
            if profiling:
                job.write_file("script", "profile-config.json", [json.dumps({
                    "dataset": spec["dataset"]["key"], "k": spec.get("min_group_size", 5)}).encode()])
            elif spec.get("profile_aliases"):
                job.write_file("script", "profiler-lib.py", [Path(__file__).with_name("profiler.py").read_bytes()])
                job.write_file("script", "aliases.json", [json.dumps(spec["profile_aliases"]).encode()])
                job.write_file("script", "user-script.py", [source])
                source = b'''import json, runpy
from pathlib import Path
p = Path(__file__).parent
runpy.run_path(str(p / "profiler-lib.py"))["alias_input"](json.loads((p / "aliases.json").read_text()))
runpy.run_path(str(p / "user-script.py"), run_name="__main__")
'''
            job.write_file("script", SCRIPT_NAME, [source])
            timeout = int(spec.get("limits", {}).get("timeout_s") or 900)
            log.info("%s: starting job %s", nr, job_id)
            with self._heartbeat(nr, job):
                outcome = self.launcher.run(job, timeout)
            log.info("%s: job %s ended: result=%s exit=%s", nr, job_id, outcome.service_result, outcome.exit_code)
            report = self._quarantine(job, outcome)
            if outcome.exit_code == 0 and outcome.service_result in (None, "success"):
                report["probe"] = self._probe(job, timeout, nr)
            job.drop_input()
            r = self.client.request("POST", f"/runner/jobs/{nr}/result", json=report)
            log.info("%s: result #%s, task %s", nr, r.json().get("result_id"), r.json().get("status"))
        finally:
            job.drop_input()
            left = job.remove()
            if left:
                log.warning("%s: %d path(s) of job %s could not be removed", nr, len(left), job_id)

    def _script(self, nr: str) -> bytes:
        try:
            return self.client.request("GET", f"/runner/jobs/{nr}/script").content
        except ApiError as e:
            raise JobFailed(FAIL_FROM_API.get(e.code, "script_missing"), e.message) from e

    def _kek(self) -> bytes:
        return data_crypto.load_kek(self.cfg.data_key_file)

    def _store_path(self, relative: str) -> Path:
        root = self.cfg.data_dir.resolve()
        path = (root / relative).resolve()
        if root not in path.parents:
            raise JobFailed("dataset_unavailable", "storage path outside the data directory")
        return path

    def _prepare_input(self, job: JobDir, ds: dict) -> None:
        try:
            dek = data_crypto.unwrap_dek(self._kek(), base64.b64decode(ds["dek_wrapped"]), ds["key"])
            sha = hashlib.sha256()

            def chunks():
                with open(self._store_path(ds["storage_path"]), "rb") as f:
                    for chunk in data_crypto.decrypt_chunks(f, dek):
                        sha.update(chunk)
                        yield chunk

            job.write_file("in", "data" + ds.get("suffix", ".bin"), chunks())
        except (data_crypto.DecryptError, data_crypto.DataKeyError, OSError, ValueError) as e:
            raise JobFailed("decrypt_error", type(e).__name__) from e
        if ds.get("sha256") and sha.hexdigest() != ds["sha256"]:
            raise JobFailed("decrypt_error", "checksum mismatch")

    def _quarantine(self, job: JobDir, outcome: Outcome) -> dict:
        """Encrypt outputs and logs into `quarantine/<job>/` with one new DEK; return the result report."""
        outputs, refused = job.outputs()
        if refused:
            log.warning("job %s: %d entr(ies) in out/ refused (not a regular file or bad name)", job.job_id,
                        len(refused))
        target_rel = f"{QUARANTINE}/{job.job_id}"
        target = self.cfg.data_dir / QUARANTINE / job.job_id
        kek = self._kek()
        dek = data_crypto.new_dek()
        files = []
        try:
            target.mkdir(mode=0o750, parents=True)
            for kind, entries in (("output", outputs), ("log", job.logs())):
                for name, path in entries:
                    fd = open_regular(path)
                    if fd is None:
                        continue
                    with os.fdopen(fd, "rb") as src, open(target / f"{name}.enc", "xb") as dst:
                        os.chmod(dst.fileno(), 0o640)
                        writer = data_crypto.EncryptingWriter(dst, dek)
                        while chunk := src.read(data_crypto.CHUNK_SIZE):
                            writer.write(chunk)
                        writer.close()
                    files.append({"name": name, "kind": kind, "path": f"{target_rel}/{name}.enc",
                                  "size": writer.size, "sha256": writer.sha256})
        except OSError as e:
            shutil.rmtree(target, ignore_errors=True)
            raise JobFailed("output_error", type(e).__name__) from e
        if refused:
            files.append(self._note(target, target_rel, dek, refused))
        wrapped = data_crypto.wrap_dek(kek, dek, f"run-result/{job.job_id}")
        return {"job_id": job.job_id, "started_at": outcome.started_at.isoformat(),
                "finished_at": outcome.finished_at.isoformat(), "exit_code": outcome.exit_code,
                "service_result": outcome.service_result, "peak_memory_mb": outcome.peak_memory_mb,
                "dek_wrapped": base64.b64encode(wrapped).decode(), "files": files, "refused": len(refused)}

    def _probe(self, job: JobDir, timeout: int, nr: str) -> dict:
        """Run the gate probe as its own sandbox job: it parses the untrusted input with our code only, and
        without network or keys. Any problem yields an error report – the server then fails checks 4 and 5."""
        fd = open_regular(job.path("out") / RESULT_NAME)
        if fd is None:
            return {"format": gate_probe.FORMAT, "error": "result_missing"}
        with os.fdopen(fd, "rb") as f:
            result = f.read(MAX_RESULT_BYTES + 1)
        if len(result) > MAX_RESULT_BYTES:
            return {"format": gate_probe.FORMAT, "error": "result_too_large"}
        try:
            probe = JobDir.create(self.cfg.jobs_dir, job.job_id + PROBE_SUFFIX, self.cfg.job_group)
            probe.limits = dict(job.limits)
        except (OSError, SandboxError) as e:
            log.warning("job %s: gate probe not prepared: %s", job.job_id, type(e).__name__)
            return {"format": gate_probe.FORMAT, "error": "probe_failed"}
        try:
            for name in os.listdir(job.path("in")):  # Same tmpfs: move, never a second plaintext copy
                os.rename(job.path("in") / name, probe.path("in") / name)
            probe.write_file("script", SCRIPT_NAME, [Path(gate_probe.__file__).read_bytes()])
            probe.write_file("script", RESULT_NAME, [result])
            with self._heartbeat(nr, probe):
                outcome = self.launcher.run(probe, timeout)
            data = read_regular(probe.path("out") / PROBE_NAME, 1024 * 1024)
            if outcome.exit_code != 0 or data is None:
                log.warning("job %s: gate probe failed: result=%s exit=%s", job.job_id, outcome.service_result,
                            outcome.exit_code)
                return {"format": gate_probe.FORMAT, "error": "probe_failed"}
            report = json.loads(data)
            return report if isinstance(report, dict) else {"format": gate_probe.FORMAT, "error": "probe_failed"}
        except (OSError, ValueError, SandboxError) as e:
            log.warning("job %s: gate probe not run: %s", job.job_id, type(e).__name__)
            return {"format": gate_probe.FORMAT, "error": "probe_failed"}
        finally:
            probe.drop_input()
            if probe.remove():
                log.warning("job %s: gate probe directory not fully removed", job.job_id)

    @staticmethod
    def _note(target: Path, target_rel: str, dek: bytes, refused: list[str]) -> dict:
        """Names the job left in `out/` that were not taken (the output gate reports them)."""
        body = json.dumps({"refused": refused}).encode()
        with open(target / "refused.json.enc", "xb") as dst:
            writer = data_crypto.EncryptingWriter(dst, dek)
            writer.write(body)
            writer.close()
        return {"name": "refused.json", "kind": "log", "path": f"{target_rel}/refused.json.enc",
                "size": writer.size, "sha256": writer.sha256}

    def _heartbeat(self, nr: str, job: JobDir):
        runner = self

        class Beat:
            def __enter__(self):
                self.stop = threading.Event()
                self.thread = threading.Thread(target=self.loop, daemon=True)
                self.thread.start()

            def loop(self):
                while not self.stop.wait(HEARTBEAT_S):
                    try:
                        runner.client.request("POST", f"/tasks/{nr}/heartbeat")
                    except ApiError as e:
                        log.warning("%s: heartbeat failed: %s", nr, e)
                        if e.code in ("invalid_transition", "forbidden", "not_found"):
                            stop_job = getattr(runner.launcher, "stop", None)
                            if stop_job:
                                stop_job(job)
                            return

            def __exit__(self, *exc):
                self.stop.set()
                self.thread.join(timeout=5)

        return Beat()


def make_launcher(cfg: RunnerConfig):
    """The sandbox of this runner: the systemd template unit, or the launcher container (compose profile `data`)."""
    if cfg.launcher == "docker":
        from app.runner.docker import DockerLauncher

        return DockerLauncher(cfg.launcher_socket)
    return SystemdLauncher(systemctl=cfg.systemctl)


def serve_forever(cfg: RunnerConfig) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    runner = Runner.from_config(cfg)
    try:
        runner.serve()
    except KeyboardInterrupt:
        pass
