"""DATEV repeated analysis: reviewed code once, local reuse, bounded schema repairs."""
import json

import pytest
from sqlalchemy import select

from app.errors import NotFound
from app.models import DatasetProfile, RunResult, WorkflowParam, WorkflowScriptPin, WorkflowStep
from app.services import dataset_profiles, state_machine as sm
from app.services.workflows import engine as wf, scripts
from app.services.workflows.publish import blocking, check_publish, definition_from_version
from tests.test_workflow_data_steps import data_graph  # noqa: F401
from tests.test_workflow_engine import finish, graph, latest  # noqa: F401

pytestmark = pytest.mark.db
SHA = "a" * 40


@pytest.fixture
def script_graph(data_graph):  # noqa: F811
    s, owner, agent, other, template, steps, runner, ds = data_graph
    template.workflow_key, template.name = "monthly", "Monthly analysis"
    for step in reversed(steps):
        step.position += 2
        s.flush()
    profile = WorkflowStep(version_id=template.current_version_id, position=1, step_key="profile",
                           step_type="runner", data_class="sealed", assignee_id=runner.id,
                           title_tpl="Profile", description_tpl="Profile dataset", acceptance_tpl="Schema exists",
                           config_json=json.dumps({"task_kind": "profile", "run": {"dataset": "{{dataset}}"}}))
    code = WorkflowStep(version_id=template.current_version_id, position=2, step_key="code", step_type="script",
                        data_class="cloud_ok", assignee_id=agent.id, title_tpl="Write monthly script",
                        description_tpl="Write a reusable analysis", acceptance_tpl="Reviewed script",
                        config_json=json.dumps({"path": "analysis/month.py", "profile_from": "profile"}))
    s.add_all([profile, code, WorkflowParam(version_id=template.current_version_id, name="dataset",
                                           param_type="dataset", required=True, label="Dataset")])
    steps[0].config_json = json.dumps({"run": {"dataset": "{{dataset}}"}})
    steps[0].inputs_json = json.dumps([{"ref": "{{steps.code.script}}", "purpose": "script"}])
    s.flush()
    s.expire(template.current_version, ["steps", "params"])
    return s, owner, agent, other, template, [profile, code, *steps], runner, ds


def profiled(env, *, column="Kostenstelle", released=False):
    s, owner, agent, other, template, steps, runner, ds = env
    run = wf.start(s, owner, template, params={"dataset": ds.id})
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", runner)
    schema = {"sheets": [{"name": "DATEV", "columns": [{"name": column, "type": "string"}]}]}
    from app.runner.profiler import fingerprint
    p = DatasetProfile(dataset_id=ds.id, task_id=row.task_id, profiler_version="test",
                       sample_csv=column + "\n" + "synthetic\n" * 20,
                       schema_json=json.dumps(schema), schema_fingerprint=fingerprint(schema),
                       status="released" if released else "generated")
    result = RunResult(task_id=row.task_id, dataset_id=ds.id, runner_id=runner.id,
                       job_id=f"profile-{row.id}", task_kind="profile")
    s.add_all([p, result]); s.flush()
    sm.transition(s, row.task, "submit", runner, {"_run_result": result, "summary": "Profiled"})
    return run, p


def release(env, run, profile):
    s, owner, *rest = env
    profile.status = "released"
    sm.transition(s, latest(s, run, 1).task, "approve", owner, {"_dataset_profile": profile})


def merged(env, run, sha=SHA):
    s, owner, agent, *rest = env
    task = latest(s, run, 2).task
    sm.transition(s, task, "claim", agent)
    task.merge_commit_sha = sha
    finish(s, owner, task)
    return task


def execute(env, run, *, exit_code=0, checks=False):
    s, owner, agent, other, template, steps, runner, ds = env
    row = latest(s, run, 3)
    sm.transition(s, row.task, "claim", runner)
    result = RunResult(task_id=row.task_id, dataset_id=ds.id, runner_id=runner.id,
                       job_id=f"execute-{row.id}", task_kind="run", script_commit_sha=row.task.run_commit_sha,
                       status="checks_failed" if checks else "pending_approval", exit_code=exit_code,
                       checks_json='{"checks":[{"n":3,"status":"failed","secret":"never-copy"}]}')
    s.add(result); s.flush()
    if exit_code:
        sm.transition(s, row.task, "abort", runner, {"body": "Schema mismatch"})
    else:
        sm.transition(s, row.task, "submit", runner, {"_run_result": result, "summary": "Executed"})
    return result


