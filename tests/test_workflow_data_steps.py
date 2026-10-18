"""WF 10: service-level DATEV handoff, human gate lifecycle and run-time zone guards."""
import json

import pytest

from app.errors import Forbidden
from app.errors import NotFound, Unprocessable
from app.models import Actor, Dataset, DatasetProfile, ProjectMember, RunResult, WorkflowParam, WorkflowStep
from app.services import dataset_profiles, output_gate, settings_store, state_machine as sm
from app.services.workflows import engine as wf
from app.services.workflows.publish import blocking, check_publish
from tests.test_workflow_engine import finish, graph, latest  # noqa: F401

pytestmark = pytest.mark.db
SHA = "a" * 40


@pytest.fixture
def data_graph(graph):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    settings_store.update(s, {"data_enabled": "true"}, owner.id)
    owner.data_access = True
    runner = Actor(name="test-runner", display_name="Runner", actor_type="agent", agent_kind="runner",
                   runtime="local", model_location="none", role="worker", max_parallel_tasks=3, is_public=True)
    s.add(runner)
    s.flush()
    s.add(ProjectMember(project_id=template.project_id, actor_id=runner.id, gitea_permission="read"))
    ds = Dataset(dataset_key="DS-0001", project_id=template.project_id, name="Test ledger",
                 uploaded_by=owner.id, status="ready")
    s.add(ds)
    s.flush()
    steps[0].step_type, steps[0].data_class = "runner", "sealed"
    steps[0].agent_role, steps[0].assignee_id = None, runner.id
    steps[0].config_json = json.dumps({"run": {"dataset": ds.dataset_key, "commit_sha": SHA,
                                              "script_path": "analysis/month.py"}})
    steps[1].step_type, steps[1].assignee_id = "gate", None
    steps[2].step_type, steps[2].assignee_id = "agent", agent.id
    steps[2].description_tpl, steps[2].acceptance_tpl = "Interpret {{steps.write.result}}", "Report exists"
    steps[2].config_json = None
    s.flush()
    return s, owner, agent, other, template, steps, runner, ds


def submit_result(s, run, runner, *, status="pending_approval"):
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", runner)
    result = RunResult(task_id=row.task_id, dataset_id=row.task.run_dataset_id, runner_id=runner.id,
                       job_id=f"wf10-{row.id}", status=status, exit_code=0)
    s.add(result)
    s.flush()
    sm.transition(s, row.task, "submit", runner, {"_run_result": result, "summary": "Job complete"})
    return result


def test_runner_gate_waits_for_real_human_release(data_graph, monkeypatch):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    run = wf.start(s, owner, template)
    first = latest(s, run, 1)
    assert first.task.task_kind == "run" and first.task.data_class == "sealed"
    assert first.task.run_commit_sha == SHA and first.task.assigned_to == runner.id
    result = submit_result(s, run, runner)
    gate = latest(s, run, 2)
    assert first.status == "succeeded" and first.task.status == "for_review"
    assert run.status == "waiting" and gate.status == "waiting" and gate.task_id is None
    assert latest(s, run, 3).task_id is None
    wf.advance(s, run)
    assert result.status == "pending_approval" and run.status == "waiting"
    with pytest.raises(Forbidden):
        output_gate.decide(s, result.id, owner, "release")  # Creator cannot release (four-eyes).
    reviewer = Actor(name="data-reviewer", display_name="Data reviewer", actor_type="human", role="admin",
                     runtime="local", model_location="local", data_access=True)
    s.add(reviewer)
    s.flush()
    monkeypatch.setattr(output_gate, "_result_json", lambda r: b'{"format":"tbr-aggregate/1",'
                        b'"tables":[{"name":"totals","columns":["n"],"count_column":"n","rows":[[12]]}]}')
    output_gate.decide(s, result.id, reviewer, "release")
    assert result.status == "released" and gate.status == "succeeded"
    report = latest(s, run, 3).task
    assert report.data_class == "cloud_ok" and str(result.id) in report.description
    finish(s, owner, report)
    assert run.status == "succeeded"


@pytest.mark.parametrize("decision", ["reject", "keep_sealed"])
def test_gate_rejection_or_keep_never_releases_to_cloud(data_graph, decision):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    run = wf.start(s, owner, template)
    result = submit_result(s, run, runner)
    output_gate.decide(s, result.id, owner, decision, "Keep private")
    assert run.status == "paused" and run.pause_reason == "gate_rejected"
    assert latest(s, run, 3).task_id is None and result.released_json is None


