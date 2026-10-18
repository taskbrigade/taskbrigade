"""Task chain across zones (research/konzept-datenzonen.md §1/§8, DZ 9): `task_data_refs`, rule 3, the follow-up drafts
at each zone change, the chain view on the task page, and that cloud agents read only released artifacts their own
task references (API and TASK.md). Real server, data store, crypto and runner flow (subprocess launcher)."""
import pytest
from sqlalchemy import select

from app.db import db
from app.errors import Unprocessable
from app.models import DataAudit, DatasetProfile, Project, RunResult, Task, TaskDataRef
from app.services import data_refs
from tests.conftest import bearer, make_token
from tests.gitea_fake import SERVICE_USER
from tests.test_data_store import PW, _set_actor, denv  # noqa: F401 (Fixture)
from tests.test_runner_jobs import GOOD, mkrun, renv, runner, task  # noqa: F401 (Fixtures)
from tests.test_sprint_editor import login_as
from tests.test_workflow import env, gitea  # noqa: F401 (Fixtures)

MERGED = "c" * 40  # Merge commit the fake Gitea reports
SCRIPT = "analysis/per_account.py"


def _api(e, method, path, who="admin", **kw):
    return e["c"].request(method, "/api/v1" + path, headers=e[who], **kw)


def _publish(e, nr, **patch):
    if patch:
        r = _api(e, "PATCH", f"/tasks/{nr}", json=patch)
        assert r.status_code == 200, r.text
    r = _api(e, "POST", f"/tasks/{nr}/transition", json={"to": "defined"})
    assert r.status_code == 200, r.text


def _only_follow_up(e, nr) -> dict:
    follow = task(e, nr)["follow_ups"]
    assert len(follow) == 1, follow
    return task(e, follow[0]["task_nr"])


@pytest.fixture
def cenv(renv, tmp_path):  # noqa: F811
    e = renv
    e["tmp"] = tmp_path
    e["claude"] = bearer(make_token("claude-code"))
    _set_actor("bob", data_access=True)  # Second person for the gate (alex created the drafts, admin merged)
    e["admin_ui"], e["bob_ui"] = login_as("admin", PW), login_as("bob", PW)
    return e


