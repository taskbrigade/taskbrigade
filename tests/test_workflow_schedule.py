"""Calendar/DST semantics independent of the database and scheduler process."""
import json
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

from app.services.workflows.schedule import is_workday, next_fire, preview, validate


def calendar(subdivision="BW", days=()):
    return NS(country="DE", subdivision=subdivision, workdays="mon,tue,wed,thu,fri", days=days)


def schedule(expr=None, rule=None, cal=None, **kwargs):
    return NS(kind="cron" if expr else "workday", cron_expr=expr,
              workday_rule_json=json.dumps(rule or {"nth": 3, "of": "month", "time": "07:00"}),
              calendar=cal, timezone="Europe/Berlin", holiday_shift="skip", overlap_policy="skip",
              misfire_policy="run_late", misfire_grace_min=360, params_json="{}", **kwargs)


@pytest.mark.parametrize("month,expected", [("2026-10", "2026-10-05T05:00:00"),
    ("2026-11", "2026-11-04T06:00:00"), ("2027-01", "2027-01-07T06:00:00")])
def test_spec_third_workday(month, expected):
    row = schedule(cal=calendar())
    assert next_fire(row, datetime.fromisoformat(month + "-01")) == datetime.fromisoformat(expected)
    validate(row)


def test_subdivision_and_exceptions():
    assert next_fire(schedule(cal=calendar("NW")), datetime(2027, 1, 1)) == datetime(2027, 1, 6, 6)
    cal = calendar(days=[NS(day=date(2027, 1, 2), kind="open"), NS(day=date(2027, 1, 4), kind="closed")])
    assert is_workday(cal, date(2027, 1, 2))
    assert not is_workday(cal, date(2027, 1, 4))
    assert not is_workday(cal, date(2027, 1, 6))


@pytest.mark.parametrize("rule,expected", [
    ({"nth": -1, "of": "month", "time": "16:00"}, datetime(2027, 1, 29, 15)),
    ({"nth": -2, "of": "month", "time": "09:00"}, datetime(2027, 1, 28, 8)),
    ({"day": 16, "of": "month", "shift": "next_workday", "time": "08:00"}, datetime(2027, 1, 18, 7)),
    ({"nth": 1, "of": "week", "time": "06:30"}, datetime(2027, 1, 4, 5, 30)),
    ({"nth": 5, "of": "quarter", "time": "07:00"}, datetime(2027, 1, 11, 6)),
])
def test_workday_forms(rule, expected):
    assert next_fire(schedule(rule=rule, cal=calendar()), datetime(2027, 1, 1)) == expected


def test_cron_gap_and_fold():
    row = schedule(expr="30 2 * * *")
    assert next_fire(row, datetime(2026, 3, 28, 23)) == datetime(2026, 3, 29, 1)
    assert next_fire(row, datetime(2026, 10, 24, 22)) == datetime(2026, 10, 25, 0, 30)
    assert next_fire(row, datetime(2026, 10, 25, 0, 30)) == datetime(2026, 10, 26, 1, 30)
    assert preview(row, datetime(2026, 10, 24), 2) == ["2026-10-24T02:30+02:00", "2026-10-25T02:30+02:00"]


@pytest.mark.parametrize("start,end,count", [(datetime(2026, 3, 28, 23), datetime(2026, 3, 29, 22), 23),
    (datetime(2026, 10, 24, 22), datetime(2026, 10, 25, 23), 25)])
def test_hourly_cron_utc_intervals(start, end, count):
    row = schedule(expr="0 * * * *")
    # Include the local midnight at the beginning of the day.
    from datetime import timedelta
    slots = []
    current = start - timedelta(seconds=1)
    while True:
        current = next_fire(row, current)
        if current >= end:
            break
        slots.append(current)
    assert len(slots) == count
    assert all(b - a == timedelta(hours=1) for a, b in zip(slots, slots[1:]))


def test_calendar_cron_shift_and_collision():
    row = schedule(expr="0 7 1 1 *", cal=calendar())
    row.holiday_shift = "next_workday"
    assert next_fire(row, datetime(2026, 12, 31)) == datetime(2027, 1, 4, 6)
    row.holiday_shift = "prev_workday"
    assert next_fire(row, datetime(2026, 12, 30)) == datetime(2026, 12, 31, 6)
    row.holiday_shift = "skip"
    assert next_fire(row, datetime(2026, 12, 30)) is None  # New Year's Day is never a workday.


@pytest.mark.parametrize("rule", [{"nth": 0, "of": "month", "time": "07:00"},
    {"nth": 3, "of": "year", "time": "07:00"}, {"nth": 3, "of": "month", "time": "25:00"},
    {"day": 32, "of": "month", "time": "07:00"}])
def test_invalid_rules(rule):
    with pytest.raises(ValueError):
        validate(schedule(rule=rule))


def test_missing_cron_and_invalid_calendar():
    row = schedule(expr="0 7 * * *")
    row.cron_expr = None
    with pytest.raises(ValueError):
        validate(row)
    row.cron_expr = "bad expression"
    with pytest.raises(ValueError):
        validate(row)


@pytest.mark.parametrize("policy", ["next_workday", "prev_workday"])
def test_shifted_weekend_slots_are_distinct(policy):
    row = schedule(expr="0 7 * * *", cal=calendar())
    row.holiday_shift = policy
    assert preview(row, datetime(2027, 1, 28, 6), 3) == [
        "2027-01-29T07:00+01:00", "2027-02-01T07:00+01:00", "2027-02-02T07:00+01:00"]


def test_app_trigger_modes(monkeypatch):
    from app import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "_with_session", lambda name, fn: calls.append(name))
    for mode, expected in (("timer", []), ("off", []), ("app", ["workflow_tick"])):
        monkeypatch.setattr(scheduler, "get_settings", lambda: NS(tbr_workflow_tick=mode))
        scheduler.workflow_tick()
        assert calls == expected
    job = scheduler.create_scheduler().get_job("workflow_tick")
    assert job.max_instances == 1 and job.coalesce
