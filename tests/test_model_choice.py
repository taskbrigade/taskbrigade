"""Model choice per task: tier/concrete model, agent settings, runner switch, start check, UI, CLI, MCP."""
import json
import time
from types import SimpleNamespace

import pytest

from app.errors import Unprocessable
from app.services import model_choice
from cli import runner
from cli.client import model_line, parse_tier
from cli.tm import task_markdown
from tests.test_runner import PROMPT, FakeTmux, RunnerClient, write_config
from tests.test_tm_prepare import FakeClient, origin, task  # noqa: F401 (Fixture)
from tests.test_ui_cli import post, tm_as, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import claim, env, events, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


def agent(kind="claude_code", default=None, tiers=None, allowed=None, name="claude-code"):
    return SimpleNamespace(name=name, actor_type="agent", agent_kind=kind, model_default=default,
                           model_tiers=json.dumps(tiers) if tiers else None,
                           models_allowed=json.dumps(allowed) if allowed else None)


# --- Resolution and validation (no DB) ---------------------------------------------------------

def test_resolve_order_model_tier_default_nothing():
    a = agent(default="sonnet", tiers={"simple": "haiku", "complex": "opus"}, allowed=["opus", "sonnet"])
    t = SimpleNamespace(model=None, model_tier=None)
    assert model_choice.resolve(t, a) == {"model": "sonnet", "source": "default", "applies": True}
    t.model_tier = "simple"
    assert model_choice.resolve(t, a)["model"] == "haiku"
    t.model_tier = "standard"  # No mapping for the tier → default model
    assert model_choice.resolve(t, a)["source"] == "default"
    t.model = "opus"
    assert model_choice.resolve(t, a) == {"model": "opus", "source": "model", "applies": True}
    bare = SimpleNamespace(model=None, model_tier="complex")
    assert model_choice.resolve(bare, agent()) == {"model": None, "source": None, "applies": True}
    assert model_choice.resolve(bare, None)["model"] is None
    assert model_choice.resolve(bare, agent(kind="hermes", tiers={"complex": "x"}))["applies"] is False


def test_validation_of_names_tiers_and_allow_list():
    assert model_choice.normalize_tier(" Complex ") == "complex" and model_choice.normalize_tier("none") is None
    with pytest.raises(Unprocessable):
        model_choice.normalize_tier("huge")
    for ok in ("sonnet", "gpt-5.6-terra", "ollama/qwen3:32b", "opus[1m]", "openrouter/x@y+z"):
        assert model_choice.normalize_model(ok) == ok
    for bad in ("a b", "$(rm)", "-m", "x" * 101, "a;b"):
        with pytest.raises(Unprocessable):
            model_choice.normalize_model(bad)
    assert model_choice.parse_list("haiku, sonnet\nopus haiku") == ["haiku", "sonnet", "opus"]
    a = agent(allowed=["opus"])
    model_choice.check_allowed(a, "opus")
    model_choice.check_allowed(a, None)
    with pytest.raises(Unprocessable) as e:
        model_choice.check_allowed(a, "haiku")
    assert e.value.code == "model_not_allowed" and "opus" in e.value.message
    with pytest.raises(Unprocessable):
        model_choice.check_allowed(None, "opus")


# --- Runner: switch per agent CLI, placeholder, history note ------------------------------------

@pytest.mark.parametrize("command,expected", [
    (["claude", "--permission-mode", "auto", "{prompt}"], ["claude", "--model", "haiku", "--permission-mode"]),
    (["codex", "--sandbox", "workspace-write", "{prompt}"], ["codex", "-m", "haiku", "--sandbox"]),
    (["env", "OPENCODE_CONFIG=/x.json", "opencode", "--prompt", "{prompt}"],
     ["env", "OPENCODE_CONFIG=/x.json", "opencode", "--model", "haiku", "--prompt"]),
    (["gemini", "-p", "{prompt}"], ["gemini", "-m", "haiku", "-p"]),
])
def test_runner_adds_model_switch_per_cli(command, expected):
    cmd = runner.build_command({"command": command}, {"task_nr": "KET-0001"}, "a", model="haiku")
    assert cmd[:len(expected)] == expected
    # Without a model the command stays as it was.
    assert runner.build_command({"command": command}, {"task_nr": "KET-0001"}, "a")[:2] == command[:2]


