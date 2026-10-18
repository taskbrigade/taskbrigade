"""Data-zone runner with DB (research/konzept-datenzonen.md §2/§5/§6): runner agents, run tasks, claim rules, the job
API and the whole runner flow – real server, real data store and crypto, the sandbox replaced by a plain
subprocess (the systemd sandbox itself: tests/test_runner_sandbox.py and deploy/sandbox-test/run.sh)."""
import base64
import json
import os
import subprocess
import sys
from datetime import datetime

import pytest
from sqlalchemy import select

from app import data_crypto
from app.db import db
from app.models import Actor, DataAudit, DatasetProfile, Project, RunResult, Task
from app.runner.config import RunnerConfig
from app.runner.sandbox import Outcome
from app.runner.service import Runner
from app.services import runner_jobs, settings_store
from cli.client import ApiError
from tests.test_data_store import PW, _stored, denv  # noqa: F401 (Fixture)
from tests.test_sprint_editor import login_as
from tests.test_workflow import env, gitea, mktask  # noqa: F401 (Fixtures)
from tests.conftest import BOOTSTRAP_PW, bearer, make_token

pytestmark = pytest.mark.db

SHA = "c0ffee" + "0" * 34
SCRIPT = "analysis/per_account.py"
GOOD = '''
import csv, json, os
rows = list(csv.DictReader(open(os.path.join(os.environ["TBR_INPUT"], "data.csv")), delimiter=";"))
if not rows or "account" not in rows[0]:
    raise SystemExit(3)
counts = {}
for r in rows:
    counts[r["account"]] = counts.get(r["account"], 0) + 1
with open(os.path.join(os.environ["TBR_OUTPUT_DIR"], "result.json"), "w") as f:
    json.dump({"format": "tbr-aggregate/1", "tables": [{"name": "per_account", "columns": ["account", "n"],
              "count_column": "n", "rows": sorted([k, v] for k, v in counts.items())}], "metrics": {}}, f)
print("SECRET-ROW-MARKER", rows[0])
'''
SCHEMA = "raise SystemExit(3)\n"


class ApiAdapter:
    """`cli.client.Client` interface on top of the in-process TestClient."""

    def __init__(self, c, headers):
        self.c, self.headers = c, headers

    def request(self, method, path, **kw):
        r = self.c.request(method, "/api/v1" + path, headers=self.headers, **kw)
        if r.status_code >= 400:
            err = r.json().get("error", {})
            raise ApiError(r.status_code, err.get("code", "error"), err.get("message", ""))
        return r

    def get(self, path):
        return self.request("GET", path).json()


class SubprocessLauncher:
    """Stand-in for the systemd unit: same environment contract, no isolation."""

    def __init__(self):
        self.ran = []

    def run(self, job, timeout_s):
        self.ran.append(job.job_id)
        env = {"TBR_INPUT": str(job.path("in")), "TBR_OUTPUT_DIR": str(job.path("out")), "PATH": os.environ["PATH"]}
        started = datetime.now()
        with open(job.path("log") / "stdout.log", "ab") as out, open(job.path("log") / "stderr.log", "ab") as err:
            done = subprocess.run([sys.executable, "-I", "-B", str(job.path("script") / "entry.py")], env=env,
                                  cwd=job.path("script"), stdout=out, stderr=err, timeout=timeout_s)
        return Outcome(started, datetime.now(), done.returncode, "success" if done.returncode == 0 else "exit-code",
                       42)


@pytest.fixture
def renv(denv, tmp_path):  # noqa: F811
    """DEMO (local_only) with data store, dataset uploaded by alex, runner agent `runner-1`, merged script."""
    e = denv
    r = e["c"].post("/api/v1/admin/actors", headers=e["admin"], json={
        "name": "runner-1", "display_name": "Runner", "actor_type": "agent", "agent_kind": "runner"})
    assert r.status_code == 201, r.text
    e["runner"] = bearer(r.json()["token"])
    alex = login_as("alex", PW)
    e["dataset"] = _stored(alex, min_group_size="2")  # Two rows per account in the sample CSV
    e["alex"] = bearer(make_token("alex"))
    with db.session() as s:  # The code task that wrote the script: approved and merged.
        # These tests create explicit jobs; the upload's automatic profile is covered separately.
        from app.services.state_machine import transition
        project = s.scalar(select(Project).where(Project.project_key == "DEMO"))
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        for auto in s.scalars(select(Task).where(Task.task_kind == "profile")):
            transition(s, auto, "cancel", admin)
        s.add(Task(task_nr="DEMO-0900", project_id=project.id, title="Write script", created_by=admin.id,
                   status="done", merge_commit_sha=SHA, row_version=1, data_class="cloud_ok"))
        s.commit()
    e["gitea"].commit_files[SHA] = {SCRIPT: GOOD}
    e["key_file"] = tmp_path / "key" / "data.key"
    return e


