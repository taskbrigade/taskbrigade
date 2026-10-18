"""`tbr` CLI: configuration and Git credential helper without network access."""

import pytest
from typer.testing import CliRunner

from cli import client as client_mod
from cli import tm

runner = CliRunner()


def test_config_from_env_overrides_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('url = "https://datei/"\ntoken = "tm_datei"\n')
    monkeypatch.delenv("TBR_URL", raising=False)
    monkeypatch.delenv("TBR_TOKEN", raising=False)
    assert client_mod.load_config(cfg) == client_mod.Config("https://datei", "tm_datei")
    monkeypatch.setenv("TBR_TOKEN", "tm_env")
    assert client_mod.load_config(cfg).token == "tm_env"


def test_missing_config(tmp_path, monkeypatch):
    monkeypatch.delenv("TBR_URL", raising=False)
    monkeypatch.delenv("TBR_TOKEN", raising=False)
    with pytest.raises(client_mod.ConfigError):
        client_mod.load_config(tmp_path / "fehlt.toml")


@pytest.mark.parametrize("request_attrs,expected", [
    ({"protocol": "http", "host": "gitea.example.internal:3001"}, True),
    ({"protocol": "https", "host": "gitea.example.internal:3001"}, False),
    ({"protocol": "https", "host": "github.com"}, False),
    ({"host": "192.168.1.10:3001"}, False),
    ({"protocol": "http", "host": "192.168.1.10"}, False),
])
def test_host_matching(request_attrs, expected):
    assert tm._host_matches(request_attrs, "http://gitea.example.internal:3001") is expected


def test_host_matching_fails_closed_without_gitea_url():
    assert not tm._host_matches({"protocol": "https", "host": "github.com"}, None)


class FakeClient:
    def __init__(self, *_):
        pass

    def get(self, path):
        assert path == "/agents/me/git-credentials"
        return {"username": "hermes-bot", "token": "geheim", "email": None, "gitea_url": "http://gitea.example.internal:3001"}


def test_git_credential_get(monkeypatch):
    monkeypatch.setattr(tm, "_client", lambda: FakeClient())
    r = runner.invoke(tm.app, ["git-credential", "get"], input="protocol=http\nhost=gitea.example.internal:3001\n\n")
    assert r.exit_code == 0 and r.stdout == "username=hermes-bot\npassword=geheim\n"
    r = runner.invoke(tm.app, ["git-credential", "get"], input="protocol=https\nhost=github.com\n\n")
    assert r.exit_code == 0 and r.stdout == ""


def test_git_credential_store_is_noop(monkeypatch):
    monkeypatch.setattr(tm, "_client", lambda: pytest.fail("store darf die API nicht aufrufen"))
    r = runner.invoke(tm.app, ["git-credential", "store"], input="username=x\npassword=y\n\n")
    assert r.exit_code == 0 and r.stdout == ""


# --- Orchestrator commands without a server -------------------------------------------

class OrchestratorFake:
    """Fake API for update/cancel/reset; `deny` simulates a worker (403)."""

    def __init__(self, deny=False, version=7):
        self.deny, self.version, self.calls = deny, version, []

    def get(self, path):
        return {"task_nr": "KET-0001", "row_version": self.version}

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if self.deny:
            raise client_mod.ApiError(403, "forbidden", "Nur Admins und Orchestratoren dürfen Tasks bearbeiten")
        if kw.get("headers", {}).get("If-Match") not in (None, str(self.version)):
            raise client_mod.ApiError(409, "version_conflict", "Task wurde zwischenzeitlich geändert")

        class R:
            status_code = 200

            @staticmethod
            def json():
                body = kw.get("json") or {}
                return {"task_nr": "KET-0001", "status": "cancelled" if body.get("to") == "cancelled" else "defined",
                        "row_version": 8, "prio": 3, **{k: v for k, v in body.items() if k not in ("to", "comment")}}
        return R()


def test_parse_due():
    assert client_mod.parse_due("2026-10-31") == "2026-10-31"
    assert client_mod.parse_due("none") is None and client_mod.parse_due("-") is None
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        client_mod.parse_due("31.10.2026")


