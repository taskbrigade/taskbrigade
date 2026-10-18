"""End-to-end runner jobs in the throwaway systemd container (deploy/sandbox-test/run.sh).

`prepare` (root): encrypt a small CSV dataset into the store like the web upload does.
`run` (tbr-runner): drive `app.runner.service.Runner` with a stub API (no server, no database) through the real
job flow – decrypt into the job directory, sandbox unit, encrypted quarantine – and check the reports.
The Docker variant (deploy/docker/data-zones-test.sh) runs the same jobs through the launcher container: paths and
launcher come from `TBR_E2E_*`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    from app import data_crypto
    from app.runner.config import RunnerConfig
    from app.runner.service import Runner, make_launcher
    from cli.client import ApiError
except ImportError:  # `xlsx` step in the runner's analysis venv (openpyxl, no app dependencies)
    if sys.argv[1:] != ["xlsx"]:
        raise
    ApiError = RuntimeError

DATA_DIR = Path(os.environ.get("TBR_E2E_DATA_DIR", "/srv/taskbrigade-data"))
KEY_FILE = Path(os.environ.get("TBR_E2E_KEY_FILE", "/etc/taskbrigade/data.key"))
SPEC = Path(os.environ.get("TBR_E2E_SPEC", "/var/lib/taskbrigade-runner/e2e-spec.json"))
JOBS_DIR = Path("/run/taskbrigade-runner/jobs")
LAUNCHER = os.environ.get("TBR_E2E_LAUNCHER", "systemd")
CSV = "monat,konto,betrag\n" + "".join(f"2026-0{1 + i % 3},{4000 + i % 2},{10 + i}.5\n" for i in range(60))

GOOD = '''
import csv, json, os
from collections import defaultdict
src = os.path.join(os.environ["TBR_INPUT"], "data.csv")
rows = list(csv.DictReader(open(src, newline="")))
if not rows or set(rows[0]) != {"monat", "konto", "betrag"}:
    raise SystemExit(3)
groups = defaultdict(lambda: [0, 0.0])
for r in rows:
    g = groups[r["monat"]]
    g[0] += 1
    g[1] += float(r["betrag"])
result = {"format": "tbr-aggregate/1",
          "tables": [{"name": "per_month", "columns": ["monat", "n", "sum"], "count_column": "n",
                      "rows": [[k, n, round(s, 2)] for k, (n, s) in sorted(groups.items())]}],
          "metrics": {"rows_total": len(rows)}}
with open(os.path.join(os.environ["TBR_OUTPUT_DIR"], "result.json"), "w") as f:
    json.dump(result, f)
print("aggregated", len(rows), "rows")
'''
SCHEMA = 'import os\nprint("expected column missing")\nraise SystemExit(3)\n'
SNEAKY = (f'data = open("{KEY_FILE}").read()\n'
          'open("/job/out/result.json", "w").write(data)\n')
NETWORK = 'import socket\nsocket.create_connection(("1.1.1.1", 53))\n'


class Response:
    def __init__(self, status: int, body=None, content: bytes = b""):
        self.status_code, self._body, self.content = status, body, content

    def json(self):
        return self._body


class StubClient:
    """Answers the runner's API calls for one task like the server would."""

    def __init__(self, spec: dict, script: str):
        self.spec, self.script, self.claimed = spec, script, False
        self.result = self.failure = None

    def request(self, method: str, path: str, **kw):
        if path == "/agents/me/claim":
            if self.claimed:
                return Response(204)
            self.claimed = True
            return Response(200, {"task_nr": "DEMO-0001", "task_kind": "run"})
        if path.endswith("/script"):
            return Response(200, content=self.script.encode())
        if path.endswith("/result"):
            self.result = kw["json"]
            return Response(200, {"result_id": 1, "status": "for_review"})
        if path.endswith("/fail"):
            self.failure = kw["json"]
            return Response(200, {"status": "aborted"})
        if path.endswith("/heartbeat"):
            return Response(200, {})
        raise ApiError(404, "not_found", path)

    def get(self, path: str):
        return dict(self.spec)


def xlsx() -> bytes:
    """The CSV as a workbook (needs openpyxl: the app venv natively, the runner venv in Docker – `xlsx` step)."""
    import csv
    import io
    from openpyxl import Workbook
    book = Workbook()
    for row in csv.reader(io.StringIO(CSV)):
        book.active.append(row)
    blob = io.BytesIO()
    book.save(blob)
    return blob.getvalue()


def prepare() -> None:
    kek = data_crypto.load_kek(KEY_FILE)
    dek = data_crypto.new_dek()
    target = DATA_DIR / "store" / "DS-0001.enc"
    with open(target, "wb") as f:
        writer = data_crypto.EncryptingWriter(f, dek)
        writer.write(CSV.encode())
        writer.close()
    target.chmod(0o640)
    import grp
    import os

    os.chown(target, 0, grp.getgrnam("tbr-data").gr_gid)  # Like the app's upload: group tbr-data, 0640
    spec = {"task_nr": "DEMO-0001", "task_kind": "run", "project": "DEMO",
            "dataset": {"key": "DS-0001", "storage_path": "store/DS-0001.enc", "suffix": ".csv",
                        "sha256": hashlib.sha256(CSV.encode()).hexdigest(), "size_bytes": len(CSV),
                        "dek_wrapped": base64.b64encode(data_crypto.wrap_dek(kek, dek, "DS-0001")).decode()},
            "script": {"path": "analysis/per_month.py", "commit_sha": "a" * 40},
            "limits": {"timeout_s": 60, "memory_mb": 512, "cpu_percent": 100}}
    raw = Path(os.environ["TBR_E2E_XLSX"]).read_bytes() if os.environ.get("TBR_E2E_XLSX") else xlsx()
    target = DATA_DIR / "store" / "DS-0002.enc"
    with target.open("wb") as f:
        writer = data_crypto.EncryptingWriter(f, dek)
        writer.write(raw)
        writer.close()
    target.chmod(0o640)
    os.chown(target, 0, grp.getgrnam("tbr-data").gr_gid)
    spec["xlsx_dataset"] = dict(spec["dataset"], key="DS-0002", storage_path="store/DS-0002.enc", suffix=".xlsx",
                                sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
                                dek_wrapped=base64.b64encode(data_crypto.wrap_dek(kek, dek, "DS-0002")).decode())
    SPEC.write_text(json.dumps(spec))
    os.chown(SPEC, -1, grp.getgrnam("tbr-data").gr_gid)
    SPEC.chmod(0o640)
    print("prepared store/DS-0001.enc")