def mkrun(e, kind="run", assign="runner-1", **run):
    body = {"project": "DEMO", "title": f"{kind} on data", "description": "d", "acceptance_criteria": "a",
            "task_kind": kind, "assigned_to": assign,
            "run": run or ({"commit_sha": SHA, "script_path": SCRIPT, "dataset": e["dataset"]} if kind == "run"
                           else {"dataset": e["dataset"]})}
    return e["c"].post("/api/v1/tasks", headers=e["admin"], json=body)


def runner(e, tmp_path, launcher=None):
    cfg = RunnerConfig(url="http://test", data_dir=e["data"], data_key_file=e["key_file"],
                       jobs_dir=tmp_path / "jobs", job_group=None)
    return Runner(cfg, ApiAdapter(e["c"], e["runner"]), launcher or SubprocessLauncher())


def task(e, nr):
    return e["c"].get(f"/api/v1/tasks/{nr}", headers=e["admin"]).json()


def audit_actions():
    with db.session() as s:
        return [r.action for r in s.scalars(select(DataAudit).order_by(DataAudit.id))]


# --- Agents and tasks --------------------------------------------------------------------------------------------

def test_runner_agent_has_no_model_and_stays_a_worker(renv):
    e = renv
    me = e["c"].get("/api/v1/agents/me", headers=e["runner"]).json()
    assert (me["agent_kind"], me["model_location"], me["role"]) == ("runner", "none", "worker")
    r = e["c"].post("/api/v1/admin/actors", headers=e["admin"], json={
        "name": "runner-2", "display_name": "R2", "actor_type": "agent", "agent_kind": "runner", "role": "reviewer"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_runner"
    r = e["c"].patch("/api/v1/admin/actors/runner-1", headers=e["admin"], json={"model_location": "cloud"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_model_location"


def test_run_task_fields_are_validated(renv):
    e = renv
    for run, code in (({"commit_sha": "abc", "script_path": SCRIPT, "dataset": e["dataset"]}, "invalid_run_field"),
                      ({"commit_sha": SHA, "script_path": "../x.py", "dataset": e["dataset"]}, "invalid_run_field"),
                      ({"commit_sha": SHA, "script_path": SCRIPT, "dataset": "DS-9999"}, "invalid_run_field"),
                      ({"commit_sha": SHA, "script_path": SCRIPT}, "invalid_run_field")):
        r = mkrun(e, **run)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (run, r.text)
    r = mkrun(e, kind="profile", commit_sha=SHA, dataset=e["dataset"])
    assert r.status_code == 422
    r = e["c"].post("/api/v1/tasks", headers=e["admin"], json={
        "project": "DEMO", "title": "x", "description": "d", "acceptance_criteria": "a", "run": {"dataset": "x"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_run_field"
    created = mkrun(e)
    assert created.status_code == 201, created.text
    t = created.json()
    assert (t["task_kind"], t["data_class_effective"], t["run"]) == (
        "run", "sealed", {"commit_sha": SHA, "script_path": SCRIPT, "dataset": e["dataset"]})


def test_runner_and_llm_agents_never_swap_task_kinds(renv):
    e = renv
    r = mkrun(e, assign="hermes")  # Local LLM agent: may not get a sealed runner task
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    r = e["c"].post("/api/v1/tasks", headers=e["admin"], json={
        "project": "DEMO", "title": "sealed work", "description": "d", "acceptance_criteria": "a",
        "data_class": "sealed", "assigned_to": "runner-1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "runner_task_kind"
    # An old assignment of a work task to the runner is never claimed.
    with db.session() as s:
        runner_id = s.scalar(select(Actor.id).where(Actor.name == "runner-1"))
        project = s.scalar(select(Project).where(Project.project_key == "DEMO"))
        s.add(Task(task_nr="DEMO-0901", project_id=project.id, title="w", created_by=runner_id, status="defined",
                   data_class="sealed", assigned_to=runner_id, row_version=1))
        s.commit()
    assert e["c"].post("/api/v1/agents/me/claim", headers=e["runner"]).status_code == 204


def test_script_contract_flag_in_task_payload(renv):
    e = renv
    work = e["c"].post("/api/v1/tasks", headers=e["admin"], json={
        "project": "DEMO", "title": "Write script", "description": "d", "acceptance_criteria": "a",
        "data_class": "cloud_ok"}).json()
    assert work["script_contract"] is True  # The project holds datasets
    assert mkrun(e).json()["script_contract"] is False


# --- Job flow ----------------------------------------------------------------------------------------------------

def test_runner_flow_puts_the_result_into_quarantine(renv, tmp_path):
    e = renv
    nr = mkrun(e).json()["task_nr"]
    launcher = SubprocessLauncher()
    assert runner(e, tmp_path, launcher).run_once() == nr
    t = task(e, nr)
    assert t["status"] == "for_review", t["comments"]
    with db.session() as s:
        result = s.scalar(select(RunResult))
        files = json.loads(result.files_json)
        assert (result.status, result.exit_code, result.service_result, result.script_commit_sha) == (
            "pending_approval", 0, "success", SHA)  # Output gate checks passed (tests/test_output_gate.py)
        wrapped, job_id = result.dek_wrapped, result.job_id
    assert sorted(f["name"] for f in files) == ["result.json", "stderr.log", "stdout.log"]
    assert all(f["path"].startswith(f"quarantine/{job_id}/") for f in files)
    kek = data_crypto.load_kek(e["key_file"])
    dek = data_crypto.unwrap_dek(kek, wrapped, runner_jobs.result_context(job_id))
    out = {f["name"]: data_crypto.decrypt_file(e["data"] / f["path"], dek) for f in files}
    assert json.loads(out["result.json"])["tables"][0]["rows"] == [["4400", 2]]
    assert b"SECRET-ROW-MARKER" in out["stdout.log"]
    raw = b"".join((e["data"] / f["path"]).read_bytes() for f in files)
    assert b"SECRET-ROW-MARKER" not in raw and b"4400" not in raw  # Ciphertext only
    thread = " ".join(c["body"] for c in t["comments"])
    assert "Exit-Code 0" in thread and "Output-Gate" in thread and "SECRET" not in thread and "4400" not in thread
    assert launcher.ran == [launcher.ran[0], launcher.ran[0] + "-gate"]  # Script job, then the gate probe
    assert not any((tmp_path / "jobs" / j).exists() for j in launcher.ran)  # Plaintext input removed
    assert {"run_started", "run_finished"} <= set(audit_actions())
    r = e["c"].post(f"/api/v1/tasks/{nr}/review", headers=e["alex"], json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "output_gate_required"


def test_exit_code_3_aborts_as_schema_mismatch(renv, tmp_path):
    e = renv
    e["gitea"].commit_files[SHA] = {SCRIPT: SCHEMA}
    nr = mkrun(e).json()["task_nr"]
    runner(e, tmp_path).run_once()
    t = task(e, nr)
    assert t["status"] == "aborted"
    assert "Exit-Code 3" in t["comments"][-1]["body"] and "Schema" in t["comments"][-1]["body"]  # UI_LANGUAGE=de
    with db.session() as s:
        assert s.scalar(select(RunResult.exit_code)) == 3


def test_unmerged_script_never_runs(renv, tmp_path):
    e = renv
    other = "b" * 40
    e["gitea"].commit_files[other] = {SCRIPT: GOOD}
    nr = mkrun(e, commit_sha=other, script_path=SCRIPT, dataset=e["dataset"]).json()["task_nr"]
    launcher = SubprocessLauncher()
    runner(e, tmp_path, launcher).run_once()
    t = task(e, nr)
    assert t["status"] == "aborted" and "nicht gemergt" in t["comments"][-1]["body"]
    assert launcher.ran == []


def test_precheck_hit_aborts_before_decrypting(renv, tmp_path):
    e = renv
    e["gitea"].commit_files[SHA] = {SCRIPT: "import socket\n"}
    nr = mkrun(e).json()["task_nr"]
    launcher = SubprocessLauncher()
    runner(e, tmp_path, launcher).run_once()
    t = task(e, nr)
    assert t["status"] == "aborted" and "line 1: import socket" in t["comments"][-1]["body"]
    assert launcher.ran == [] and "run_failed" in audit_actions()


def test_profile_jobs_generate_local_artifacts(renv, tmp_path):
    e = renv
    nr = mkrun(e, kind="profile").json()["task_nr"]
    runner(e, tmp_path).run_once()
    t = task(e, nr)
    assert t["status"] == "for_review", t["comments"]
    with db.session() as s:
        row = s.scalar(select(DatasetProfile))
        assert row.status == "generated" and len(row.schema_fingerprint) == 64
        assert "4400" not in row.sample_csv
        pid = row.id
    assert e["c"].get(f"/api/v1/data/profiles/{pid}", headers=e["hermes"]).status_code == 404
    assert e["c"].get(f"/api/v1/data/profiles/{pid}", headers=e["claude"]).status_code == 404
    alex = login_as("alex", PW)
    page = alex.get(f"/data/{e['dataset']}")
    assert page.status_code == 200 and "Synthetische Beispieldaten" in page.text
    self_release = alex.post(f"/data/{e['dataset']}/profile", data={
        "csrf_token": alex.csrf, "decision": "release", "profile_id": pid}, follow_redirects=False)
    assert self_release.status_code == 403 and "zweiter Mensch" in self_release.text
    approver = login_as("admin", BOOTSTRAP_PW)
    released = approver.post(f"/data/{e['dataset']}/profile", data={
        "csrf_token": approver.csrf, "decision": "release", "alias_s0c1": "synthetic_account", "profile_id": pid},
        follow_redirects=False)
    assert released.status_code == 303, released.text
    assert task(e, nr)["status"] == "done"
    with db.session() as s:
        row = s.get(DatasetProfile, pid)
        assert row.status == "released" and row.released_by is not None
        assert "synthetic_account" in row.schema_json
        draft = s.get(Task, row.code_task_id)
        assert draft.status == "draft" and draft.data_class == "cloud_ok"
    assert "profile_released" in audit_actions()
    # The profile API remains scoped to the explicit handoff after release.
    url = f"/api/v1/data/profiles/{pid}"
    assert e["c"].get(url, headers=e["claude"]).status_code == 404
    with db.session() as s:
        row = s.get(DatasetProfile, pid)
        draft = s.get(Task, row.code_task_id)
        code_nr = draft.task_nr
    r = e["c"].patch(f"/api/v1/tasks/{code_nr}", headers=e["admin"], json={
        "assigned_to": "claude-code", "acceptance_criteria": "Use the approved schema."})
    assert r.status_code == 200, r.text
    assert e["c"].post(f"/api/v1/tasks/{code_nr}/transition", headers=e["admin"],
                       json={"to": "defined"}).status_code == 200
    assert e["c"].post("/api/v1/agents/me/claim", headers=e["claude"],
                       json={"task_nr": code_nr}).status_code == 200
    r = e["c"].get(url, headers=e["claude"])
    assert r.status_code == 200 and "synthetic_account" in r.text and "aliases_json" not in r.text
    assert e["c"].get(url, headers=e["hermes"]).status_code == 404
    repeated = approver.post(f"/data/{e['dataset']}/profile", data={
        "csrf_token": approver.csrf, "decision": "release", "profile_id": pid}, follow_redirects=False)
    assert repeated.status_code == 409
    # The released alias is applied inside the same job launcher before executing reviewed code.
    e["gitea"].commit_files[SHA] = {SCRIPT: GOOD.replace("account", "synthetic_account")}
    run_nr = mkrun(e).json()["task_nr"]
    assert runner(e, tmp_path).run_once() == run_nr
    assert task(e, run_nr)["status"] == "for_review"


def test_profile_reject_and_stale_form_do_not_release(renv, tmp_path):
    e = renv
    nr = mkrun(e, kind="profile").json()["task_nr"]
    runner(e, tmp_path).run_once()
    with db.session() as s:
        pid = s.scalar(select(DatasetProfile.id))
    alex = login_as("alex", PW)
    url = f"/data/{e['dataset']}/profile"
    stale = alex.post(url, data={"csrf_token": alex.csrf, "decision": "release", "profile_id": pid + 1},
                      follow_redirects=False)
    assert stale.status_code == 409
    bob = login_as("bob", PW)  # Member without data right: no profile data/control.
    assert "Schema-Profil" not in bob.get(f"/data/{e['dataset']}").text
    denied = bob.post(url, data={"csrf_token": bob.csrf, "decision": "release", "profile_id": pid},
                      follow_redirects=False)
    assert denied.status_code == 403
    rejected = alex.post(url, data={"csrf_token": alex.csrf, "decision": "reject", "profile_id": pid},
                         follow_redirects=False)
    assert rejected.status_code == 303
    assert task(e, nr)["status"] == "changes_requested"
    with db.session() as s:
        row = s.get(DatasetProfile, pid)
        assert row.status == "rejected" and row.released_at is None and row.code_task_id is None
    assert "profile_rejected" in audit_actions()


def test_upload_automatically_queues_a_profile(renv, tmp_path):
    e = renv
    key = _stored(login_as("alex", PW))
    with db.session() as s:
        from app.models import Dataset
        ds = s.scalar(select(Dataset).where(Dataset.dataset_key == key))
        job = s.scalar(select(Task).where(Task.run_dataset_id == ds.id))
        nr = job.task_nr
        assert job.task_kind == "profile" and job.data_class == "sealed" and job.status == "defined"
        assert job.assigned_to is None and ds.status == "profiling"
    # Membership is required for unassigned jobs.
    # Assigning explicitly also establishes project membership via the ordinary task service.
    r = e["c"].patch(f"/api/v1/tasks/{nr}", headers=e["admin"], json={"assigned_to": "runner-1"})
    assert r.status_code == 200, r.text
    assert runner(e, tmp_path).run_once() == nr
    assert task(e, nr)["status"] == "for_review"


def test_job_api_is_for_the_assigned_runner_only(renv):
    e = renv
    nr = mkrun(e).json()["task_nr"]
    r = e["c"].get(f"/api/v1/runner/jobs/{nr}", headers=e["hermes"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_a_runner"
    r = e["c"].get(f"/api/v1/runner/jobs/{nr}", headers=e["runner"])
    assert r.status_code == 409  # Not claimed yet
    assert e["c"].post("/api/v1/agents/me/claim", headers=e["runner"]).json()["task_nr"] == nr
    spec = e["c"].get(f"/api/v1/runner/jobs/{nr}", headers=e["runner"]).json()
    assert spec["dataset"]["key"] == e["dataset"] and spec["dataset"]["suffix"] == ".csv"
    assert base64.b64decode(spec["dataset"]["dek_wrapped"]) and spec["limits"]["timeout_s"] == 900
    assert e["c"].get(f"/api/v1/runner/jobs/{nr}/script", headers=e["runner"]).text == GOOD
    # The runner cannot submit like a coding agent, and nobody can fake a job result through submit.
    r = e["c"].post(f"/api/v1/tasks/{nr}/submit", headers=e["runner"], json={
        "branch_name": "x", "commit_sha": SHA, "summary": "s", "_run_result": 1})
    assert r.status_code == 422
    r = e["c"].post(f"/api/v1/runner/jobs/{nr}/result", headers=e["runner"], json={
        "job_id": "x", "exit_code": 0, "service_result": "success",
        "files": [{"name": "result.json", "kind": "output", "path": "store/DS-0001.enc", "size": 1,
                   "sha256": "0" * 64}]})
    assert r.status_code == 422  # Files must lie in the job's quarantine folder


def test_limits_follow_admin_settings(renv):
    with db.session() as s:
        assert runner_jobs.limits(s) == {"memory_mb": 4096, "cpu_percent": 200, "timeout_s": 900}
        settings_store.update(s, {"runner_memory_mb": "1024", "runner_timeout_s": "60"}, None)
        s.commit()
        assert runner_jobs.limits(s) == {"memory_mb": 1024, "cpu_percent": 200, "timeout_s": 60}