def complete(env, run):
    s, owner, *rest = env
    result = execute(env, run)
    assert run.status == "waiting" and latest(s, run, 4).status == "waiting"
    result.status, result.released_json = "released", "{}"
    sm.transition(s, latest(s, run, 3).task, "gate_release", owner,
                  {"_gate": result, "_author": owner, "body": "Released"})
    finish(s, owner, latest(s, run, 5).task)
    assert run.status == "succeeded"


def test_datev_first_repeat_and_schema_change(script_graph):
    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    assert not blocking(check_publish(definition_from_version(template.current_version, template, template.project),
                                      data_enabled=True))
    run, profile = profiled(env)
    assert run.status == "waiting" and latest(s, run, 2).task_id is None
    release(env, run, profile)
    task = latest(s, run, 2).task
    assert profile.schema_json in task.description and "analysis/month.py" in task.description
    original = merged(env, run)
    assert latest(s, run, 3).task.run_commit_sha == SHA
    complete(env, run)
    repeat, generated = profiled(env)
    assert latest(s, repeat, 2).task_id is None and latest(s, repeat, 2).status == "succeeded"
    assert generated.status == "generated" and latest(s, repeat, 3).task.run_commit_sha == SHA
    with pytest.raises(NotFound):
        dataset_profiles.visible(s, generated.id, agent)
    complete(env, repeat)
    changed, current = profiled(env, column="KST")
    assert latest(s, changed, 2).task_id is None
    release(env, changed, current)
    repair = latest(s, changed, 2).task
    assert repair.follow_up_of_id == original.id and "analysis/month.py" in repair.title
    assert '"added": [["DATEV", "KST"]]' in repair.description
    assert '"removed": [["DATEV", "Kostenstelle"]]' in repair.description
    merged(env, changed, "b" * 40)
    pins = s.scalars(select(WorkflowScriptPin).order_by(WorkflowScriptPin.id)).all()
    assert len(pins) == 2 and pins[0].retired_at and pins[1].retired_at is None
    assert latest(s, changed, 3).task.run_commit_sha == "b" * 40
    complete(env, changed)
    assert json.loads(latest(s, run, 2).output_json)["script"]["commit"] == SHA


@pytest.mark.parametrize("checks", [False, True])
def test_exit_three_and_failed_checks_repair_once_then_pause(script_graph, checks):
    env = script_graph
    s = env[0]
    run, profile = profiled(env, released=True)
    merged(env, run)
    execute(env, run, exit_code=0 if checks else 3, checks=checks)
    repair = latest(s, run, 2)
    assert repair.attempt == 2 and repair.task_id
    assert "never-copy" not in repair.task.description
    wf.advance(s, run)
    assert latest(s, run, 2).task_id == repair.task_id
    merged(env, run, "b" * 40)
    execute(env, run, exit_code=0 if checks else 3, checks=checks)
    assert run.status == "paused" and run.pause_reason == "script_repair_limit"
    assert latest(s, run, 2).attempt == 2


def test_other_exit_uses_existing_failure_recovery(script_graph):
    run, profile = profiled(script_graph, released=True)
    merged(script_graph, run)
    execute(script_graph, run, exit_code=1)
    assert run.status == "paused" and run.pause_reason == "task_aborted"
    assert latest(script_graph[0], run, 2).attempt == 1