def test_parse_usage():
    assert client_mod.parse_usage("input=100,output=200,cost_cents=12,model=gpt-4") == {
        "input_tokens": 100, "output_tokens": 200, "cost_cents": 12, "model": "gpt-4"}
    assert client_mod.parse_usage("input=5") == {"input_tokens": 5}
    assert client_mod.parse_usage("cost_cents=3,model=x") == {"cost_cents": 3, "model": "x"}
    assert client_mod.parse_usage(None) is None
    assert client_mod.parse_usage("") is None
    with pytest.raises(ValueError, match="must be an integer"):
        client_mod.parse_usage("input=abc")
    with pytest.raises(ValueError, match="invalid"):
        client_mod.parse_usage("foo=1")
    with pytest.raises(ValueError, match="at least one"):
        client_mod.parse_usage("model=gpt-4")


def test_update_sends_patch_with_if_match(monkeypatch):
    fake = OrchestratorFake()
    monkeypatch.setattr(tm, "_client", lambda: fake)
    r = runner.invoke(tm.app, ["update", "ket-0001", "--due", "2026-10-31", "--assign", "codex", "--prio", "2",
                               "--title", "T", "--description", "D", "--acceptance", "A"])
    assert r.exit_code == 0, r.output
    method, path, kw = fake.calls[-1]
    assert (method, path, kw["headers"]) == ("PATCH", "/tasks/KET-0001", {"If-Match": "7"})
    assert kw["json"] == {"due_date": "2026-10-31", "assigned_to": "codex", "prio": 2, "title": "T",
                          "description": "D", "acceptance_criteria": "A"}
    assert "due_date=2026-10-31" in r.stdout and "assigned_to=codex" in r.stdout

    r = runner.invoke(tm.app, ["update", "KET-0001", "--assign", "none", "--due", "none"])
    assert fake.calls[-1][2]["json"] == {"due_date": None, "assigned_to": None}

    r = runner.invoke(tm.app, ["update", "KET-0001", "--prio", "2", "--version", "3"])
    assert r.exit_code == 1 and "Version conflict" in r.output
    assert runner.invoke(tm.app, ["update", "KET-0001"]).exit_code == 2
    assert runner.invoke(tm.app, ["update", "KET-0001", "--due", "morgen"]).exit_code == 2


def test_parse_depends_on():
    assert client_mod.parse_depends_on(["ket-1", "KET-2, ket-1"]) == ["KET-1", "KET-2"]
    assert client_mod.parse_depends_on("KET-1,KET-2") == ["KET-1", "KET-2"]
    assert client_mod.parse_depends_on(None) == [] and client_mod.parse_depends_on(["none"]) == []
    assert client_mod.parse_depends_on("-") == []
    with pytest.raises(ValueError, match="none"):
        client_mod.parse_depends_on(["none", "KET-1"])


def test_update_depends_on_set_replace_clear_unchanged(monkeypatch):
    fake = OrchestratorFake()
    monkeypatch.setattr(tm, "_client", lambda: fake)
    r = runner.invoke(tm.app, ["update", "KET-0001", "--depends-on", "ket-0002", "--depends-on", "KET-0003"])
    assert r.exit_code == 0, r.output
    assert fake.calls[-1][2]["json"] == {"depends_on": ["KET-0002", "KET-0003"]}
    r = runner.invoke(tm.app, ["update", "KET-0001", "--depends-on", "KET-0004"])  # Replaces, does not add.
    assert fake.calls[-1][2]["json"] == {"depends_on": ["KET-0004"]}
    r = runner.invoke(tm.app, ["update", "KET-0001", "--depends-on", "none"])
    assert r.exit_code == 0 and fake.calls[-1][2]["json"] == {"depends_on": []}
    r = runner.invoke(tm.app, ["update", "KET-0001", "--prio", "2"])  # Without the option: not sent.
    assert "depends_on" not in fake.calls[-1][2]["json"]
    r = runner.invoke(tm.app, ["update", "KET-0001", "--depends-on", "none", "--depends-on", "KET-0002"])
    assert r.exit_code == 2 and "none" in r.output


