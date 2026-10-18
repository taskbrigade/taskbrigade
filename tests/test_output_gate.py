"""Output gate with DB (research/konzept-datenzonen.md §6/§8, DZ 8): the runner flow through the automatic checks, the
release view, four-eyes, reject/keep in zone R, the inbox category, the audit trail – and that nothing unreleased
reaches an agent. Real server, data store, crypto and probe; the sandbox is a plain subprocess
(`tests/test_runner_jobs.SubprocessLauncher`)."""
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor, DataAudit, Project, RunResult, Task, TaskEvent
from app.runner.sandbox import Outcome
from app.services import data_audit, output_gate, settings_store
from tests.conftest import bearer, make_token
from tests.test_data_store import PW, _set_actor, _stored, denv  # noqa: F401 (Fixture)
from tests.test_runner_jobs import SHA, SCRIPT, SubprocessLauncher, mkrun, renv, runner, task  # noqa: F401
from tests.test_sprint_editor import login_as
from tests.test_workflow import env, gitea  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db

CUSTOMERS = ("customer;contact;account;amount\n" + "".join(
    f"Customer {i:03d};c{i:03d}@example.org;{4400 + 10 * (i % 2)};{i}.50\n" for i in range(12)))
SECRET = "Customer 007"

PER_ACCOUNT = '''
import csv, json, os
rows = list(csv.DictReader(open(os.path.join(os.environ["TBR_INPUT"], "data.csv")), delimiter=";"))
counts, totals = {}, {}
for r in rows:
    counts[r["account"]] = counts.get(r["account"], 0) + 1
    totals[r["account"]] = totals.get(r["account"], 0) + float(r["amount"]) / 3
json.dump({"format": "tbr-aggregate/1", "tables": [{"name": "per_account", "columns": ["account", "n", "total"],
           "count_column": "n", "rows": [[k, counts[k], totals[k]] for k in sorted(counts)]}],
           "metrics": {"rows": len(rows)}}, open(os.path.join(os.environ["TBR_OUTPUT_DIR"], "result.json"), "w"))
'''
LEAK = PER_ACCOUNT.replace('[[k, counts[k], totals[k]] for k in sorted(counts)]',
                           '[[r["customer"].upper(), 6, 1.0] for r in rows]')
# Raw rows of the bookings sample (renv dataset, k = 2), columns reordered and the date reformatted
RAW = PER_ACCOUNT.replace('"columns": ["account", "n", "total"]', '"columns": ["a", "b", "n", "c"]').replace(
    '[[k, counts[k], totals[k]] for k in sorted(counts)]',
    '[[r["amount"] + "0", r["account"], 6, " " + r["booking_date"]] for r in rows]')
EXTRA = PER_ACCOUNT + 'open(os.path.join(os.environ["TBR_OUTPUT_DIR"], "rows.csv"), "w").write("x")\n'
NEW_SHA = "beef" + "0" * 36


@pytest.fixture
def genv(renv, tmp_path):  # noqa: F811
    """renv plus a customer dataset (k = 5, the default) and bob with the data right as a second person."""
    e = renv
    e["customers"] = _stored(login_as("alex", PW), content=CUSTOMERS.encode(), filename="customers.csv")
    _set_actor("bob", data_access=True)
    e["gitea"].commit_files[SHA] = {SCRIPT: PER_ACCOUNT}
    e["tmp"] = tmp_path
    e["alex_ui"], e["bob_ui"] = login_as("alex", PW), login_as("bob", PW)
    e["admin_ui"] = login_as("admin", PW)
    e["claude"] = bearer(make_token("claude-code"))
    return e


def run(e, script=None, launcher=None, dataset=None) -> tuple[str, RunResult]:
    if script is not None:
        e["gitea"].commit_files[SHA] = {SCRIPT: script}
    r = mkrun(e, commit_sha=SHA, script_path=SCRIPT, dataset=dataset or e["customers"])
    assert r.status_code == 201, r.text
    nr = r.json()["task_nr"]
    assert runner(e, e["tmp"], launcher).run_once() == nr
    return nr, latest(nr)


def latest(nr) -> RunResult:
    with db.session() as s:
        task_id = s.scalar(select(Task.id).where(Task.task_nr == nr))
        return s.scalars(select(RunResult).where(RunResult.task_id == task_id).order_by(RunResult.id.desc())).first()