def test_pin_reuse_across_versions_and_path_change(script_graph):
    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    complete(env, run)
    from app.api.workflows import Draft, StepDraft, ParamDraft
    from app.services.workflows import api
    draft = Draft(name=template.name, steps=[StepDraft(**{
        **{key: getattr(st, key) for key in ("step_key", "step_type", "title_tpl", "description_tpl", "acceptance_tpl",
                                            "assignee_id", "data_class", "review_mode")},
        "config": json.loads(st.config_json) if st.config_json else None,
        "inputs": json.loads(st.inputs_json) if st.inputs_json else None}) for st in steps],
        params=[ParamDraft(name="dataset", param_type="dataset", label="Dataset")])
    api.save_draft(s, owner, template.project, template.workflow_key, draft)
    api.publish(s, owner, template)
    repeat, generated = profiled(env)
    assert latest(s, repeat, 2).task_id is None and repeat.version.version == 2
    complete(env, repeat)
    scripts.retire(s, template, "code", owner)
    rebuilt, generated = profiled(env)
    assert latest(s, rebuilt, 2).task_id is None and rebuilt.status == "waiting"
    release(env, rebuilt, generated)
    assert latest(s, rebuilt, 2).task_id


def test_schema_diff_excludes_data_values():
    class Profile:
        schema_json = '{"sheets":[{"name":"DATEV","columns":[{"name":"KST","type":"string","values":["secret"]}]}]}'
    assert "secret" not in json.dumps(scripts.schema_diff(Profile(), Profile()))


@pytest.mark.parametrize("scheduled_retry", [False, True])
def test_only_one_open_repair_per_pin(script_graph, scheduled_retry):
    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    if scheduled_retry:
        steps[1].agent_role, steps[1].assignee_id, steps[1].max_retries = "writer", None, 1
    run, profile = profiled(env, released=True)
    merged(env, run)
    complete(env, run)
    changed, current = profiled(env, column="KST", released=True)
    repair = latest(s, changed, 2).task
    repair.follow_up_of_id = None  # The loop guard must use engine metadata, not editable task links.
    if scheduled_retry:
        sm.transition(s, repair, "claim", agent)
        sm.transition(s, repair, "abort", agent, {"body": "Temporary failure"})
        assert latest(s, changed, 2).next_retry_at
    # Allow another run of the same template and the same pin to reach its mismatch.
    run2 = wf.start(s, owner, template, params={"dataset": ds.id}, force_parallel=True)
    row = latest(s, run2, 1)
    sm.transition(s, row.task, "claim", runner)
    p = DatasetProfile(dataset_id=ds.id, task_id=row.task_id, profiler_version="test", sample_csv="",
                       schema_json=current.schema_json, schema_fingerprint=current.schema_fingerprint, status="released")
    result = RunResult(task_id=row.task_id, dataset_id=ds.id, runner_id=runner.id,
                       job_id=f"parallel-{row.id}", task_kind="profile")
    s.add_all([p, result]); s.flush()
    sm.transition(s, row.task, "submit", runner, {"_run_result": result, "summary": "Profiled"})
    assert run2.status == "paused" and run2.pause_reason == "script_repair_open"
    assert latest(s, run2, 2).task_id is None
    assert repair.status == ("aborted" if scheduled_retry else "defined")


def test_manual_repair_after_other_exit_requires_human(script_graph):
    env = script_graph
    s, owner, agent, *rest = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    execute(env, run, exit_code=1)
    from app.errors import Forbidden
    with pytest.raises(Forbidden):
        scripts.repair(s, agent, run)
    scripts.repair(s, owner, run)
    assert latest(s, run, 2).attempt == 2 and latest(s, run, 2).task_id
    assert json.loads(latest(s, run, 2).output_json)["manual_repair"]


def test_release_during_manual_pause_defers_code(script_graph):
    env = script_graph
    s, owner, *rest = env
    run, profile = profiled(env)
    wf.pause(s, owner, run)
    release(env, run, profile)
    assert run.status == "paused" and latest(s, run, 2).task_id is None
    wf.resume(s, owner, run)
    assert latest(s, run, 2).task_id