def test_runner_model_flag_override_and_placeholder():
    cfg = {"command": ["mycli", "{prompt}"], "model_flag": "--llm="}
    assert runner.build_command(cfg, {"task_nr": "X-1"}, "a", model="m1")[:2] == ["mycli", "--llm=m1"]
    assert runner.build_command({"command": ["claude", "{prompt}"], "model_flag": ""}, {"task_nr": "X-1"}, "a",
                                model="m1")[1] != "--model"  # Empty = no switch
    cfg = {"command": ["agent", "--model", "{model}", "--x={model}", "{prompt}"]}
    assert runner.build_command(cfg, {"task_nr": "X-1"}, "a", model="m2")[:4] == ["agent", "--model", "m2", "--x=m2"]
    cmd = runner.build_command(cfg, {"task_nr": "X-1"}, "a")  # No model: argument and its switch are left out
    assert cmd[0] == "agent" and len(cmd) == 2 and "{model}" not in " ".join(cmd)
    parts, applied, note = runner.with_model(["unknown-cli", "{prompt}"], {"command": ["unknown-cli"]}, "m3")
    assert parts == ["unknown-cli", "{prompt}"] and applied is None and "model_flag" in note


def test_runner_reports_model_and_starts_with_it(tmp_path, origin, monkeypatch):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["true"], mode="foreground")
    t = {**task("KET-0050", "task/KET-0050-x", origin.as_uri()),
         "model_effective": {"model": "haiku", "source": "tier", "applies": True}}
    fc = RunnerClient([t])
    started = []
    monkeypatch.setattr(runner.subprocess, "call", lambda cmd, **kw: started.append(cmd) or 0)
    assert "KET-0050 finished" in runner.run_once(fc, root)
    assert started[0][started[0].index("--") + 1:] == ["true"]
    note = [c for c in fc.calls if c[1] == "/tasks/KET-0050/model-used"]
    # `true` has no known switch: reported as not applied, the agent starts without a model.
    assert note and note[0][2]["model"] is None and "not applied" in note[0][2]["note"]
    write_config(root, ["claude", "{prompt}"], mode="foreground")
    fc = RunnerClient([{**t, "task_nr": "KET-0051", "branch_name": "task/KET-0051-x"}])
    monkeypatch.setattr(runner, "check_prerequisites", lambda cfg: None)
    runner.run_once(fc, root)
    agent_cmd = started[1][started[1].index("--") + 1:]
    assert agent_cmd[0] == "claude" and agent_cmd[agent_cmd.index("--model") + 1] == "haiku"
    note = [c for c in fc.calls if c[1] == "/tasks/KET-0051/model-used"][0][2]
    assert note == {"model": "haiku", "note": "--model haiku (tier)"}


def test_runner_without_model_reports_default(tmp_path, origin, monkeypatch):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["true"], mode="foreground")
    fc = RunnerClient([task("KET-0052", "task/KET-0052-x", origin.as_uri())])  # Older server: no model_effective
    monkeypatch.setattr(runner.subprocess, "call", lambda cmd, **kw: 0)
    runner.run_once(fc, root)
    assert [c[2] for c in fc.calls if c[1].endswith("/model-used")] == [{"model": None, "note": None}]


# --- Start check: unavailable model ----------------------------------------------------------------

@pytest.mark.parametrize("screen,agent_name", [
    ("⎿ There's an issue with the selected model (claude-x). It may not exist or you may not have access to it.\n> ",
     "claude"),
    ("■ The 'gpt-9' model is not supported when using Codex with a ChatGPT account.\n› ", "codex"),
    ("ProviderModelNotFoundError: Model not found: local/qwen\n", "opencode"),
    ("Error: model not available on your plan\n> ", "claude"),
])
def test_classify_screen_detects_unavailable_model(screen, agent_name):
    assert runner.classify_screen(screen, agent_name, PROMPT)[0] == "model"


