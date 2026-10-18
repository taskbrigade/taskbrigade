"""Pure schedule calculations. CronTrigger is only a wall-clock expression parser; no job store.

DST is explicit: daily wall times use fold=0, gaps move to the first valid minute. Cron expressions
with an unrestricted hour follow UTC intervals and retain both folds. All public instants are naive UTC.
"""
import calendar as month_calendar
import json
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays
from apscheduler.triggers.cron import CronTrigger

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SHIFTS = ("skip", "next_workday", "prev_workday")


def utc(value):
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def cron(expr):
    """The sole APScheduler adapter. Five fields; weekday names recommended (Monday=0)."""
    return CronTrigger.from_crontab(expr, timezone=UTC)


@lru_cache(maxsize=128)
def _holidays(country, subdivision, year):
    return holidays.country_holidays(country, subdiv=subdivision, years=year)


def is_workday(cal, day):
    if cal is None:
        return day.weekday() < 5
    override = next((d.kind for d in cal.days if d.day == day), None)
    if override:
        return override == "open"
    return (WEEKDAYS[day.weekday()] in cal.workdays.split(",") and
            day not in _holidays(cal.country, cal.subdivision, day.year))


def shift_day(cal, day, policy):
    if is_workday(cal, day):
        return day
    if policy == "skip":
        return None
    delta = timedelta(days=1 if policy == "next_workday" else -1)
    for _ in range(366):
        day += delta
        if is_workday(cal, day):
            return day
    raise ValueError("Calendar has no workday within a year")


def wall_instants(wall, tz, hourly=False):
    first = wall.replace(tzinfo=tz, fold=0)
    back = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if back != wall:
        # A gap runs at its end, rather than retaining the minute within the gap.
        for _ in range(24 * 60):
            wall += timedelta(minutes=1)
            first = wall.replace(tzinfo=tz, fold=0)
            if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == wall:
                return [] if hourly else [utc(first)]
        raise ValueError("Invalid local date")
    result = [utc(first)]
    second = wall.replace(tzinfo=tz, fold=1)
    if hourly and second.utcoffset() != first.utcoffset():
        result.append(utc(second))
    return result


def validate(schedule):
    ZoneInfo(schedule.timezone)
    if schedule.kind == "cron":
        if not isinstance(schedule.cron_expr, str) or not schedule.cron_expr.strip():
            raise ValueError("A cron expression is required")
        cron(schedule.cron_expr)
    elif schedule.kind == "workday":
        rule = json.loads(schedule.workday_rule_json)
        if not isinstance(rule, dict) or rule.get("of") not in ("month", "week", "quarter"):
            raise ValueError("Invalid workday period")
        if set(rule) - {"of", "nth", "day", "shift", "time"}:
            raise ValueError("Unknown workday rule field")
        time.fromisoformat(rule["time"])
        if len(rule["time"]) != 5:
            raise ValueError("Time must be HH:MM")
        if ("nth" in rule) == ("day" in rule):
            raise ValueError("Choose nth or day")
        if "nth" in rule:
            limit = {"week": 7, "month": 31, "quarter": 92}[rule["of"]]
            if type(rule["nth"]) is not int or not 1 <= abs(rule["nth"]) <= limit:
                raise ValueError("Invalid workday index")
        elif rule["of"] != "month" or type(rule["day"]) is not int or not 1 <= rule["day"] <= 31:
            raise ValueError("Invalid day of month")
        if rule.get("shift", "next_workday") not in SHIFTS:
            raise ValueError("Invalid holiday shift")
    else:
        raise ValueError("Invalid schedule kind")
    if schedule.holiday_shift not in SHIFTS or schedule.overlap_policy not in ("skip", "queue"):
        raise ValueError("Invalid schedule policy")
    if schedule.misfire_policy not in ("run_late", "skip") or not 0 <= schedule.misfire_grace_min <= 525600:
        raise ValueError("Invalid misfire policy")
    params = json.loads(schedule.params_json or "{}")
    if not isinstance(params, dict):
        raise ValueError("Parameters must be an object")
    cal = schedule.calendar
    if cal:
        if not cal.workdays or set(cal.workdays.split(",")) - set(WEEKDAYS):
            raise ValueError("Invalid calendar weekdays")
        _holidays(cal.country, cal.subdivision, datetime.now(UTC).year)