def test_reused_aliases_stay_local_and_reach_runner(script_graph):
    env = script_graph
    s, owner, *rest = env
    run, profile = profiled(env, released=True)
    raw = json.loads(profile.schema_json)
    profile.aliases_json = json.dumps({"original": {"sheets": [{"name": "DATEV", "columns": [{"name": "Kostenstelle"}]}]},
                                       "aliases": {"s0c0": "cost_center"}})
    raw["sheets"][0]["columns"][0]["name"] = "cost_center"
    profile.schema_json = json.dumps(raw)
    from app.runner.profiler import fingerprint
    profile.schema_fingerprint = fingerprint(raw)
    merged(env, run)
    complete(env, run)
    repeat, generated = profiled(env)
    assert generated.status == "generated" and latest(s, repeat, 2).task_id is None
    assert latest(s, repeat, 1).task.status == "done"
    selected = scripts.runner_profile(s, latest(s, repeat, 3).task)
    assert selected.id == profile.id and selected.aliases_json == profile.aliases_json
    assert generated.schema_json != profile.schema_json


def test_path_change_requires_new_script(script_graph):
    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    complete(env, run)
    # Simulate the configuration of a new immutable version; the key alone cannot reuse the old path.
    steps[1].config_json = json.dumps({"path": "analysis/new_month.py", "profile_from": "profile"})
    changed, profile = profiled(env, released=True)
    assert latest(s, changed, 2).task_id
    assert "analysis/new_month.py" in latest(s, changed, 2).task.description


@pytest.mark.parametrize("field,value", [("review", "agent"), ("optional", True),
                                         ("config", {"path": "../bad.py", "profile_from": "profile"}),
                                         ("config", {"path": 123, "profile_from": "profile"}),
                                         ("config", {"path": "analysis/month.py", "profile_from": "missing"}),
                                         ("config", {"path": "analysis/month.py", "profile_from": "profile",
                                                     "max_repairs_per_run": -1})])
def test_invalid_script_definition_rejected(script_graph, field, value):
    s, owner, agent, other, template, steps, runner, ds = script_graph
    definition = definition_from_version(template.current_version, template, template.project)
    definition["steps"][1][field] = value
    assert any(i.code == "workflow_script_invalid" for i in check_publish(definition, data_enabled=True))


def test_real_submit_and_merge_preserve_repair_metadata(script_graph, respx_mock):
    from app.errors import Forbidden
    from app.services import settings_store
    from tests.gitea_fake import FakeGitea, GITEA, SERVICE_USER

    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    fake = FakeGitea(respx_mock)
    fake.add_repo(SERVICE_USER, "demo")
    settings_store.update(s, {"gitea_url": GITEA, "gitea_token": "test-service-token"}, owner.id)
    template.project.gitea_owner, template.project.gitea_repo = SERVICE_USER, "demo"
    run, profile = profiled(env, released=True)

    def submit():
        row = latest(s, run, 2)
        task = row.task
        sm.transition(s, task, "claim", agent)
        fake.push_branch(SERVICE_USER, "demo", task.branch_name)
        fake.put_branch_file(SERVICE_USER, "demo", task.branch_name, "analysis/month.py", "print('aggregate')")
        sm.transition(s, task, "submit", agent, {"branch_name": task.branch_name, "commit_sha": "a" * 40,
                                               "summary": "Script complete"})
        assert json.loads(row.output_json)["input_profiles"] == [profile.id]
        with pytest.raises(Forbidden):
            wf.check_decision(s, task, agent)
        sm.transition(s, task, "approve", owner)
        assert task.status == "done" and task.merge_commit_sha == "c" * 40
        return row

    original = submit()
    execute(env, run, exit_code=3)
    repair = submit()
    assert json.loads(repair.output_json)["repair_pin_id"] == json.loads(original.output_json)["pin_id"]
    execute(env, run, exit_code=3)
    assert run.status == "paused" and run.pause_reason == "script_repair_limit"


def test_actual_profile_release_wakes_repair_after_local_reuse(script_graph):
    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    template.project.data_four_eyes = False
    run, profile = profiled(env)
    dataset_profiles.decide(s, ds, owner, "release")
    assert latest(s, run, 2).task_id and profile.code_task_id is None
    merged(env, run)
    complete(env, run)
    repeat, generated = profiled(env)
    assert latest(s, repeat, 1).task.status == "done" and generated.status == "generated"
    execute(env, repeat, exit_code=3)
    assert repeat.status == "waiting" and latest(s, repeat, 2).task_id is None
    dataset_profiles.decide(s, ds, owner, "release")
    assert latest(s, repeat, 2).task_id and generated.code_task_id is None
    assert repeat.status == "running"