def test_session_model_error_aborts_without_blocking(tmp_path, origin, capsys, monkeypatch):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["true"], mode="tmux", start_check_seconds=0.2)
    fc = RunnerClient([task("KET-0053", "task/KET-0053-x", origin.as_uri())])
    from cli.prepare import prepare
    prepare(fc, root)
    monkeypatch.setattr(runner, "_tmux", FakeTmux(
        pane="⎿ There's an issue with the selected model (opus-x). It may not exist or you may not have access.\n> "))
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexec sleep 600\n")
    fake.chmod(0o755)
    started = time.monotonic()
    rc = runner.session(fc, "KET-0053", root, [str(fake), "--model", "opus-x", PROMPT], poll_s=0.1, check_s=0.1,
                        pane="%5")
    assert time.monotonic() - started < 30 and rc != 0
    aborts = [c for c in fc.calls if c[1] == "/tasks/KET-0053/abort"]
    assert len(aborts) == 1 and "the model opus-x is not available" in aborts[0][2]["reason"]
    assert not [c for c in fc.calls if c[1].endswith(("/release", "/submit"))]
    assert runner.read_block(root) is None  # Task-specific: the agent keeps taking other tasks.
    assert "handed back to a human" in capsys.readouterr().out


def test_session_agent_quits_on_model_error_before_start_check(tmp_path, origin, monkeypatch):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["true"], mode="tmux", start_check_seconds=60)
    fc = RunnerClient([task("KET-0054", "task/KET-0054-x", origin.as_uri())])
    from cli.prepare import prepare
    prepare(fc, root)
    monkeypatch.setattr(runner, "_tmux", FakeTmux(pane="Error: model_not_found: gpt-x\n$ "))
    runner.session(fc, "KET-0054", root, ["false", "-m", "gpt-x"], poll_s=0.1, check_s=0.1, pane="%5")
    assert [c[1] for c in fc.calls if c[1].endswith(("/abort", "/release"))] == ["/tasks/KET-0054/abort"]


# --- CLI helpers and TASK.md ---------------------------------------------------------------------

def test_parse_tier_and_task_md_line():
    assert [parse_tier(v) for v in ("einfach", "Standard", "komplex", "none", "")] == [
        "simple", "standard", "complex", None, None]
    with pytest.raises(ValueError):
        parse_tier("riesig")
    base = {"task_nr": "KET-0001", "title": "T", "status": "defined", "prio": 3}
    assert model_line(base) is None
    md = task_markdown({**base, "model_tier": "simple",
                        "model_effective": {"model": "haiku", "source": "tier", "applies": True}})
    assert "- Model: `haiku` (tier simple)" in md
    md = task_markdown({**base, "model_tier": "complex", "model": "opus",
                        "model_effective": {"model": "opus", "source": "model", "applies": False}})
    assert "(tier complex, chosen explicitly) – ignored by this agent (it chooses its model itself)" in md


# --- Server, UI, CLI, MCP (DB) -------------------------------------------------------------------