class FailingFake(OrchestratorFake):
    def request(self, method, path, **kw):
        raise client_mod.ApiError(422, "unprocessable", "KET-0001 cannot depend on itself")


def test_update_depends_on_server_errors_are_readable(monkeypatch):
    monkeypatch.setattr(tm, "_client", lambda: FailingFake())
    r = runner.invoke(tm.app, ["update", "KET-0001", "--depends-on", "KET-0001"])
    assert r.exit_code == 1 and "cannot depend on itself" in r.output and "Traceback" not in r.output
    monkeypatch.setattr(tm, "_client", lambda: OrchestratorFake(deny=True))
    r = runner.invoke(tm.app, ["update", "KET-0001", "--depends-on", "KET-0002"])
    assert r.exit_code == 1 and "403" in r.output and "No permission" in r.output


def test_task_markdown_lists_dependencies_with_status():
    t = {"task_nr": "K-1", "title": "T", "status": "defined", "prio": 3, "description": "D",
         "dependencies": [{"task_nr": "K-0", "status": "in_progress", "title": "Basis"}]}
    assert "## Dependencies\n\n- K-0 (in_progress): Basis" in tm.task_markdown(t)


def test_cancel_reset_transition_and_worker_403(monkeypatch):
    fake = OrchestratorFake()
    monkeypatch.setattr(tm, "_client", lambda: fake)
    r = runner.invoke(tm.app, ["cancel", "KET-0001", "--reason", "doppelt"])
    assert r.exit_code == 0 and "cancelled" in r.stdout
    assert fake.calls[-1][:2] == ("POST", "/tasks/KET-0001/transition")
    assert fake.calls[-1][2]["json"] == {"to": "cancelled", "comment": "doppelt"}
    r = runner.invoke(tm.app, ["reset", "KET-0001"])
    assert r.exit_code == 0 and fake.calls[-1][2]["json"] == {"to": "defined"}
    assert runner.invoke(tm.app, ["cancel", "KET-0001"]).exit_code == 2  # Reason is required.

    monkeypatch.setattr(tm, "_client", lambda: OrchestratorFake(deny=True))
    for args in (["cancel", "KET-0001", "--reason", "x"], ["reset", "KET-0001"], ["update", "KET-0001", "--prio", "1"]):
        r = runner.invoke(tm.app, args)
        assert r.exit_code == 1 and "403" in r.output and "No permission" in r.output, args


def test_create_warnings():
    assert tm.create_warnings({"task_nr": "K-1", "status": "defined", "assigned_to": "x", "due_date": None}) == []
    w = tm.create_warnings({"task_nr": "K-1", "status": "draft", "assigned_to": None, "due_date": None})
    assert len(w) == 2 and "draft" in w[0] and "tbr update K-1 --assign" in w[1]
    assert not any("due" in x.lower() for x in w)  # Due date does not affect claiming.


def test_show_due_and_last_status_change():
    t = {"task_nr": "K-1", "title": "T", "status": "in_progress", "prio": 3, "due_date": "2026-10-31",
         "events": [{"type": "created", "actor": "admin", "from": None, "to": "defined",
                     "created_at": "2026-10-01T08:00:00"},
                    {"type": "assigned", "actor": "admin", "from": None, "to": "codex",
                     "created_at": "2026-10-01T08:00:00"},
                    {"type": "status_change", "actor": "codex", "from": "defined", "to": "in_progress",
                     "created_at": "2026-10-07T09:51:12"}]}
    md = tm.task_markdown(t)
    assert "- Due: 2026-10-31" in md
    assert "- Last status change: defined → in_progress (2026-10-07 09:51 UTC, codex)" in md
    assert "- Due: –" in tm.task_markdown({**t, "due_date": None, "events": []})


# --- Sprints without a server -----------------------------------------------------------