def test_recreate_control_in_ui_and_human_only_api(script_graph):
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.conftest import BOOTSTRAP_PW, make_token
    from tests.test_workflows_ui import _login, post

    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    complete(env, run)
    agent.role = "orchestrator"
    s.commit()
    token = make_token(agent.name)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        denied = client.post("/api/v1/workflows/DEMO/monthly/scripts/code/recreate",
                             headers={"Authorization": "Bearer " + token})
        assert denied.status_code == 409
        _login(client, "admin", BOOTSTRAP_PW)
        page = client.get("/workflows/DEMO/monthly")
        assert page.status_code == 200 and "Skript neu erstellen" in page.text
        response = post(client, "/workflows/DEMO/monthly/scripts/code/recreate", follow_redirects=False)
        assert response.status_code == 303
    s.expire_all()
    pin = s.scalar(select(WorkflowScriptPin))
    assert pin.retired_at
    assert json.loads(latest(s, run, 2).output_json)["script"]["commit"] == SHA


def test_manual_repair_control_in_ui(script_graph):
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.conftest import BOOTSTRAP_PW
    from tests.test_workflows_ui import _login, post

    env = script_graph
    s, owner, *rest = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    execute(env, run, exit_code=1)
    s.commit()
    with TestClient(app) as client:
        _login(client, "admin", BOOTSTRAP_PW)
        page = client.get(f"/workflow-runs/{run.id}")
        assert page.status_code == 200 and "Reparatur-Task erstellen" in page.text
        response = post(client, f"/workflow-runs/{run.id}/repair", follow_redirects=False)
        assert response.status_code == 303
    s.expire_all()
    assert latest(s, run, 2).attempt == 2 and latest(s, run, 2).task_id


def test_generated_profile_output_is_masked_for_agent(script_graph):
    from app.services.workflows import api

    s, owner, agent, *rest = script_graph
    run, profile = profiled(script_graph)
    data = api.run_info(s, agent, run)
    assert data["steps"][0]["outputs"] == "***" and data["steps"][1]["outputs"] == "***"
    assert str(profile.schema_fingerprint) not in json.dumps(data, default=str)


def test_zero_automatic_repairs_pauses_schema_change(script_graph):
    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    complete(env, run)
    steps[1].config_json = json.dumps({"path": "analysis/month.py", "profile_from": "profile",
                                     "max_repairs_per_run": 0})
    changed, profile = profiled(env, column="KST", released=True)
    assert changed.status == "paused" and changed.pause_reason == "script_repair_limit"
    assert latest(s, changed, 2).task_id is None


def test_preexecution_fingerprint_failure_requests_repair(script_graph):
    from app.errors import Conflict
    from app.services import runner_jobs
    from app.runner.service import FAIL_FROM_API

    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    task = latest(s, run, 3).task
    sm.transition(s, task, "claim", runner)
    profile.schema_fingerprint = "f" * 64
    with pytest.raises(Conflict) as exc:
        scripts.runner_profile(s, task)
    assert FAIL_FROM_API[exc.value.code] == "schema_mismatch"
    runner_jobs.fail(s, runner, task.task_nr, "schema_mismatch")
    assert latest(s, run, 2).attempt == 2 and latest(s, run, 2).task_id
    assert task.status == "cancelled"