def test_no_gate_keeps_sealed_and_allows_reference_only_notification(data_graph):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    steps[1].step_type, steps[1].config_json = "notify", '{"text":"{{steps.write.task_nr}}"}'
    steps[2].step_type, steps[2].description_tpl = "notify", "Finished"
    run = wf.start(s, owner, template)
    result = submit_result(s, run, runner)
    assert run.status == "succeeded" and result.status == "kept_sealed"
    assert latest(s, run, 1).task.status == "done" and result.released_json is None


@pytest.mark.parametrize("field", ["description_tpl", "config_json", "inputs_json"])
def test_runtime_missing_gate_in_every_read_location(data_graph, field):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    steps[1].step_type = "notify"
    steps[2].description_tpl = "Report"
    value = "{{steps.write.result}}"
    setattr(steps[2], field, {"config_json": json.dumps({"text": value}),
                             "inputs_json": json.dumps([{"ref": value, "purpose": "input"}])}.get(field, value))
    run = wf.start(s, owner, template)
    result = submit_result(s, run, runner)
    assert result.status == "kept_sealed" and run.status == "paused"
    assert latest(s, run, 3).task_id is None


def test_raw_summary_cannot_cross_even_after_gate(data_graph):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    steps[2].description_tpl = '{{steps.write.summary | default("fallback")}}'
    run = wf.start(s, owner, template)
    result = submit_result(s, run, runner)
    # A release does not reclassify the runner's arbitrary text/logs.
    result.status, result.released_json = "released", "{}"
    sm.transition(s, latest(s, run, 1).task, "gate_release", owner,
                  {"_gate": result, "_author": owner, "body": "Test release"})
    assert run.pause_reason == "zone_boundary_without_gate" and latest(s, run, 3).task_id is None


def test_inherited_producer_class_survives_project_change(graph):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    template.project.privacy = "local_only"
    steps[0].assignee_id, steps[0].agent_role = other.id, None
    steps[1].step_type = "notify"
    steps[1].description_tpl = '{{steps.write.summary | default("fallback")}}'
    run = wf.start(s, owner, template)
    wf.pause(s, owner, run)
    finish(s, owner, latest(s, run, 1).task)
    template.project.privacy = "cloud_ok"
    wf.resume(s, owner, run)
    assert run.pause_reason == "zone_boundary_without_gate" and latest(s, run, 2).task_id is None


@pytest.mark.parametrize("review,optional", [("auto", False), ("manual", True)])
def test_auto_gate_and_optional_gate_fail_closed_at_runtime(data_graph, review, optional):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    steps[1].review_mode, steps[1].optional = review, optional
    run = wf.start(s, owner, template)
    result = submit_result(s, run, runner)
    assert run.pause_reason == "workflow_gate_invalid" and result.status == "pending_approval"
    assert latest(s, run, 3).task_id is None


def test_human_release_during_pause_defers_successor(data_graph):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    run = wf.start(s, owner, template)
    result = submit_result(s, run, runner)
    wf.pause(s, owner, run)
    result.status, result.released_json = "released", "{}"
    sm.transition(s, latest(s, run, 1).task, "gate_release", owner,
                  {"_gate": result, "_author": owner, "body": "Test release"})
    assert run.status == "paused" and latest(s, run, 2).status == "succeeded"
    assert latest(s, run, 3).task_id is None
    wf.resume(s, owner, run)
    assert latest(s, run, 3).task_id


def test_failed_checks_pause_without_gate_activation(data_graph):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    run = wf.start(s, owner, template)
    submit_result(s, run, runner, status="checks_failed")
    assert run.pause_reason == "gate_checks_failed" and latest(s, run, 2).status == "pending"