def _runner(spec: dict, script: str) -> tuple[Runner, StubClient]:
    cfg = RunnerConfig(url="http://stub", data_dir=DATA_DIR, data_key_file=KEY_FILE, jobs_dir=JOBS_DIR,
                       launcher=LAUNCHER)
    client = StubClient(spec, script)
    return Runner(cfg, client, make_launcher(cfg)), client


def _decrypt(report: dict, name: str) -> bytes:
    kek = data_crypto.load_kek(KEY_FILE)
    dek = data_crypto.unwrap_dek(kek, base64.b64decode(report["dek_wrapped"]), f"run-result/{report['job_id']}")
    entry = next(f for f in report["files"] if f["name"] == name)
    return data_crypto.decrypt_file(DATA_DIR / entry["path"], dek)


def run() -> int:
    spec = json.loads(SPEC.read_text())
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {label}{': ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    runner, client = _runner(spec, GOOD)
    runner.run_once()
    report = client.result or {}
    check("good script: exit 0, success", report.get("exit_code") == 0 and report.get("service_result") == "success",
          f"exit={report.get('exit_code')} result={report.get('service_result')}")
    names = sorted(f["name"] for f in report.get("files", []))
    check("good script: result.json + logs in quarantine", names == ["result.json", "stderr.log", "stdout.log"],
          str(names))
    if "result.json" in names:
        result = json.loads(_decrypt(report, "result.json"))
        check("good script: aggregate decrypts", result["metrics"]["rows_total"] == 60,
              json.dumps(result["tables"][0]["rows"]))
        stored = (DATA_DIR / report["files"][0]["path"]).read_bytes()
        check("quarantine holds ciphertext only", b"per_month" not in stored and b"2026-0" not in stored)
        check("stdout captured in its log", b"aggregated 60 rows" in _decrypt(report, "stdout.log"))
    check("job directory removed", not any(JOBS_DIR.iterdir()))

    profiler_spec = dict(spec, task_kind="profile", min_group_size=5)
    runner, client = _runner(profiler_spec, "raise RuntimeError('must not run repository code')")
    runner.run_once()
    report = client.result or {}
    check("profiler: sandbox exit 0", report.get("exit_code") == 0
          and report.get("service_result") == "success")
    names = {f["name"] for f in report.get("files", [])}
    check("profiler: schema and synthetic sample encrypted", {"schema.json", "sample.csv", "result.json"} <= names)
    if "sample.csv" in names:
        import csv
        import io
        sample = list(csv.reader(io.StringIO(_decrypt(report, "sample.csv").decode())))
        original = {cell for row in list(csv.reader(io.StringIO(CSV)))[1:] for cell in row}
        check("profiler: no original cell value in sample", len(sample) == 21
              and not original.intersection(cell for row in sample[1:] for cell in row if cell))
        schema = json.loads(_decrypt(report, "schema.json"))
        meta = json.loads(_decrypt(report, "result.json"))
        from app.runner.profiler import fingerprint
        check("profiler: stable fingerprint", fingerprint(schema) == meta["schema_fingerprint"])
    check("profiler: plaintext job removed", not any(JOBS_DIR.iterdir()))

    runner, client = _runner(dict(profiler_spec, dataset=spec["xlsx_dataset"]), "")
    runner.run_once()
    report = client.result or {}
    check("XLSX profiler: sandbox exit 0", report.get("exit_code") == 0
          and report.get("service_result") == "success")
    if report.get("exit_code") == 0:
        import csv
        import io
        sample = list(csv.reader(io.StringIO(_decrypt(report, "sample.csv").decode())))
        original = {cell for row in list(csv.reader(io.StringIO(CSV)))[1:] for cell in row}
        check("XLSX profiler: no original cells", len(sample) == 21
              and not original.intersection(cell for row in sample[1:] for cell in row if cell))

    runner, client = _runner(spec, SCHEMA)
    runner.run_once()
    check("schema mismatch reports exit code 3", (client.result or {}).get("exit_code") == 3,
          str((client.result or {}).get("exit_code")))

    runner, client = _runner(spec, SNEAKY)
    runner.run_once()
    rep = client.result or {}
    check("reading the data key from the job fails", rep.get("exit_code") not in (0, None)
          and "result.json" not in [f["name"] for f in rep.get("files", [])],
          f"exit={rep.get('exit_code')}")

    runner, client = _runner(spec, NETWORK)
    runner.run_once()
    check("pre-check stops a socket import before any decrypt", (client.failure or {}).get("code") == "precheck"
          and client.result is None, json.dumps(client.failure))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["xlsx"]:
        sys.stdout.buffer.write(xlsx())
        raise SystemExit(0)
    raise SystemExit(prepare() or 0 if sys.argv[1:] == ["prepare"] else run())
