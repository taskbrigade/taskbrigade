"""Kanban columns: questions and aborted share one column (DB-free)."""
from datetime import UTC, datetime, timedelta

from starlette.requests import Request

from app.i18n import use_language
from app.web.common import templates
from app.web.tasks_ui import DONE_RANGES, KANBAN_COLUMNS, _kanban_columns

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def card(nr: str, status: str, hours: int = 0) -> dict:
    return {"task_nr": nr, "project": "DEMO", "title": f"Task {nr}", "status": status, "prio": 3,
            "assigned_to": "builder", "updated_at": T0 + timedelta(hours=hours), "sprint": None}


def test_questions_and_aborted_share_one_column():
    keys = [k for k, _s, _d in KANBAN_COLUMNS]
    assert "questions" not in keys and "aborted" not in keys and keys.count("waiting") == 1
    assert len(keys) == 7  # One column fewer than before; still fits 1280 px.
    items = [card("Q-new", "questions", 5), card("A-new", "aborted", 4), card("Q-old", "questions", 1),
             card("A-old", "aborted", 2), card("D", "defined")]
    cols = {c["key"]: c for c in _kanban_columns(items, [])}
    # Aborted first, then questions, each longest waiting first.
    assert [t["task_nr"] for t in cols["waiting"]["cards"]] == ["A-old", "A-new", "Q-old", "Q-new"]
    assert [t["task_nr"] for t in cols["defined"]["cards"]] == ["D"]
    # Dropping into the shared column means abort – never an ambiguous "ask".
    assert cols["waiting"]["drop"] == "aborted"


def render_board(items: list[dict], lang: str) -> str:
    with use_language(lang):
        return templates.get_template("kanban.html").render(
            columns=_kanban_columns(items, []), can_drag=True, projects=[], agents=[], owners=[], owner="all",
            sprints=[], sprint_names={}, done_ranges=DONE_RANGES, done_range="3d", done_total=0, done_all=0,
            done_backlog_url="/backlog?status=done", request=Request(
                {"type": "http", "method": "GET", "path": "/kanban", "query_string": b"", "headers": [],
                 "scheme": "http", "server": ("testserver", 80), "root_path": ""}))


def test_board_shows_shared_column_and_status_text():
    html = render_board([card("DEMO-0001", "aborted"), card("DEMO-0002", "questions")], "de")
    assert "Rückfrage / Abgebrochen" in html and "(2)" in html
    assert html.count('class="kanban-col"') == 7 and "--cols: 7" in html
    assert 'data-column="waiting"' in html and 'data-status="aborted" data-label="Abgebrochen (Agent)"' in html
    # Status as text on the card, not only as colour.
    assert 'card-status st-aborted">Abgebrochen (Agent)<' in html and 'card-status st-questions">Frage<' in html
    assert "Questions / Aborted" in render_board([], "en")


def test_board_has_done_filter_in_both_languages():
    de, en = render_board([], "de"), render_board([], "en")
    assert '<select name="done"' in de and '<option value="3d" selected>Erledigt: Letzte 3 Tage</option>' in de
    for label in ("Heute", "Letzte Woche", "Letzter Monat", "Alle"):
        assert f"Erledigt: {label}<" in de
    for label in ("Today", "Last 3 days", "Last week", "Last month", "All"):
        assert f"Done: {label}<" in en