def test_parse_batch_same_format_as_ui():
    entries, errors = client_mod.parse_batch("# Kommentar\nEins\n\nZwei | codex | 1\nDrei |  | \n")
    assert errors == []
    assert entries == [{"title": "Eins", "assigned_to": None, "prio": 3},
                       {"title": "Zwei", "assigned_to": "codex", "prio": 1},
                       {"title": "Drei", "assigned_to": None, "prio": 3}]
    _entries, errors = client_mod.parse_batch(" | codex\na|b|c|d\nX | | 0\n" + "y" * 201)
    assert [e.split(":")[0] for e in errors] == ["Line 1", "Line 2", "Line 3", "Line 4"]


class SprintFake:
    def __init__(self):
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(path)
        return [{"id": 4, "name": "Sprint A"}, {"id": 7, "name": "Herbst"}]


def test_resolve_sprint_by_id_or_name():
    fake = SprintFake()
    assert client_mod.resolve_sprint(fake, None, "12") == 12 and fake.calls == []  # Ids need no lookup.
    assert client_mod.resolve_sprint(fake, "ket", "herbst") == 7 and fake.calls == ["/projects/KET/sprints"]
    with pytest.raises(ValueError, match="4 Sprint A, 7 Herbst"):
        client_mod.resolve_sprint(fake, "KET", "Winter")
    with pytest.raises(ValueError, match="give the project too"):
        client_mod.resolve_sprint(fake, None, "Herbst")
    assert client_mod.is_no_sprint("None") and client_mod.is_no_sprint("-") and not client_mod.is_no_sprint("4")


def test_task_markdown_shows_sprint_context():
    t = {"task_nr": "K-1", "title": "T", "status": "defined", "prio": 3, "sprint_name": "Sprint A",
         "sprint_status": "released", "sprint_goal": "Login fertig", "description": "D"}
    md = tm.task_markdown(t)
    assert "- Sprint: Sprint A (released)" in md and "## Sprint Sprint A" in md and "Login fertig" in md
    assert md.index("## Sprint Sprint A") < md.index("## Description")
    assert "Sprint" not in tm.task_markdown({**t, "sprint_name": None})


def test_release_and_close_reports():
    r = {"sprint": {"name": "A"}, "released": [{"task_nr": "K-1", "title": "Eins"}],
         "blocked": [{"task_nr": "K-2", "title": "Zwei", "missing": ["description"]}]}
    text = client_mod.release_report(r)
    assert "1 task(s) set to defined" in text and "+ K-1 Eins" in text and "K-2 Zwei (missing: description)" in text
    assert "Warning" not in text  # No `waiting` (older servers) or an empty list: no warning.
    r["waiting"] = [{"task_nr": "K-1", "title": "Eins", "waits_for": [
        {"task_nr": "K-7", "sprint": None}, {"task_nr": "K-8", "sprint": 3, "sprint_name": "B",
                                             "sprint_status": "planned"}]}]
    text = client_mod.release_report(r)
    assert "Warning: 1 released task(s) wait for" in text and "! K-1 Eins waits for K-7 (no sprint), K-8 (B, planned)" \
        in text
    r = {"sprint": {"name": "A"}, "moved_to": None, "moved": [], "detached": []}
    assert client_mod.close_report(r) == "Sprint 'A' closed."
    w = tm.create_warnings({"task_nr": "K-1", "status": "draft", "assigned_to": "x", "sprint_status": "planned",
                            "sprint_name": "A"})
    assert w == ["K-1 is a draft because sprint 'A' is not released yet – the sprint release publishes it "
                 "(tbr sprint release)."]


def test_sprint_commands_refuse_bad_input(monkeypatch, tmp_path):
    monkeypatch.setattr(tm, "_client", lambda: pytest.fail("no API call expected"))
    f = tmp_path / "t.txt"
    f.write_text(" | codex\n")
    r = runner.invoke(tm.app, ["sprint", "add-tasks", "4", "-p", "KET", "--file", str(f)])
    assert r.exit_code == 2 and "Line 1: title is missing" in r.output
    r = runner.invoke(tm.app, ["sprint", "add-tasks", "4", "-p", "KET", "--file", str(f) + "x"])
    assert r.exit_code == 2 and "Cannot read" in r.output
    r = runner.invoke(tm.app, ["sprint", "close", "4", "-p", "KET", "--detach", "--move-to", "5"])
    assert r.exit_code == 2 and "not both" in r.output