def gate(result) -> dict:
    return {c["n"]: c for c in json.loads(result.checks_json)["checks"]}


def decide(c, result_id, decision, note=""):
    return c.post(f"/data/results/{result_id}/decide", data={"csrf_token": c.csrf, "decision": decision,
                                                              "note": note}, follow_redirects=False)


def audit(action=None):
    with db.session() as s:
        rows = s.scalars(select(DataAudit).order_by(DataAudit.id)).all()
        return [r for r in rows if action is None or r.action == action]


def cloud_task(e) -> str:
    """A loosened cloud_ok task for claude-code in the local_only project: a cloud step that may read releases."""
    r = e["c"].post("/api/v1/tasks", headers=e["admin"], json={
        "project": "DEMO", "title": "Interpret result", "description": "d", "acceptance_criteria": "a",
        "data_class": "cloud_ok", "assigned_to": "claude-code"})
    assert r.status_code == 201, r.text
    return r.json()["task_nr"]


# --- Automatic checks in the runner flow -------------------------------------------------------------------------

def test_passing_result_waits_for_release(genv):
    e = genv
    nr, result = run(e)
    assert result.status == "pending_approval" and task(e, nr)["status"] == "for_review"
    checks = gate(result)
    assert [checks[n]["status"] for n in range(1, 10)] == ["ok"] * 6 + ["skipped", "ok", "applied"]
    assert checks[4]["params"]["identifier_columns"] >= 2 and checks[5]["params"]["input_rows"] == 12
    assert SECRET not in result.checks_json and result.released_json is None
    thread = " ".join(c["body"] for c in task(e, nr)["comments"])
    assert "Output-Gate" in thread and SECRET not in thread
    assert [a.detail_json for a in audit("gate_checked")][-1].count("pending_approval") == 1


@pytest.mark.parametrize("script, n, code", [(LEAK, 4, "identifier_values"), (RAW, 5, "raw_rows"),
                                             (EXTRA, 1, "extra_files")], ids=["identifiers", "raw_rows", "extra"])
def test_hard_failure_sends_the_run_task_back(genv, script, n, code):
    e = genv
    nr, result = run(e, script, dataset=e["dataset"] if n == 5 else None)
    checks = gate(result)
    assert result.status == "checks_failed" and (checks[n]["status"], checks[n]["code"]) == ("failed", code)
    t = task(e, nr)
    assert t["status"] == "changes_requested" and t["run"]["commit_sha"] is None
    for text in (result.checks_json, json.dumps(t), *[a.detail_json or "" for a in audit()]):
        assert SECRET not in text and SECRET.upper() not in text and "example.org" not in text.lower()
    # The runner does not run the same script again …
    assert e["c"].post("/api/v1/agents/me/claim", headers=e["runner"]).status_code == 204
    # … until a human sets the merged commit of a repaired script.
    with db.session() as s:
        project = s.scalar(select(Project).where(Project.project_key == "DEMO"))
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        s.add(Task(task_nr="DEMO-0950", project_id=project.id, title="Repair", created_by=admin.id, status="done",
                   merge_commit_sha=NEW_SHA, row_version=1, data_class="cloud_ok"))
        s.commit()
    r = e["c"].patch(f"/api/v1/tasks/{nr}", headers=e["admin"], json={
        "run": {"commit_sha": NEW_SHA, "script_path": SCRIPT, "dataset": t["run"]["dataset"]}})
    assert r.status_code == 200, r.text
    assert e["c"].post("/api/v1/agents/me/claim", headers=e["runner"]).json()["task_nr"] == nr


def test_small_groups_fail_with_the_admin_k(genv):
    e = genv
    with db.session() as s:
        settings_store.update(s, {"gate_min_group_size": "7"}, None)
        s.commit()
    _nr, result = run(e)  # Six rows per account
    assert result.status == "checks_failed"
    assert (gate(result)[3]["code"], gate(result)[3]["params"]["k"]) == ("small_group", 7)


def test_a_failing_probe_fails_closed(genv):
    class NoProbe(SubprocessLauncher):
        def run(self, job, timeout_s):
            if job.job_id.endswith("-gate"):
                return Outcome(datetime.now(), datetime.now(), 1, "exit-code")
            return super().run(job, timeout_s)

    _nr, result = run(genv, launcher=NoProbe())
    assert result.status == "checks_failed" and gate(result)[4]["code"] == "probe_error"


