"""WF 14: canonical YAML export/import of workflow templates, the `draft_workflow` MCP tool and its UI (test DB).

Covers: lossless round trip, import creates an unpublished draft only (never publishes), the same publish checks
as the editor refuse broken documents, rights (read for export, edit for import), and the CLI/MCP wiring.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import WorkflowTemplate, WorkflowTemplateVersion
from app.services.workflows import transfer
from tests.conftest import make_token
from tests.test_workflow_engine import graph  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
def api(graph):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    other.role = "orchestrator"
    s.commit()
    tokens = {"owner": make_token(owner.name), "agent": make_token(agent.name), "orch": make_token(other.name),
              "outsider": make_token("opencode")}
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client, {k: {"Authorization": "Bearer " + v} for k, v in tokens.items()}, s, template, steps


def _published_version(s, template):
    return s.scalar(select(WorkflowTemplateVersion).where(
        WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.state == "published"))


def _document(api, headers, key="content"):
    response = api[0].get(f"/api/v1/workflows/DEMO/{key}/export", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/yaml")
    return response.text


def test_export_document_shape_and_round_trip(api):
    client, h, s, template, steps = api
    text = _document(api, h["owner"])
    doc = transfer.parse_yaml(text)
    assert doc["workflow"] == "content" and doc["version"] == 1
    assert [st["key"] for st in doc["steps"]] == ["write", "approve", "inform"]
    assert doc["steps"][0]["role"] == "writer" and doc["steps"][0]["type"] == "agent"
    assert doc["steps"][1]["assignee"] == "admin" and doc["steps"][2]["config"]["to"] == ["owner"]
    # Export is stable: exporting again yields identical bytes.
    assert _document(api, h["owner"]) == text

    # Import the same document into a new template; the stored draft reproduces the document losslessly.
    imported = client.put("/api/v1/workflows/DEMO/copy/import",
                          headers={**h["orch"], "Content-Type": "application/yaml"}, content=text)
    assert imported.status_code == 200, imported.text
    assert imported.json()["state"] == "draft"
    s.commit()
    copy = s.scalar(select(WorkflowTemplate).where(WorkflowTemplate.workflow_key == "copy"))
    # The document does not carry the template key, and a fresh draft reproduces the same version number.
    original = {k: v for k, v in transfer.parse_yaml(text).items() if k not in ("workflow", "version")}
    produced = {k: v for k, v in transfer.document(copy, copy.versions[0]).items()
                if k not in ("workflow", "version")}
    assert produced == original, json.dumps({"produced": produced, "original": original}, indent=2, default=str)


def test_round_trip_keeps_zero_and_non_default_backoff(api):
    """The export→import round trip is lossless for retry_backoff_s (0 and a non-default value)."""
    from app.models import WorkflowStep

    client, h, s, template, steps = api
    version = _published_version(s, template)
    extra = WorkflowStep(version_id=version.id, position=4, step_key="run", step_type="agent",
                         title_tpl="Run", agent_role="writer", review_mode="manual",
                         max_retries=0, retry_backoff_s=0)
    extra2 = WorkflowStep(version_id=version.id, position=5, step_key="run2", step_type="agent",
                          title_tpl="Run again", agent_role="writer", review_mode="manual",
                          max_retries=3, retry_backoff_s=45)
    s.add_all([extra, extra2])
    s.commit()

    text = _document(api, h["owner"])
    doc = transfer.parse_yaml(text)
    by_key = {st["key"]: st for st in doc["steps"]}
    assert by_key["run"]["retry_backoff_s"] == 0
    assert by_key["run2"]["retry_backoff_s"] == 45 and by_key["run2"]["max_retries"] == 3
    assert by_key["run"].get("max_retries", 0) == 0  # agent default is 0, so it may stay implicit

    imported = client.put("/api/v1/workflows/DEMO/copy/import",
                          headers={**h["orch"], "Content-Type": "application/yaml"}, content=text)
    assert imported.status_code == 200, imported.text
    s.commit()
    copy = s.scalar(select(WorkflowTemplate).where(WorkflowTemplate.workflow_key == "copy"))
    original = {k: v for k, v in doc.items() if k not in ("workflow", "version")}
    produced = {k: v for k, v in transfer.document(copy, copy.versions[0]).items()
                if k not in ("workflow", "version")}
    assert produced == original, json.dumps({"produced": produced, "original": original}, indent=2, default=str)


def test_import_creates_draft_only_never_publishes(api):
    client, h, s, template, steps = api
    text = _document(api, h["owner"])
    client.put("/api/v1/workflows/DEMO/copy/import", headers={**h["orch"], "Content-Type": "application/yaml"},
               content=text)
    s.commit()
    copy = s.scalar(select(WorkflowTemplate).where(WorkflowTemplate.workflow_key == "copy"))
    assert copy.current_version is None  # never auto-published by the import
    assert copy.versions[0].state == "draft" and copy.versions[0].published_at is None
    # Re-import replaces the same draft (one draft per template), never creates a second version.
    second = client.put("/api/v1/workflows/DEMO/copy/import", headers={**h["orch"], "Content-Type": "application/yaml"},
                        content=text)
    assert second.status_code == 200 and second.json()["version"] == 1
    s.commit()
    assert s.scalar(select(func.count()).select_from(WorkflowTemplateVersion).where(
        WorkflowTemplateVersion.template_id == copy.id)) == 1
    # A human publishes it through the normal endpoint.
    published = client.post("/api/v1/workflows/DEMO/copy/publish", headers=h["owner"])
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"


def test_import_refuses_broken_document(api):
    client, h, s, template, steps = api
    before = s.scalar(select(func.count()).select_from(WorkflowTemplate))
    bad_docs = [
        ":",  # YAML syntax error
        "workflow: x\nsteps: []",  # no steps
        "workflow: x\nsteps:\n  - key: write\n    type: agent\n    title: T",  # agent without role/assignee
        "workflow: x\nsteps:\n  - key: write\n    type: agent\n    title: T\n    role: writer\n    nope: 1",  # unknown field
        "workflow: x\nsteps:\n  - {key: write, type: agent, title: T, role: writer}\n  - {key: write, type: notify, title: U}",  # dup key
        "workflow: x\nparams:\n  - {name: 9, type: string}\nsteps:\n  - {key: write, type: agent, title: T, role: writer}",  # bad name
    ]
    for body in bad_docs:
        response = client.put("/api/v1/workflows/DEMO/bad/import", headers={**h["orch"], "Content-Type": "text/yaml"},
                              content=body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error"]["code"] == "workflow_import_invalid"
    s.commit()
    assert s.scalar(select(func.count()).select_from(WorkflowTemplate)) == before  # nothing stored


def test_import_rights_and_masking(api):
    client, h, s, template, steps = api
    # Export needs read rights; an outsider cannot read the template.
    assert client.get("/api/v1/workflows/DEMO/content/export", headers=h["outsider"]).status_code == 403
    # Import needs edit rights; an agent without the orchestrator role is refused.
    body = _document(api, h["owner"])
    assert client.put("/api/v1/workflows/DEMO/x/import", headers={**h["agent"], "Content-Type": "text/yaml"},
                      content=body).status_code == 403
    # A step of a class the caller may not read blocks the export (a YAML export cannot mask fields).
    steps[0].data_class = "local_only"
    s.commit()
    assert client.get("/api/v1/workflows/DEMO/content/export", headers=h["agent"]).status_code == 403


def test_cli_and_mcp_draft_workflow(api, tmp_path, monkeypatch):
    client, h, s, template, steps = api
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cli import tm
    from cli.mcp_server import TaskBrigadeMCP

    text = _document(api, h["owner"])
    path = tmp_path / "content.yaml"
    path.write_text(text, encoding="utf-8")

    class Fake:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if method == "GET":
                return SimpleNamespace(status_code=200, text=text)
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"workflow": "DEMO/mcp-draft", "name": "Draft", "version": 1})

    fake = Fake()
    monkeypatch.setattr(tm, "_client", lambda: fake)
    runner = CliRunner()
    result = runner.invoke(tm.app, ["workflow", "export", "DEMO/content", "--output", str(tmp_path / "out.yaml")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out.yaml").read_text(encoding="utf-8") == text
    result = runner.invoke(tm.app, ["workflow", "import", "DEMO/content", str(path)])
    assert result.exit_code == 0, result.output
    assert fake.calls[-1][0] == "PUT" and fake.calls[-1][1] == "/workflows/DEMO/content/import"

    # MCP tool forwards the document as a raw PUT body.
    mcp = TaskBrigadeMCP(fake, tmp_path)
    report = mcp.draft_workflow("DEMO/mcp-draft", text)
    assert "mcp-draft" in report
    assert fake.calls[-1][0] == "PUT" and fake.calls[-1][2]["content"] == text


def test_ui_export_and_import(graph):
    """UI: export link downloads YAML, the upload stores a draft and the editor offers the form."""
    s, owner, agent, other, template, steps = graph
    from tests.conftest import BOOTSTRAP_PW

    s.commit()
    before_current = template.current_version_id
    with TestClient(app) as web:
        page = web.get("/login").text
        csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
        assert web.post("/login", data={"username": "admin", "password": BOOTSTRAP_PW, "csrf_token": csrf},
                        follow_redirects=False).status_code == 303
        csrf = web.get("/workflows").text.split('"X-CSRF-Token": "')[1].split('"')[0]
        exported = web.get("/workflows/DEMO/content/export")
        assert exported.status_code == 200 and "content" in exported.text
        assert "attachment" in exported.headers["content-disposition"]
        editor = web.get("/workflows/DEMO/content/edit").text
        assert "YAML importieren" in editor and 'enctype="multipart/form-data"' in editor
        r = web.post("/workflows/DEMO/content/import", data={"csrf_token": csrf, "action": "save"},
                     files={"file": ("content.yaml", exported.text, "application/yaml")})
        assert r.status_code == 200 and "Entwurf aus YAML importiert" in r.text, r.text[-500:]
        s.commit()
        s.expire_all()
        draft = s.scalar(select(WorkflowTemplateVersion).where(
            WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.state == "draft"))
        assert draft is not None
        assert s.get(WorkflowTemplate, template.id).current_version_id == before_current  # unchanged
