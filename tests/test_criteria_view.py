"""Acceptance-criteria checklist parsing and safe inline Markdown for the task read view."""
from pathlib import Path

from markupsafe import Markup

from app.web.common import criteria_view, render_inline


def test_checkbox_lines_are_counted_and_plain_bullets_stay_items():
    view = criteria_view("- [x] one\n- [ ] two\n- [X] three\n- plain\n\nA note")
    assert (view["checks"], view["met"], view["empty"]) == (3, 2, False)
    assert [i["kind"] for i in view["items"]] == ["check", "check", "check", "bullet", "text"]
    assert view["items"][0]["done"] and not view["items"][1]["done"] and view["items"][2]["done"]
    assert view["items"][3]["text"] == "plain" and view["items"][4]["text"] == "A note"


def test_text_without_checkboxes_is_left_for_markdown():
    view = criteria_view("- just a bullet\n1. and a number")
    assert view["checks"] == 0 and not view["empty"]
    assert criteria_view("")["empty"] and criteria_view(None)["empty"]
    assert criteria_view("   \n")["empty"]


def test_inline_markdown_escapes_html_and_drops_javascript_links():
    html = render_inline("**fett** <script>alert(1)</script> [x](javascript:alert(1))")
    assert isinstance(html, Markup)
    assert "<strong>fett</strong>" in html
    text = str(html)
    assert "<script>" not in text and "alert(1)" in text and 'href="javascript:' not in text
    assert not str(html).startswith("<p>")


def test_task_page_phone_css_scrolls_tabs_not_the_page():
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 639px)", 1)[1].split("@media", 1)[0]
    assert "overflow-x: auto" in phone and ".props-card" in phone and "position: sticky" in phone
    assert "grid-template-columns: 1fr 1fr" in phone