# --- Decisions, four-eyes, audit -----------------------------------------------------------------------------------

def test_release_needs_a_second_person_and_is_audited(genv):
    e = genv
    nr, result = run(e)
    with db.session() as s:  # bob merged the script: involved like the run task's creator (admin)
        code = s.scalar(select(Task).where(Task.merge_commit_sha == SHA))
        s.add(TaskEvent(task_id=code.id, actor_id=s.scalar(select(Actor.id).where(Actor.name == "bob")),
                        event_type="merged"))
        s.commit()
    for c in (e["admin_ui"], e["bob_ui"]):
        page = c.get(f"/data/results/{result.id}").text
        assert "Vier-Augen-Regel: Eine zweite Person muss freigeben" in page and 'value="release"' not in page
        r = decide(c, result.id, "release")
        assert r.status_code == 303 and "err=" in r.headers["location"]
    assert latest(nr).status == "pending_approval"
    r = decide(e["alex_ui"], result.id, "release", "Looks fine")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    result = latest(nr)
    assert result.status == "released" and task(e, nr)["status"] == "done"
    released = json.loads(result.released_json)
    assert released["tables"][0]["rows"][0][2] == round(released["tables"][0]["rows"][0][2], 2)
    assert json.loads(result.approvals_json)[0]["actor"] == "alex"
    row = audit("result_released")[-1]
    detail = json.loads(row.detail_json)
    assert detail.pop("release_policy") == json.loads(result.release_policy_json)
    assert detail == {"decision": "release", "four_eyes": True, "note": "Looks fine", "task": nr}
    with db.session() as s:
        assert data_audit.verify(s).ok
    page = e["alex_ui"].get(f"/data/results/{result.id}").text
    assert "Freigegebene Ausgabe" in page and "Looks fine" in page and 'value="release"' not in page


def test_four_eyes_can_be_switched_off_per_project(genv):
    e = genv
    nr, result = run(e)
    r = e["c"].patch("/api/v1/projects/DEMO", headers=e["admin"], json={"data_four_eyes": False})
    assert r.status_code == 200, r.text
    assert audit("four_eyes_changed")
    assert decide(e["admin_ui"], result.id, "release").headers["location"].count("msg=") == 1
    assert latest(nr).status == "released"


def test_reject_needs_a_reason_and_keep_sealed_finishes_without_release(genv):
    e = genv
    nr, result = run(e)
    assert "err=" in decide(e["alex_ui"], result.id, "reject").headers["location"]
    assert decide(e["alex_ui"], result.id, "reject", "Wrong grouping").status_code == 303
    assert latest(nr).status == "rejected"
    t = task(e, nr)
    assert t["status"] == "changes_requested" and t["run"]["commit_sha"] is None
    assert "Wrong grouping" not in json.dumps(t)  # The note stays in the gate, not in the agent-visible thread
    # A failed result can stay in zone R (results without a zone change, research/konzept-workflows.md §11.2).
    e["gitea"].commit_files[SHA] = {SCRIPT: LEAK}
    nr2, failed = run(e)
    assert failed.status == "checks_failed"
    assert decide(e["bob_ui"], failed.id, "keep_sealed", "List for accounting").status_code == 303
    assert latest(nr2).status == "kept_sealed" and task(e, nr2)["status"] == "done"
    assert latest(nr2).released_json is None
    assert {"result_rejected", "result_kept_sealed"} <= {a.action for a in audit()}


def test_only_deciders_see_the_release_view(genv):
    e = genv
    _nr, result = run(e)
    page = e["alex_ui"].get(f"/data/results/{result.id}")
    assert page.status_code == 200 and "4400" in page.text and "Automatische Prüfungen" in page.text
    assert "result_viewed" in {a.action for a in audit()}
    _set_actor("bob", data_access=False)  # Member without the data right
    assert e["bob_ui"].get(f"/data/results/{result.id}").status_code == 404
    assert decide(e["bob_ui"], result.id, "release").status_code == 404
    carol = login_as("carol", PW)  # Not a member
    assert carol.get(f"/data/results/{result.id}").status_code == 404