def configure(env, name="claude-code", **body):
    body = {"model_default": "sonnet", "model_tiers": {"simple": "haiku", "complex": "opus"},
            "models_allowed": ["opus", "sonnet"], **body}
    r = env["c"].patch(f"/api/v1/admin/actors/{name}", headers=env["admin"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.db
def test_agent_settings_api_and_task_validation(env):  # noqa: F811
    mkproject(env)
    out = configure(env)
    assert out["model_default"] == "sonnet" and out["model_tiers"] == {"simple": "haiku", "complex": "opus"}
    assert out["models_allowed"] == ["opus", "sonnet"]
    me = env["c"].get("/api/v1/agents/me", headers=env["claude"]).json()
    assert me["model_tiers"]["simple"] == "haiku"
    plain = mktask(env, assigned_to="claude-code")  # Existing behaviour: no tier, no model
    assert plain["model_tier"] is None and plain["model_effective"]["model"] == "sonnet"
    t = mktask(env, assigned_to="claude-code", model_tier="simple")
    assert t["model_effective"] == {"model": "haiku", "source": "tier", "applies": True}
    t = mktask(env, assigned_to="claude-code", model_tier="simple", model="opus")
    assert t["model"] == "opus" and t["model_effective"]["source"] == "model"
    r = env["c"].post("/api/v1/tasks", headers=env["admin"], json={
        "project": "KET", "title": "x", "status": "draft", "assigned_to": "claude-code", "model": "haiku"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_not_allowed"
    r = env["c"].post("/api/v1/tasks", headers=env["admin"], json={
        "project": "KET", "title": "x", "status": "draft", "model": "opus"})  # No assignee
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_not_allowed"
    r = env["c"].patch(f"/api/v1/tasks/{plain['task_nr']}", headers=env["admin"], json={"model": "haiku"})
    assert r.status_code == 422 and "nicht freigegeben" in r.json()["error"]["message"]
    r = env["c"].patch(f"/api/v1/tasks/{plain['task_nr']}", headers=env["admin"],
                       json={"model_tier": "complex", "model": "opus"})
    assert r.status_code == 200 and (r.json()["model_tier"], r.json()["model"]) == ("complex", "opus")
    assert ("model_tier", None, "complex") in events(env, plain["task_nr"])
    r = env["c"].patch(f"/api/v1/tasks/{plain['task_nr']}", headers=env["admin"], json={"model": None})
    assert r.json()["model"] is None and r.json()["model_effective"]["model"] == "opus"  # Tier stays


@pytest.mark.db
def test_reassignment_keeps_tier_and_drops_disallowed_model(env):  # noqa: F811
    mkproject(env)
    configure(env)
    configure(env, "codex", model_default=None, model_tiers={"simple": "gpt-5.6-luna"},
              models_allowed=["gpt-5.6-sol", "sonnet"])
    nr = mktask(env, assigned_to="claude-code", model_tier="simple", model="opus")["task_nr"]
    r = env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "codex"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["model_tier"] == "simple" and t["model"] is None
    assert t["model_effective"] == {"model": "gpt-5.6-luna", "source": "tier", "applies": True}
    dropped = [e for e in t["events"] if e["type"] == "model_dropped"]
    assert dropped and dropped[0]["from"] == "opus" and "codex" in dropped[0]["note"]
    # Reassigning together with a model the new agent does not allow is refused – nothing changes.
    r = env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "claude-code",
                                                                          "model": "gpt-5.6-sol"})
    assert r.status_code == 422
    assert env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["assigned_to"] == "codex"
    # A model the new agent allows survives the reassignment.
    env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"model": "sonnet"})
    t = env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "claude-code"}).json()
    assert t["model"] == "sonnet" and not [e for e in t["events"] if e["type"] == "model_dropped"][1:]


@pytest.mark.db
def test_model_used_event_and_ignoring_agents(env):  # noqa: F811
    mkproject(env)
    configure(env)
    nr = mktask(env, assigned_to="claude-code", model_tier="complex")["task_nr"]
    claim(env)
    r = env["c"].post(f"/api/v1/tasks/{nr}/model-used", headers=env["claude"],
                      json={"model": "opus", "note": "--model opus (tier)"})
    assert r.status_code == 200, r.text
    assert ("model_used", None, "opus") in events(env, nr)
    assert env["c"].post(f"/api/v1/tasks/{nr}/model-used", headers=env["codex"],
                         json={"model": "x"}).status_code == 403
    # Hermes (framework): tier is shown but not applied.
    t = mktask(env, assigned_to="hermes", model_tier="simple")
    assert t["model_effective"]["applies"] is False