@pytest.mark.parametrize("exit_code", [0, 3])
def test_runner_result_report_repairs_and_retires_failed_job(script_graph, monkeypatch, exit_code):
    from app.services import runner_jobs, output_gate

    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    run, profile = profiled(env, released=True)
    merged(env, run)
    row = latest(s, run, 3)
    old_task = row.task
    sm.transition(s, old_task, "claim", runner)

    def failed_check(session, result, probe, refused):
        result.status = "checks_failed"
        result.checks_json = '{"checks":[{"n":3,"status":"failed"}]}'

    monkeypatch.setattr(output_gate, "check", failed_check)
    job = f"wf11-report-{row.id}"
    result = runner_jobs.record_result(s, runner, old_task.task_nr, {
        "job_id": job, "exit_code": exit_code, "service_result": "success",
        "files": [{"kind": "output", "name": "result.json", "path": f"quarantine/{job}/result.json.enc",
                   "size": 1, "sha256": "a" * 64}], "dek_wrapped": "dGVzdA=="})
    assert result.exit_code == exit_code and old_task.status == "cancelled"
    assert latest(s, run, 2).attempt == 2 and latest(s, run, 2).task_id
    assert latest(s, run, 3).attempt == 2 and latest(s, run, 3).task_id is None


def test_script_agent_retry_preserves_profile_and_pin_metadata(script_graph):
    from app.models import WorkflowRoleBinding
    from app.services.workflows.errors import progress

    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    steps[1].agent_role, steps[1].assignee_id, steps[1].max_retries = "writer", None, 1
    s.add(WorkflowRoleBinding(project_id=template.project_id, role="writer", actor_id=other.id, rank=2))
    run, profile = profiled(env, released=True)
    row = latest(s, run, 2)
    sm.transition(s, row.task, "claim", agent)
    sm.transition(s, row.task, "abort", agent, {"body": "Temporary failure"})
    assert row.next_retry_at
    progress(s, run, row.next_retry_at)
    retried = latest(s, run, 2)
    assert retried.task_id == row.task_id and retried.assignee_id == other.id
    assert json.loads(retried.output_json)["input_profiles"] == [profile.id]
    sm.transition(s, retried.task, "claim", other)
    retried.task.merge_commit_sha = SHA
    finish(s, owner, retried.task)
    assert latest(s, run, 3).task.run_commit_sha == SHA


def test_profile_reuse_across_intervening_metadata_step(script_graph):
    env = script_graph
    s, owner, agent, other, template, steps, runner, ds = env
    for step in reversed(steps[1:]):
        step.position += 1
        s.flush()
    steps[1].data_class = None  # Effective cloud_ok is sufficient.
    s.add(WorkflowStep(version_id=template.current_version_id, position=2, step_key="metadata",
                       step_type="notify", title_tpl="Profile complete", config_json='{"text":"{{steps.profile.status}}"}'))
    s.flush(); s.expire(template.current_version, ["steps"])
    assert not blocking(check_publish(definition_from_version(template.current_version, template, template.project),
                                      data_enabled=True))
    run, profile = profiled(env)
    assert latest(s, run, 2).status == "succeeded" and latest(s, run, 3).task_id is None
    release(env, run, profile)
    task = latest(s, run, 3).task
    sm.transition(s, task, "claim", agent)
    task.merge_commit_sha = SHA
    finish(s, owner, task)
    assert latest(s, run, 4).task.run_commit_sha == SHA
    wf.cancel(s, owner, run)
    repeat, profile = profiled(env)
    assert latest(s, repeat, 3).status == "succeeded" and latest(s, repeat, 3).task_id is None
    assert profile.status == "generated"


def test_large_schema_diff_uses_run_api_without_overflowing_task(script_graph):
    from app.runner.profiler import fingerprint

    env = script_graph
    s = env[0]
    run, profile = profiled(env, released=True)
    merged(env, run)
    complete(env, run)
    changed, current = profiled(env, column="KST")
    schema = {"sheets": [{"name": "DATEV", "columns": [
        {"name": f"column_{i}_" + "x" * 150, "type": "string"} for i in range(500)]}]}
    current.schema_json, current.schema_fingerprint = json.dumps(schema), fingerprint(schema)
    release(env, changed, current)
    row = latest(s, changed, 2)
    assert len(row.task.description.encode()) < 60000
    assert f"/api/v1/workflow-runs/{changed.id}" in row.task.description
    assert len(json.loads(row.output_json)["schema_diff"]["added"]) == 500
    s.flush()
