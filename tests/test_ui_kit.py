"""Redesign foundation: --ui-* tokens in app.css, components in ui.css/_ui.html, self-hosted IBM Plex
and the admin-only showcase /admin/ui-kit."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.i18n import use_language
from app.web.common import templates
from tests.test_owner import _ui_login, human
from tests.test_ui_cli import ui  # noqa: F401 (Fixtures)
from tests.test_workflow import env, gitea  # noqa: F401 (Fixtures)

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
APP_CSS = (STATIC / "app.css").read_text()
UI_CSS = (STATIC / "ui.css").read_text()
TOKENS = dict(re.findall(r"(--ui-[\w-]+):\s*([^;]+);", APP_CSS))


def _hex(token: str) -> str:
    value = TOKENS[token].strip()
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", value), f"{token}: {value}"
    return value


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_components_use_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", UI_CSS, flags=re.S)
    assert not re.findall(r"#[0-9A-Fa-f]{3,8}\b|\b(?:rgba?|hsla?)\(|\b(?:white|black)\b(?!-)", css)
    used = set(re.findall(r"var\((--ui-[\w-]+)\)", css))
    assert used and not used - set(TOKENS), f"undefined tokens: {sorted(used - set(TOKENS))}"
    assert len(re.findall(r"^\s*--ui-bg:", APP_CSS, flags=re.M)) == 1  # Defined once.


# Every text/background combination the components use (ui.css) – WCAG AA for normal text.
TEXT_PAIRS = [("text", "surface"), ("text", "bg"), ("text-2", "surface"), ("text-2", "bg"), ("text-2", "surface-2"),
              ("text-2", "track"), ("text-2", "primary-tint"), ("text-2", "divider"), ("text-3", "track"),
              ("text-3", "surface"), ("text", "track"), ("primary", "surface"), ("primary", "primary-tint"),
              ("primary-hover", "primary-tint"), ("primary", "surface-2"), ("on-primary", "primary"),
              ("on-primary", "primary-hover"), ("danger", "surface"), ("neutral-fg", "neutral-bg"),
              ("tier-fg", "tier-bg"),
              *[(f"st-{s}-fg", f"st-{s}-bg") for s in ("draft", "defined", "progress", "questions", "review", "rework",
                                                       "done")],
              *[(f"zone-{z}-fg", f"zone-{z}-bg") for z in ("cloud", "local", "sealed")],
              *[("on-primary", f"prio-{n}") for n in range(1, 6)],
              *[("on-primary", f"avatar-{k}") for k in ("human", "codex", "claude_code", "opencode", "openclaw",
                                                        "hermes", "other")],
              *[(f"kind-{k}-fg", f"kind-{k}-bg") for k in ("claude_code", "openclaw", "codex", "hermes", "opencode")]]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_contrast_at_least_4_5(fg, bg):
    ratio = _contrast(_hex(f"--ui-{fg}"), _hex(f"--ui-{bg}"))
    assert ratio >= 4.5, f"--ui-{fg} on --ui-{bg}: {ratio:.2f}:1"


def test_task_colours_match_the_redesign_spec():
    assert {k: TOKENS[k] for k in ("--ui-bg", "--ui-text", "--ui-text-2", "--ui-primary", "--ui-primary-hover",
                                   "--ui-brand-orange")} == {
        "--ui-bg": "#F4F5F7", "--ui-text": "#1B2430", "--ui-text-2": "#5B6675", "--ui-primary": "#24527A",
        "--ui-primary-hover": "#163A59", "--ui-brand-orange": "#DA5E38"}


def test_ibm_plex_is_self_hosted_with_fallbacks():
    faces = re.findall(r"@font-face\s*{[^}]*}", APP_CSS)
    assert len(faces) == 8
    for face in faces:
        url = re.search(r"url\((/static/[^)]+)\)", face).group(1)
        assert (STATIC / url.removeprefix("/static/")).is_file(), url
        assert "font-display: swap" in face
    assert "googleapis" not in APP_CSS + UI_CSS
    assert TOKENS["--ui-font"].rstrip().endswith("sans-serif") and "system-ui" in TOKENS["--ui-font"]
    assert TOKENS["--ui-font-mono"].rstrip().endswith("monospace")
    assert (STATIC / "vendor" / "fonts" / "OFL.txt").read_text().startswith("Copyright")
    base = (Path(templates.env.loader.searchpath[0]) / "base.html").read_text()
    assert base.index("/static/app.css") < base.index("/static/ui.css")


def _render_macro(source: str, lang: str = "en") -> str:
    with use_language(lang):
        return templates.env.from_string('{% import "_ui.html" as ui %}' + source).render()


def test_macros_set_labels_and_aria():
    assert _render_macro('{{ ui.status("for_review") }}') == '<span class="ui-pill ui-st-for_review">Review</span>'
    assert 'class="ui-pill ui-st-released">Released<' in _render_macro('{{ ui.status("released", kind="sprint") }}')
    # Project statuses use the translated project label (never the raw value "active"/"paused"/"archived").
    assert _render_macro('{{ ui.status("active", kind="project") }}') == '<span class="ui-pill ui-st-active">Active</span>'
    assert 'class="ui-pill ui-st-paused">Pausiert<' in _render_macro('{{ ui.status("paused", kind="project") }}', "de")
    assert _render_macro('{{ ui.status("archived", kind="project") }}') == '<span class="ui-pill ui-st-archived">Archived</span>'
    assert 'aria-label="Priority 1 – highest"' in _render_macro("{{ ui.prio(1) }}")
    assert 'aria-label="Priorität 1 – höchste"' in _render_macro("{{ ui.prio(1) }}", "de")
    chip = _render_macro('{{ ui.actor("claude-code", "claude_code") }}')
    assert 'ui-avatar-claude_code" aria-hidden="true">CL<' in chip and '<span class="ui-actor-name" title="claude-code">claude-code</span>' in chip
    assert 'ui-avatar-human" aria-hidden="true">AD<' in _render_macro('{{ ui.actor("Alex Doe") }}')
    assert "ui-avatar-other" in _render_macro('{{ ui.actor("runner", "runner") }}')
    seg = _render_macro('{{ ui.segmented([("a", "A", None), ("b", "B", 2)], "b", "View") }}')
    assert 'role="group" aria-label="View" data-segmented' in seg
    assert 'type="button" value="a" aria-pressed="false"' in seg and 'value="b" aria-pressed="true"' in seg
    submit = _render_macro('{{ ui.segmented([("a", "A", None)], "a", "View", name="v") }}')
    assert 'type="submit" name="v" value="a"' in submit and "data-segmented" not in submit
    switch = _render_macro('{{ ui.switch("mail", "Mail", checked=True) }}')
    assert '<input type="checkbox" role="switch" name="mail" value="1" checked>' in switch
    assert '<input type="checkbox" name="s" value="x" checked>' in _render_macro('{{ ui.chip("s", "x", "X", True) }}')
    search = _render_macro('{{ ui.search("q", "Search tasks") }}')
    assert 'class="ui-visually-hidden">Search tasks<' in search and 'type="search"' in search
    tabs = _render_macro('{{ ui.tabs([("/a", "A"), ("/b", "B")], "/b", "Sections") }}')
    assert '<a href="/a">A</a><a href="/b" aria-current="page">B</a>' in tabs


def test_inbox_notices_header_and_backlog_titles():
    """compact notices header, and backlog titles are not the 24px task heading."""
    inbox = (STATIC.parent / "templates" / "inbox.html").read_text()
    assert 'class="ui-btn ui-btn-secondary ui-btn-xs"' in inbox
    assert '>{{ _("Mark all read") }}</button>' in inbox
    assert 'href="/profile#notifications">{{ _("Settings") }}</a>' in inbox
    assert "Notification settings" not in inbox
    de = (STATIC.parent / "locales" / "de.json").read_text()
    assert '"Mark all read": "Alle gelesen"' in de
    assert '"Settings": "Einstellungen"' in de
    assert '"Notification settings":' not in de
    head = APP_CSS[APP_CSS.index("summary.inbox-notices-head"):APP_CSS.index("#inbox-notices-msg")]
    assert "flex-wrap: wrap" in head
    assert ".inbox-notices-tools button { white-space: nowrap; }" in head
    # The 24px size is the task-page heading, not every element that shares the class name.
    assert "h1.task-title {" in APP_CSS and "font-size: 24px" in APP_CSS
    bare = [line for line in APP_CSS.splitlines() if line.startswith(".task-title")]
    assert bare == []
    backlog = (STATIC / "backlog.css").read_text()
    title = backlog[backlog.index("table.backlog .task-title > a"):backlog.index(".backlog-meta")]
    # The link takes its font from the shared `.ui-task-title` (14px / 600, ui.css).
    assert "font-size" not in title
    assert "font-size: 14px; font-weight: 500; line-height: 1.35;" in backlog  # The cell itself stays 14px.
    meta = backlog[backlog.index(".backlog-meta {"):backlog.index("table.backlog .backlog-changed")]
    assert "font-size: 12px" in meta and "color: var(--ui-text-2)" in meta
    # Kanban, planning and projects do not use this class, so their table/link sizes stay put.
    root = STATIC.parent / "templates"
    for name in ("kanban.html", "planning.html", "projects.html", "project_detail.html"):
        assert "task-title" not in (root / name).read_text()


def test_showcase_shows_every_component():
    req = SimpleNamespace(url=SimpleNamespace(path="/admin/ui-kit", query=""), cookies={})
    user = SimpleNamespace(name="admin", is_admin=True, actor_id=1)
    with use_language("en"):
        html = templates.get_template("admin/ui_kit.html").render(request=req, user=user, db_ready=True, csrf="x",
                                                                  section="ui_kit")
    for cls in ("ui-page-header", "ui-btn ui-btn-secondary", "ui-btn ui-btn-sm", "ui-btn ui-btn-xs",
                "ui-btn ui-btn-danger", "ui-pill ui-st-in_progress", "ui-pill ui-zone-sealed", "ui-prio ui-prio-5", "ui-key",
                "ui-actor", "ui-seg", "ui-chip", "ui-switch", "ui-select", "ui-search", "ui-input", "ui-card",
                "ui-table ui-table-cards", "ui-table-scroll", "ui-tabs", "ui-empty"):
        assert f'class="{cls}' in html, cls
    assert 'href="/admin/ui-kit" aria-current="page">UI kit<' in html
    assert 'data-label="Status"' in html


@pytest.mark.db
def test_ui_kit_is_admin_only(env, ui):  # noqa: F811
    r = ui.get("/admin/ui-kit")
    assert r.status_code == 200 and 'class="ui-page ui-kit"' in r.text
    assert '<link rel="stylesheet" href="/static/ui.css' in r.text
    human(env, "mia", password="mia-password-123")
    assert _ui_login("mia", "mia-password-123").get("/admin/ui-kit").status_code == 403



def test_task_titles_share_one_style_without_hyphenation():
    # Backlog, Planning and Kanban use `.ui-task-title` (14 px / 600); no automatic hyphenation.
    rule = re.search(r"\.ui-task-title, a\.ui-task-title \{([^}]*)\}", UI_CSS).group(1)
    assert "var(--ui-fs-base)" in rule and "font-weight: 600" in rule and "hyphens: manual" in rule
    tpl = STATIC.parent / "templates"
    assert 'class="card-title ui-task-title"' in (tpl / "_card.html").read_text()
    assert 'class="plan-title ui-task-title"' in (tpl / "_plan_row.html").read_text()
    assert 'class="ui-task-title"' in (tpl / "_backlog_table.html").read_text()
    for css in STATIC.glob("*.css"):
        assert "hyphens: auto" not in css.read_text(), css.name
    assert "html { hyphens: manual; }" in APP_CSS


def test_markdown_headings_stay_below_the_page_title():
    assert ".md :is(h1, h2) { font-size: 16px; }" in APP_CSS
    assert ".md :is(h3, h4, h5, h6) { font-size: 14px; }" in APP_CSS
    assert "h1.task-title { margin: .35rem 0 0; font-size: 24px;" in APP_CSS