@pytest.mark.db
def test_ui_admin_models_task_form_and_board(env, ui):  # noqa: F811
    post(ui, "/profile/display", show_all_agents="1")  # Seeded agents have no owner; an admin sees them only so.
    mkproject(env)
    page = ui.get("/admin/users/claude-code").text
    assert 'name="model_default"' in page and 'name="model_simple"' in page and 'name="models_allowed"' in page
    r = post(ui, "/admin/users/claude-code", display_name="claude-code", role="worker", runtime="cloud",
             agent_kind="claude_code", lease_minutes="120", max_parallel_tasks="1", model_default="sonnet",
             model_simple="haiku", model_standard="", model_complex="opus", models_allowed="opus, sonnet")
    assert r.status_code == 200 and "Gespeichert" in r.text
    me = env["c"].get("/api/v1/admin/actors/claude-code", headers=env["admin"]).json()
    assert me["model_tiers"] == {"simple": "haiku", "complex": "opus"} and me["models_allowed"] == ["opus", "sonnet"]
    assert "ignoriert" in ui.get("/admin/users/hermes").text  # Hint for agents that choose their model
    form = ui.get("/tasks/new").text
    assert 'name="model_tier"' in form and "data-model-select" in form and "&#34;opus&#34;" in form
    r = post(ui, "/tasks/new", project="KET", title="Mit Stufe", description="d", acceptance_criteria="a",
             assigned_to="claude-code", status="defined", model_tier="simple", model="none")
    assert r.status_code == 200, r.text
    nr = r.url.path.rsplit("/", 1)[-1]
    assert "<code>haiku</code>" in r.text and 'class="tier tier-simple"' in r.text
    r = post(ui, "/tasks/new", project="KET", title="Falsch", assigned_to="claude-code", model="haiku")
    assert r.status_code == 422 and "nicht freigegeben" in r.text
    assert 'class="tier tier-simple"' in ui.get("/kanban").text
    assert 'class="tier tier-simple"' in ui.get("/backlog").text
    version = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["row_version"]
    r = post(ui, f"/tasks/{nr}/edit", row_version=str(version), title="Mit Stufe", prio="3",
             assigned_to="claude-code", model_tier="complex", model="opus")
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert (t["model_tier"], t["model"]) == ("complex", "opus")
    # Without model fields (older form) nothing changes; `none` clears.
    post(ui, f"/tasks/{nr}/edit", row_version=str(t["row_version"]), title="Mit Stufe", prio="3",
         assigned_to="claude-code")
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert (t["model_tier"], t["model"]) == ("complex", "opus")
    post(ui, f"/tasks/{nr}/edit", row_version=str(t["row_version"]), title="Mit Stufe", prio="3",
         assigned_to="claude-code", model_tier="none", model="none")
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert (t["model_tier"], t["model"]) == (None, None)


@pytest.mark.db
def test_sprint_editor_saves_tier_and_model(env, ui):  # noqa: F811
    mkproject(env)
    configure(env)
    entries = [{"title": "A", "assigned_to": "claude-code", "prio": 2, "model_tier": "complex", "model": "opus"},
               {"title": "B", "prio": 3}]
    r = post(ui, "/sprints/new", project="KET", name="S1", entries=json.dumps(entries))
    assert r.status_code == 200, r.text
    assert 'data-field="model_tier"' in r.text and "&#34;model_tier&#34;: &#34;complex&#34;" in r.text
    items = env["c"].get("/api/v1/tasks?project=KET", headers=env["admin"]).json()["items"]
    got = {t["title"]: (t["model_tier"], t["model"]) for t in items}
    assert got == {"A": ("complex", "opus"), "B": (None, None)}


@pytest.mark.db
def test_tbr_create_update_and_mcp(env, tm_as, monkeypatch):  # noqa: F811
    mkproject(env)
    configure(env)
    r = tm_as(env["admin"], "create", "--project", "KET", "--title", "Klein", "--assign", "claude-code",
              "--description", "d", "--acceptance", "a", "--tier", "einfach")
    assert r.exit_code == 0, r.output
    nr = r.stdout.strip()
    r = tm_as(env["admin"], "update", nr, "--model", "opus")
    assert r.exit_code == 0 and "model=opus" in r.stdout
    r = tm_as(env["admin"], "update", nr, "--model", "haiku")
    assert r.exit_code != 0 and "nicht freigegeben" in r.output
    assert tm_as(env["admin"], "update", nr, "--tier", "riesig").exit_code == 2
    r = tm_as(env["admin"], "show", nr)
    assert "- Model: `opus` (tier simple, chosen explicitly)" in r.stdout
    import cli.client as client_mod
    from cli.mcp_server import TaskBrigadeMCP
    mcp = TaskBrigadeMCP(client_mod.Client(client_mod.Config("http://testserver",
                                                             env["admin"]["Authorization"].split()[1])), None)
    out = mcp.create_task("KET", "Per MCP", "d", "a", assigned_to="claude-code", tier="simple")
    assert "created" in out and "Model: `haiku` (tier simple)" in out
    nr2 = out.split()[0]
    out = mcp.update_task(nr2, tier="none", model="sonnet")
    assert "Model: `sonnet` (chosen explicitly)" in out
    assert "Not changed" in mcp.update_task(nr2, tier="huge")