def test_generic_review_actions_cannot_decide_runner_results(genv):
    e = genv
    nr, result = run(e)
    for decision in ("approve", "reject", "reject_conflict"):
        r = e["c"].post(f"/api/v1/tasks/{nr}/review", headers=e["alex"], json={"decision": decision,
                                                                             "comment": "x"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "output_gate_required", decision
    from app.errors import Conflict
    from app.services.state_machine import transition

    with db.session() as s:
        t = s.scalar(select(Task).where(Task.task_nr == nr))
        with pytest.raises(Conflict) as err:
            transition(s, t, "gate_release", None, {"_gate": 1, "body": "x"})  # No result object → refused
        assert err.value.code == "output_gate_required"
    assert latest(nr).status == "pending_approval"


# --- Nothing unreleased reaches an agent ---------------------------------------------------------------------------

def test_only_released_results_reach_cloud_agents(genv):
    e = genv
    cloud_task(e)
    nr, result = run(e)
    pending = e["c"].get(f"/api/v1/data/results/{result.id}", headers=e["claude"])
    assert pending.status_code == 404
    assert e["c"].get(f"/api/v1/data/results/{result.id}", headers=e["runner"]).status_code == 404
    t = e["c"].get(f"/api/v1/tasks/{nr}", headers=e["claude"])
    assert "4400" not in t.text  # Task payload and thread: exit code, runtime and check numbers only
    decide(e["alex_ui"], result.id, "release")
    # An open task in the project is not enough (DZ 9): the agent's task must reference the result.
    assert e["c"].get(f"/api/v1/data/results/{result.id}", headers=e["claude"]).status_code == 404
    follow = task(e, nr)["follow_ups"]
    assert len(follow) == 1 and follow[0]["status"] == "draft"  # Proposed "Interpret result" draft
    interpret = follow[0]["task_nr"]
    assert [r["label"] for r in task(e, interpret)["data_refs"]] == [f"result#{result.id}"]
    r = e["c"].patch(f"/api/v1/tasks/{interpret}", headers=e["admin"], json={
        "assigned_to": "claude-code", "acceptance_criteria": "Explain the totals."})
    assert r.status_code == 200, r.text
    assert e["c"].get(f"/api/v1/data/results/{result.id}", headers=e["claude"]).status_code == 404  # Still a draft
    assert e["c"].post(f"/api/v1/tasks/{interpret}/transition", headers=e["admin"],
                       json={"to": "defined"}).status_code == 200
    got = e["c"].get(f"/api/v1/data/results/{result.id}", headers=e["claude"])
    assert got.status_code == 200, got.text
    assert got.json()["content"]["tables"][0]["rows"][0][0] in ("4400", "4410") and got.json()["task"] == nr
    assert "result_read" in {a.action for a in audit()}
    # Agents without a task referencing the result get nothing, released or not.
    assert e["c"].get(f"/api/v1/data/results/{result.id}", headers=e["hermes"]).status_code == 404
    # Every other status stays invisible.
    for script, decision in ((LEAK, None), (PER_ACCOUNT, "reject"), (PER_ACCOUNT, "keep_sealed"),
                             (PER_ACCOUNT, None)):
        _nr, other = run(e, script)
        if decision:
            decide(e["alex_ui"], other.id, decision, "no")
        assert latest(_nr).status != "released"
        r = e["c"].get(f"/api/v1/data/results/{other.id}", headers=e["claude"])
        assert r.status_code == 404, (script[:20], decision)


def test_inbox_lists_data_releases_instead_of_a_review(genv):
    e = genv
    nr, result = run(e)
    page = e["alex_ui"].get("/").text
    assert "Datenfreigaben" in page and f"/data/results/{result.id}" in page
    assert f'hx-post="/inbox/{nr}/approve"' not in page
    detail = e["alex_ui"].get(f"/tasks/{nr}").text
    assert "Freigabe-Ansicht öffnen" in detail and 'value="approve"' not in detail
    assert f"/data/results/{result.id}" in e["alex_ui"].get(f"/data/{e['customers']}").text
    decide(e["alex_ui"], result.id, "release")
    assert "Datenfreigaben" not in e["alex_ui"].get("/").text


def test_keep_sealed_by_system_for_workflows(genv):
    e = genv
    nr, result = run(e)
    with db.session() as s:
        output_gate.keep_sealed_by_system(s, s.get(RunResult, result.id), "workflow without gate")
        s.commit()
    assert latest(nr).status == "kept_sealed" and task(e, nr)["status"] == "done"
    assert audit("result_kept_sealed")[-1].via == "scheduler"