@pytest.mark.db
def test_chain_from_profile_to_interpretation(cenv):
    e = cenv
    # 1. Profile (zone R) → released by a second human → draft "Write analysis script" (zone C, schema reference).
    profile_nr = mkrun(e, kind="profile").json()["task_nr"]
    assert [r["label"] for r in task(e, profile_nr)["data_refs"]] == [e["dataset"]]
    runner(e, e["tmp"]).run_once()
    with db.session() as s:
        pid = s.scalar(select(DatasetProfile.id))
    r = e["admin_ui"].post(f"/data/{e['dataset']}/profile", data={
        "csrf_token": e["admin_ui"].csrf, "decision": "release", "profile_id": pid}, follow_redirects=False)
    assert r.status_code == 303, r.text
    code = _only_follow_up(e, profile_nr)
    assert code["status"] == "draft" and code["data_class_effective"] == "cloud_ok"
    assert [(r["type"], r["purpose"], r["status"]) for r in code["data_refs"]] == [("profile", "schema", "released")]
    page = e["admin_ui"].get(f"/tasks/{code['task_nr']}").text
    assert "zone-chain" in page and f'href="/tasks/{profile_nr}"' in page and f"profile#{pid}" in page

    # 2. The cloud agent reads the released profile only through its own open task (API and TASK.md artifacts).
    _publish(e, code["task_nr"], assigned_to="claude-code", acceptance_criteria="Use the approved schema.")
    assert _api(e, "POST", "/agents/me/claim", "claude", json={"task_nr": code["task_nr"]}).status_code == 200
    got = _api(e, "GET", f"/tasks/{code['task_nr']}/artifacts", "claude")
    assert got.status_code == 200, got.text
    [art] = got.json()["items"]
    assert art["label"] == f"profile#{pid}" and art["content"]["schema"]["format"] == "tbr-schema/1"
    assert "4400" not in art["content"]["sample_csv"]
    with db.session() as s:
        assert s.scalar(select(DataAudit.action).where(DataAudit.action == "profile_read")) == "profile_read"
    assert _api(e, "GET", f"/tasks/{code['task_nr']}/artifacts", "hermes").json()["error"]["code"] == \
        "artifacts_forbidden"
    assert _api(e, "GET", f"/data/profiles/{pid}", "hermes").status_code == 404

    # 3. Script merged → draft "Run script" (zone R) with merge commit, script path from the PR and input dataset.
    branch = task(e, code["task_nr"])["branch_name"]
    e["gitea"].push_branch(SERVICE_USER, "demo", branch)
    e["gitea"].pr_files = [SCRIPT, "tests/test_per_account.py", "README.md"]
    r = _api(e, "POST", f"/tasks/{code['task_nr']}/submit", "claude",
             json={"branch_name": branch, "commit_sha": "d" * 40, "summary": "script"})
    assert r.status_code == 200, r.text
    r = _api(e, "POST", f"/tasks/{code['task_nr']}/review", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    run = _only_follow_up(e, code["task_nr"])
    assert (run["status"], run["task_kind"], run["data_class_effective"]) == ("draft", "run", "sealed")
    assert run["run"] == {"commit_sha": MERGED, "script_path": SCRIPT, "dataset": e["dataset"]}
    assert [(r["type"], r["purpose"]) for r in run["data_refs"]] == [("dataset", "input")]

    # 4. Run → output gate → released by bob → draft "Interpret result" (zone C, output reference).
    e["gitea"].commit_files[MERGED] = {SCRIPT: GOOD}
    _publish(e, run["task_nr"], assigned_to="runner-1")
    assert runner(e, e["tmp"]).run_once() == run["task_nr"]
    with db.session() as s:
        result = s.scalar(select(RunResult).order_by(RunResult.id.desc()))
    assert result.status == "pending_approval"
    r = e["bob_ui"].post(f"/data/results/{result.id}/decide", data={
        "csrf_token": e["bob_ui"].csrf, "decision": "release", "note": ""}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
    interpret = _only_follow_up(e, run["task_nr"])
    assert interpret["status"] == "draft" and interpret["data_class_effective"] == "cloud_ok"
    assert [(r["label"], r["status"]) for r in interpret["data_refs"]] == [(f"result#{result.id}", "released")]

    # The chain view shows all four steps on every task of the chain, with the handovers.
    page = e["admin_ui"].get(f"/tasks/{run['task_nr']}").text
    for nr in (profile_nr, code["task_nr"], run["task_nr"], interpret["task_nr"]):
        assert f'href="/tasks/{nr}"' in page
    assert f"Commit {MERGED[:7]}" in page and f"result#{result.id}" in page and 'aria-current="step"' in page
    assert "Eingabe" in page and "nur Runner" in page  # Box "Data": the input dataset stays in zone R

    # 5. The interpretation agent reads the released aggregate inline – nothing else of zone R.
    _publish(e, interpret["task_nr"], assigned_to="claude-code", acceptance_criteria="Explain the result.")
    [art] = _api(e, "GET", f"/tasks/{interpret['task_nr']}/artifacts", "claude").json()["items"]
    assert art["type"] == "result" and art["content"]["tables"][0]["name"] == "per_account"
    assert "SECRET-ROW-MARKER" not in str(art)
    raw = _api(e, "GET", f"/tasks/{run['task_nr']}/artifacts", "claude")
    assert raw.status_code == 403  # Not its task
    with db.session() as s:
        assert s.scalar(select(Task).where(Task.task_nr == interpret["task_nr"])).follow_up_of_id is not None


@pytest.mark.db
def test_rule_3_references_follow_the_class(cenv):
    e = cenv
    with db.session() as s:
        from app.services import tasks

        project = s.scalar(select(Project).where(Project.project_key == "DEMO"))
        alex = project.owner_id
        from app.models import Actor, Dataset

        owner = s.get(Actor, alex)
        ds = s.scalar(select(Dataset).where(Dataset.dataset_key == e["dataset"]))
        cloud = tasks.create(s, owner, project="DEMO", title="cloud", status="draft", data_class="cloud_ok")
        with pytest.raises(Unprocessable) as err:
            data_refs.add(s, cloud, "dataset", ds.id, owner)
        assert err.value.code == "data_class_ref"
        profile = DatasetProfile(dataset_id=ds.id, profiler_version="1", schema_fingerprint="0" * 64,
                                 schema_json="{}", sample_csv="", status="generated")
        s.add(profile)
        s.flush()
        with pytest.raises(Unprocessable) as err:
            data_refs.add(s, cloud, "profile", profile.id, owner)
        assert err.value.code == "data_ref_unreleased"
        with pytest.raises(Unprocessable) as err:
            data_refs.add(s, cloud, "profile", profile.id, owner, purpose="input")
        assert err.value.code == "invalid_data_ref"
        sealed = tasks.create(s, owner, project="DEMO", title="sealed", status="draft", data_class="sealed")
        data_refs.add(s, sealed, "dataset", ds.id, owner)
        data_refs.add(s, sealed, "profile", profile.id, owner)
        data_refs.add(s, sealed, "profile", profile.id, owner)  # Idempotent
        assert s.query(TaskDataRef).filter_by(task_id=sealed.id).count() == 2
        sealed_nr = sealed.task_nr
        s.commit()
    # Loosening a task with an input dataset (or an unreleased profile) is refused (rule 3).
    r = _api(e, "PATCH", f"/tasks/{sealed_nr}", json={"data_class": "cloud_ok"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "data_class_ref"
    page = e["admin_ui"].get(f"/tasks/{sealed_nr}").text
    assert "Schema" in page and "Wartet auf Freigabe" in page


@pytest.mark.db
def test_run_draft_needs_its_script_path_before_release(cenv):
    e = cenv
    body = {"project": "DEMO", "title": "run later", "description": "d", "acceptance_criteria": "a",
            "task_kind": "run", "status": "draft", "run": {"commit_sha": MERGED, "dataset": e["dataset"]}}
    r = _api(e, "POST", "/tasks", json=body)
    assert r.status_code == 201, r.text
    nr = r.json()["task_nr"]
    r = _api(e, "POST", f"/tasks/{nr}/transition", json={"to": "defined"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "run_fields_incomplete"
    defined = _api(e, "POST", "/tasks", json={**body, "status": "defined"})
    assert defined.status_code == 422  # Created as defined it needs every field at once
    alex = login_as("alex", PW)
    page = alex.get(f"/tasks/{nr}").text
    assert 'action="/tasks/' + nr + '/run-fields"' in page
    r = alex.post(f"/tasks/{nr}/run-fields", data={"csrf_token": alex.csrf, "script_path": SCRIPT,
                                                   "commit_sha": MERGED, "dataset": e["dataset"]},
                  follow_redirects=False)
    assert r.status_code == 303, r.text
    assert task(e, nr)["run"]["script_path"] == SCRIPT
    r = _api(e, "POST", f"/tasks/{nr}/transition", json={"to": "defined"})
    assert r.status_code == 200, r.text


@pytest.mark.db
def test_workflow_and_plain_tasks_get_no_chain(cenv, monkeypatch):
    e = cenv
    r = _api(e, "POST", "/tasks", json={"project": "DEMO", "title": "plain", "description": "d",
                                        "acceptance_criteria": "a"})
    nr = r.json()["task_nr"]
    follow = _api(e, "POST", "/tasks", json={"project": "DEMO", "title": "next", "description": "d",
                                             "acceptance_criteria": "a", "follow_up_of": nr}).json()["task_nr"]
    with db.session() as s:
        t = s.scalar(select(Task).where(Task.task_nr == follow))
        assert data_refs.chain(s, t) is None  # A follow-up link alone is no chain across zones
        data_refs.on_transition(s, t, "approve", None, {})  # No profile reference: no run draft
        assert s.scalar(select(Task.id).where(Task.follow_up_of_id == t.id)) is None

        def broken(*args):
            raise Unprocessable("Project is archived")
        monkeypatch.setattr(data_refs, "propose_interpretation", broken)
        data_refs.on_transition(s, t, "gate_release", None, {"_gate": RunResult(task_id=t.id)})  # Logged, not raised
    assert "zone-chain" not in e["admin_ui"].get(f"/tasks/{follow}").text


# --- TASK.md (agent side, no DB) -----------------------------------------------------------------------------------

class _Client:
    def __init__(self, items=None, fail=False):
        self.items, self.fail, self.paths = items or [], fail, []

    def get(self, path):
        from cli.client import ApiError

        self.paths.append(path)
        if self.fail:
            raise ApiError(404, "not_found", "old server")
        return {"items": self.items}


def test_task_md_lists_references_and_inlines_released_artifacts():
    from cli.prepare import artifacts_note
    from cli.tm import task_markdown

    refs = [{"type": "dataset", "id": 3, "purpose": "input", "label": "DS-0003", "status": "ready"},
            {"type": "result", "id": 12, "purpose": "output", "label": "result#12", "status": "released"},
            {"type": "profile", "id": 8, "purpose": "schema", "label": "profile#8", "status": "generated"}]
    t = {"task_nr": "DEMO-0104", "title": "Interpret", "status": "in_progress", "prio": 3, "data_refs": refs}
    md = task_markdown(t)
    assert "## Data references" in md and "dataset DS-0003 – raw data, only the runner reads it" in md
    assert "- output: result#12 (released)" in md
    items = [{**refs[0], "content": None}, {**refs[1], "content": {"tables": [], "metrics": {"total": 41}}},
             {**refs[2], "content": None}]
    client = _Client(items)
    note = artifacts_note(client, t)
    assert client.paths == ["/tasks/DEMO-0104/artifacts"]
    assert "## Approved data artifacts" in note and '"total": 41' in note and "### result#12 (output)" in note
    assert "DS-0003" not in note and "Not released (status generated)" in note
    assert artifacts_note(_Client(fail=True), t) == ""  # Older server
    assert artifacts_note(_Client(), {"task_nr": "X-1"}) == "" and task_markdown(
        {"task_nr": "X-1", "title": "t", "status": "draft", "prio": 3}).count("Data references") == 0
