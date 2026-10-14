"""Admin → New agent: project checkboxes must not trip Pico's `input + small` rule (overlapping rows)."""
import re
from types import SimpleNamespace

from starlette.requests import Request

from app.i18n import use_language
from app.web.common import templates


def render_page() -> str:
    projects = [SimpleNamespace(project_key="ALPHA", name="Alpha", privacy="cloud_ok"),
                SimpleNamespace(project_key="BETA", name="Beta", privacy="local_only")]
    with use_language("en"):
        return templates.get_template("admin/agent_new.html").render(
            form={"projects": ["BETA"]}, kinds=["claude_code"], roles=["worker"], projects=projects,
            gitea_auto=False, csrf="x", request=Request(
                {"type": "http", "method": "GET", "path": "/admin/agents/new", "query_string": b"", "headers": [],
                 "scheme": "http", "server": ("testserver", 80), "root_path": ""}))


def test_project_checkbox_label_text_is_not_a_sibling_small():
    html = render_page()
    labels = re.findall(r'<label><input type="checkbox" name="projects".*?</label>', html, re.S)
    assert len(labels) == 2
    for label in labels:
        # A <small> directly after the input (text nodes do not break `+`) becomes a block with negative margin.
        assert re.search(r"<input[^>]*>\s*<span>[^<]*<small", label), label
    assert 'value="BETA" checked' in html and "local only" in labels[1]
