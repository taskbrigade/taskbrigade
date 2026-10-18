"""Kanban: period filter for the done column (today / 3 / 7 / 30 days / all)."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update
from starlette.requests import Request

from app.db import db
from app.models import Task
from app.services import tasks as svc
from app.web import tasks_ui
from app.web.tasks_ui import DONE_RANGES, _done_range_choice, done_since

# 2026-10-10 10:00 UTC = 12:00 in Europe/Berlin (CEST); local midnight = 2026-10-09 22:00 UTC.
NOW = datetime(2026, 10, 10, 10, 0)
MIDNIGHT = datetime(2026, 10, 9, 22, 0)


def _request(query: str = "", cookie: str | None = None) -> Request:
    headers = [(b"cookie", f"tm_done_range={cookie}".encode())] if cookie is not None else []
    return Request({"type": "http", "method": "GET", "path": "/kanban", "query_string": query.encode(),
                    "headers": headers})


def test_done_since_boundaries(monkeypatch):
    monkeypatch.setattr(tasks_ui.get_settings(), "tz_display", "Europe/Berlin")
    assert done_since("today", NOW) == MIDNIGHT  # Local midnight, not UTC midnight.
    assert done_since("3d", NOW) == NOW - timedelta(days=3)
    assert done_since("7d", NOW) == NOW - timedelta(days=7)
    assert done_since("30d", NOW) == NOW - timedelta(days=30)
    assert done_since("all", NOW) is None
    # Shortly after local midnight the UTC date is still the previous day.
    assert done_since("today", datetime(2026, 10, 9, 22, 30)) == MIDNIGHT
    assert done_since("today", datetime(2026, 10, 9, 21, 59)) == datetime(2026, 10, 8, 22, 0)
    # Winter time: local midnight is 23:00 UTC.
    assert done_since("today", datetime(2026, 12, 1, 12, 0)) == datetime(2026, 11, 30, 23, 0)


def test_done_range_choice_parameter_cookie_default():
    assert list(DONE_RANGES) == ["today", "3d", "7d", "30d", "all"]
    assert _done_range_choice(_request()) == ("3d", False)  # Default: last 3 days, no cookie written.
    assert _done_range_choice(_request(cookie="7d")) == ("7d", False)  # Remembered choice.
    assert _done_range_choice(_request("done=today", cookie="7d")) == ("today", True)  # Parameter wins, saved.
    assert _done_range_choice(_request("done=bogus")) == ("3d", True)
    assert _done_range_choice(_request(cookie="bogus")) == ("3d", True)  # Unknown cookie value is replaced.


# --- UI with test DB ------------------------------------------------------------------------

from tests.test_owner import _show_all, _two_owners, _ui_login  # noqa: E402
from tests.test_workflow import env, gitea, mktask  # noqa: E402,F401 (Fixtures)


def _done(nr: str, done_at: datetime | None, updated_at: datetime) -> None:
    with db.session() as s:
        s.execute(update(Task).where(Task.task_nr == nr).values(status="done", done_at=done_at,
                                                                updated_at=updated_at))
        s.commit()


def _done_column(html: str) -> str:
    return html.split('class="kanban-col" data-column="done"', 1)[1].split("</section>", 1)[0]  # Not the phone tab.


@pytest.mark.db
def test_ui_done_filter_ranges_cookie_and_other_filters(env, monkeypatch):
    monkeypatch.setattr(tasks_ui.get_settings(), "tz_display", "Europe/Berlin")
    monkeypatch.setattr(svc, "utcnow", lambda: NOW)
    _two_owners(env)  # anna owns ZWEI, admin owns KET; anna's "Show all projects" is off.
    ages = {"heute": MIDNIGHT + timedelta(minutes=1), "gestern": MIDNIGHT - timedelta(minutes=1),
            "drei": NOW - timedelta(days=3) + timedelta(minutes=1), "vier": NOW - timedelta(days=3, minutes=1),
            "sieben": NOW - timedelta(days=7) + timedelta(minutes=1), "acht": NOW - timedelta(days=7, minutes=1),
            "dreissig": NOW - timedelta(days=30) + timedelta(minutes=1),
            "alt": NOW - timedelta(days=30, minutes=1)}
    for title, at in ages.items():
        nr = mktask(env, project="ZWEI", title=f"Done-{title}")["task_nr"]
        _done(nr, at, NOW)
    nr = mktask(env, project="ZWEI", title="Done-fallback")["task_nr"]  # No done_at: updated_at counts.
    _done(nr, None, NOW - timedelta(days=2))
    _done(mktask(env, title="Done-KET")["task_nr"], NOW - timedelta(hours=1), NOW)  # Someone else's project.
    open_nr = mktask(env, project="ZWEI", title="Offen-alt")["task_nr"]
    with db.session() as s:  # Other columns are not filtered by period.
        s.execute(update(Task).where(Task.task_nr == open_nr).values(updated_at=NOW - timedelta(days=90)))
        s.commit()

    anna = _ui_login("anna", "anna-passwort-123")

    def shown(url: str) -> set[str]:
        col = _done_column(anna.get(url).text)
        return {t.split("<", 1)[0] for t in col.split("Done-")[1:]}

    r = anna.get("/kanban")
    assert '<option value="3d" selected>Erledigt: Letzte 3 Tage</option>' in r.text  # Default.
    assert "tm_done_range" not in r.cookies and "Offen-alt" in r.text
    assert shown("/kanban") == {"heute", "gestern", "drei", "fallback"}
    col = _done_column(r.text)
    assert ">4 von 9</span>" in col  # Filtered count, of all done tasks matching the other filters (KET excluded).
    assert shown("/kanban?done=today") == {"heute"}
    assert anna.cookies.get("tm_done_range") == "today"
    assert shown("/kanban") == {"heute"}  # Remembered in the cookie.
    assert shown("/kanban?done=7d") == {"heute", "gestern", "drei", "vier", "sieben", "fallback"}
    assert shown("/kanban?done=30d") == ages.keys() - {"alt"} | {"fallback"}
    r = anna.get("/kanban?done=all")
    assert ">9</span>" in _done_column(r.text) and "Die neuesten" not in r.text
    assert shown("/kanban?done=all&project=KET") == set()  # Project filter still applies.

    _show_all(anna, True)  # Profile switch on: other owners' done tasks join, period filter still applies.
    assert shown("/kanban?done=today&owner=all") == {"heute", "KET"}
    assert shown("/kanban?done=3d&owner=all&project=KET") == {"KET"}
    assert shown("/kanban?done=today&owner=all&agent=none") == {"heute", "KET"}
    assert shown("/kanban?done=today&owner=all&agent=claude-code") == set()


@pytest.mark.db
def test_ui_done_filter_all_is_capped_with_hint(env, monkeypatch):
    monkeypatch.setattr(tasks_ui, "KANBAN_DONE_LIMIT", 3)
    _two_owners(env)
    for i in range(5):
        nr = mktask(env, project="ZWEI", title=f"Done-{i}")["task_nr"]
        _done(nr, svc.utcnow() - timedelta(days=40 + i), svc.utcnow())
    anna = _ui_login("anna", "anna-passwort-123")
    col = _done_column(anna.get("/kanban?done=all").text)
    assert col.count('class="card"') == 3 and "Alle 5 im Backlog anzeigen" in col
    assert [f"Done-{i}" in col for i in range(5)] == [True, True, True, False, False]  # Newest first.
    assert "/backlog?status=done&amp;sort=done_at&amp;desc=1" in col
    backlog = anna.get("/backlog?status=done&sort=done_at&desc=1").text
    assert all(f"Done-{i}" in backlog for i in range(5))