def test_datev_profile_code_runner_gate_report_service(data_graph):
    """§11.1 without WF 11's pins: a reviewed agent supplies the merged script commit."""
    s, owner, agent, other, template, steps, runner, ds = data_graph
    for step in reversed(steps):
        step.position += 2
        s.flush()
    profile_step = WorkflowStep(version_id=template.current_version_id, position=1, step_key="profile",
                               step_type="runner", data_class="sealed", assignee_id=runner.id,
                               title_tpl="Profile", description_tpl="Profile dataset", acceptance_tpl="Schema exists",
                               config_json=json.dumps({"task_kind": "profile", "run": {"dataset": "{{datensatz}}"}}))
    code_step = WorkflowStep(version_id=template.current_version_id, position=2, step_key="code", step_type="agent",
                            data_class="cloud_ok", assignee_id=agent.id, title_tpl="Write monthly script",
                            description_tpl="Use released schema {{steps.profile.profile}}", acceptance_tpl="Reviewed")
    s.add_all([profile_step, code_step])
    s.add(WorkflowParam(version_id=template.current_version_id, name="datensatz", param_type="dataset",
                        required=True, label="Dataset"))
    steps[0].config_json = json.dumps({"run": {"dataset": "{{datensatz}}",
                                              "commit_sha": "{{steps.code.merge_commit}}",
                                              "script_path": "analysis/month.py"}})
    s.flush()
    s.expire(template.current_version, ["steps", "params"])
    run = wf.start(s, owner, template, params={"datensatz": ds.id})
    task = latest(s, run, 1).task
    assert task.task_kind == "profile"
    profile = DatasetProfile(dataset_id=ds.id, task_id=task.id, profiler_version="test",
                             schema_fingerprint="b" * 64, schema_json='{"sheets":[]}', sample_csv="")
    s.add(profile)
    s.flush()
    result = RunResult(task_id=task.id, dataset_id=ds.id, runner_id=runner.id,
                       job_id="datev-profile", task_kind="profile")
    s.add(result)
    s.flush()
    sm.transition(s, task, "claim", runner)
    sm.transition(s, task, "submit", runner, {"_run_result": result, "summary": "Profile generated"})
    assert run.status == "waiting" and latest(s, run, 2).task_id is None
    profile.status = "released"
    sm.transition(s, task, "approve", owner, {"_dataset_profile": profile})
    code = latest(s, run, 2).task
    assert str(profile.id) in code.description
    assert profile.schema_json in code.description
    sm.transition(s, code, "claim", agent)
    assert dataset_profiles.visible(s, profile.id, agent).id == profile.id
    with pytest.raises(NotFound):
        dataset_profiles.visible(s, profile.id, other)
    code.merge_commit_sha = SHA
    finish(s, owner, code)
    execution = latest(s, run, 3)
    assert execution.task.run_commit_sha == SHA and execution.task.run_dataset_id == ds.id
    sm.transition(s, execution.task, "claim", runner)
    result = RunResult(task_id=execution.task_id, dataset_id=ds.id, runner_id=runner.id,
                       job_id="datev-run", status="pending_approval")
    s.add(result)
    s.flush()
    sm.transition(s, execution.task, "submit", runner, {"_run_result": result, "summary": "Aggregated"})
    assert run.status == "waiting" and latest(s, run, 4).status == "waiting"
    result.status, result.released_json = "released", "{}"
    sm.transition(s, execution.task, "gate_release", owner,
                  {"_gate": result, "_author": owner, "body": "Released"})
    assert latest(s, run, 5).task.data_class == "cloud_ok"
    finish(s, owner, latest(s, run, 5).task)
    assert run.status == "succeeded"


def test_publish_zone_reads_and_manual_gates():
    definition = {"steps": [
        {"key": "run", "type": "runner", "role": "runner", "data_class": "sealed", "title": "Run"},
        {"key": "report", "type": "agent", "role": "writer", "data_class": "cloud_ok",
         "title": "Interpret {{steps.run.result}}"}]}
    assert any(i.code == "zone_boundary_without_gate" for i in check_publish(definition, data_enabled=True))
    definition["steps"].insert(1, {"key": "gate", "type": "gate", "review": "manual", "title": "Release"})
    assert not blocking(check_publish(definition, data_enabled=True))
    definition["steps"][1]["review"] = "auto"
    assert any(i.code == "workflow_gate_invalid" for i in check_publish(definition, data_enabled=True))


def test_foreign_or_deleted_dataset_parameter_is_refused(data_graph):
    s, owner, agent, other, template, steps, runner, ds = data_graph
    s.add(WorkflowParam(version_id=template.current_version_id, name="dataset", param_type="dataset",
                        required=True, label="Dataset"))
    s.flush()
    s.expire(template.current_version, ["params"])
    with pytest.raises(Unprocessable):
        wf.start(s, owner, template, params={"dataset": ds.id + 90000})
    ds.status = "deleted"
    with pytest.raises(Unprocessable):
        wf.start(s, owner, template, params={"dataset": ds.id}, force_parallel=True)
