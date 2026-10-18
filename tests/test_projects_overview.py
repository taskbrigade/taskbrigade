"""Projects overview service (`app/services/projects.overview`) and the relative-time filter.

Plain unit tests (no DB) for the filter; the aggregate query is covered against the test DB in
`tests/test_projects_ui.py`.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.i18n import relative_time, use_language
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


def test_relative_time_days_and_fallback():
    """today/yesterday/„n days ago“ per TZ_DISPLAY, older values as locale date, `None` as em dash."""
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    with use_language("en"):
        assert relative_time(None) == "–"
        assert relative_time(now, now) == "today"
        assert relative_time(now - timedelta(hours=6), now) == "today"  # Same day, hours do not matter.
        assert relative_time(now - timedelta(days=1), now) == "yesterday"
        assert relative_time(now - timedelta(days=3), now) == "3 days ago"
        assert relative_time(datetime(2026, 10, 1, 12, 0, tzinfo=UTC), now) == "2026-10-01"
    with use_language("de"):
        assert relative_time(now, now) == "heute"
        assert relative_time(now - timedelta(days=1), now) == "gestern"
        assert relative_time(now - timedelta(days=4), now) == "vor 4 Tagen"
        assert relative_time(datetime(2026, 10, 1, 12, 0, tzinfo=UTC), now) == "01.10.2026"


def test_relative_time_naive_and_future_values():
    """Naive timestamps count as UTC; a future timestamp (clock skew) does not produce a negative day count."""
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    with use_language("en"):
        assert relative_time(datetime(2026, 10, 17, 6, 0), now) == "today"
        assert relative_time(now + timedelta(days=2), now) == "today"


@pytest.mark.db
def test_overview_groups_per_project(env):  # noqa: F811 (fixtures imported above)
    """`overview` returns one entry per project with counts, last activity and agent count – no query per row."""
    from sqlalchemy import text

    from app.db import db
    from app.services import projects

    mkproject(env, key="KET")
    mkproject(env, key="ZWEI")
    mktask(env, title="A")
    with db.session() as s:
        ids = dict(s.execute(text("SELECT project_key, id FROM projects")).all())
        s.execute(text("INSERT INTO project_members (project_id, actor_id) "
                       "SELECT :p, id FROM actors WHERE name='claude-code'"), {"p": ids["KET"]})
        s.commit()
        rows = projects.overview(s, [ids["KET"], ids["ZWEI"], 9999])
    assert set(rows) == {ids["KET"], ids["ZWEI"], 9999}  # Empty projects are present too (no KeyError).
    assert rows[ids["KET"]]["counts"] == {"defined": 1}
    assert rows[ids["KET"]]["agents"] == 1
    assert rows[ids["KET"]]["last_activity"] is not None
    assert rows[ids["ZWEI"]] == {"counts": {}, "last_activity": None, "agents": 0}
    assert projects.overview(s, []) == {}