def _period(day, kind):
    if kind == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    month = (day.month - 1) // 3 * 3 + 1 if kind == "quarter" else day.month
    start = day.replace(month=month, day=1)
    months = 3 if kind == "quarter" else 1
    return start, date(start.year + (month + months - 1) // 12, (month + months - 1) % 12 + 1, 1)


def _workday_date(cal, start, end, rule):
    if "day" in rule:
        if rule["day"] > month_calendar.monthrange(start.year, start.month)[1]:
            return None
        return shift_day(cal, start.replace(day=rule["day"]), rule.get("shift", "next_workday"))
    days = []
    while start < end:
        if is_workday(cal, start):
            days.append(start)
        start += timedelta(days=1)
    index = rule["nth"] - 1 if rule["nth"] > 0 else rule["nth"]
    return days[index] if -len(days) <= index < len(days) else None


def next_fire(schedule, after):
    """First distinct slot strictly after `after`, including shifted calendar slots."""
    after = utc(after)
    tz = ZoneInfo(schedule.timezone)
    local = after.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)
    best = None
    if schedule.kind == "workday":
        rule = json.loads(schedule.workday_rule_json)
        # A shifted day can land in another period; include the previous period.
        start, _ = _period(local.date() - timedelta(days=100), rule["of"])
        for _ in range(500):
            start, end = _period(start, rule["of"])
            day = _workday_date(schedule.calendar, start, end, rule)
            if day:
                for candidate in wall_instants(datetime.combine(day, time.fromisoformat(rule["time"])), tz):
                    if candidate > after and (best is None or candidate < best):
                        best = candidate
            if best and start > best.replace(tzinfo=UTC).astimezone(tz).date() + timedelta(days=366):
                return best
            # Without day shifting, future periods cannot precede this result.
            if best and "nth" in rule:
                return best
            start = end
        return best
    trigger = cron(schedule.cron_expr)
    shifted = schedule.calendar is not None and schedule.holiday_shift != "skip"
    # Dates before the previous workday cannot shift beyond today. Dates beyond the next
    # workday following the best candidate cannot shift back before that candidate either.
    first_day = local.date()
    if shifted and schedule.holiday_shift == "next_workday":
        first_day = shift_day(schedule.calendar, first_day - timedelta(days=1), "prev_workday")
    hourly = schedule.cron_expr.split()[1] in ("*", "*/1")
    if shifted:
        cursor = datetime.combine(first_day, time(), UTC)
    else:
        # Start near the requested instant instead of rescanning midnight for each minute slot.
        # During the first fold, earlier wall minutes in the second fold are still in the future.
        cursor_wall = local
        offset_delta = local.replace(tzinfo=tz, fold=0).utcoffset() - local.replace(tzinfo=tz, fold=1).utcoffset()
        if hourly and offset_delta > timedelta(0):
            cursor_wall -= offset_delta
        cursor = cursor_wall.replace(tzinfo=UTC)
    stop_day = None
    for _ in range(2_000_000):
        raw = trigger.get_next_fire_time(None, cursor)
        if raw is None or raw.year > local.year + 8:
            return best
        wall = raw.replace(tzinfo=None)
        if best and not shifted and utc(wall.replace(tzinfo=tz, fold=0)) > best:
            return best
        if best and shifted and wall.date() > stop_day:
            return best
        day = shift_day(schedule.calendar, wall.date(), schedule.holiday_shift) if schedule.calendar else wall.date()
        if day:
            for candidate in wall_instants(datetime.combine(day, wall.time()), tz, hourly):
                if candidate > after and (best is None or candidate < best):
                    best = candidate
                    if shifted:
                        stop_day = shift_day(schedule.calendar,
                            candidate.replace(tzinfo=UTC).astimezone(tz).date() + timedelta(days=1), "next_workday")
        cursor = raw + timedelta(seconds=1)
    raise ValueError("Schedule calculation limit reached")


def preview(schedule, after, count=5):
    result = []
    for _ in range(count):
        after = next_fire(schedule, after)
        if after is None:
            break
        result.append(after.replace(tzinfo=UTC).astimezone(ZoneInfo(schedule.timezone)).isoformat(timespec="minutes"))
    return result
