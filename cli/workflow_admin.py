"""Operator scheduler entry points; database operations require a current schema."""
import calendar as month_calendar
import json
from datetime import date, datetime
from types import SimpleNamespace

import typer
from sqlalchemy import select

from cli.i18n import _


def database():
    from app.config import get_settings
    from app.db import db

    result = db.init_from_settings(get_settings())
    if not db.ready:
        typer.echo(_("Database not ready: %(error)s", error=result.error or "schema is not up to date"), err=True)
        raise typer.Exit(1)
    return db


def register(app):
    group = typer.Typer(help=_("Workflow scheduler operations."), no_args_is_help=True)
    calendar = typer.Typer(help=_("Workflow workday calendars."), no_args_is_help=True)
    app.add_typer(group, name="workflows")
    group.add_typer(calendar, name="calendar")

    @group.command("tick", help=_("Process due workflow schedules."))
    def tick(now: str = typer.Option(None, "--now")):
        from app.services.workflows.tick import tick as process
        try:
            instant = datetime.fromisoformat(now) if now else None
        except ValueError:
            raise typer.BadParameter(_("Use an ISO datetime; an omitted offset means UTC.")) from None
        with database().session() as session:
            result = process(session, instant)
        typer.echo(json.dumps(result))
        if result.get("failed"):
            raise typer.Exit(1)

    @group.command("schedules", help=_("Show upcoming workflow schedules."))
    def schedules(next_count: int = typer.Option(5, "--next", min=1, max=100)):
        from app.models import WorkflowSchedule
        from app.services.workflows.schedule import preview
        from datetime import UTC

        with database().session() as session:
            for row in session.scalars(select(WorkflowSchedule).order_by(WorkflowSchedule.id)):
                typer.echo(f"{row.id} {row.template.project.project_key}/{row.template.workflow_key} {row.name} "
                           f"enabled={row.enabled} timezone={row.timezone}")
                for instant in preview(row, datetime.now(UTC), next_count):
                    typer.echo(f"  {instant}")

    @calendar.command("check", help=_("Check workdays in a month."))
    def check(calendar_key: str, month: str):
        from app.services.workflows.schedule import is_workday
        from app.models import WorkflowCalendar

        try:
            start = date.fromisoformat(month + "-01")
            if calendar_key.isdigit():
                with database().session() as session:
                    cal = session.get(WorkflowCalendar, int(calendar_key))
                    if cal is None:
                        raise ValueError
                    # Consume the relation while attached.
                    cal.days
                    _show_month(cal, start, is_workday)
            else:
                country, separator, subdivision = calendar_key.partition("-")
                cal = SimpleNamespace(country=country, subdivision=subdivision or None,
                                      workdays="mon,tue,wed,thu,fri", days=[])
                _show_month(cal, start, is_workday)
        except (ValueError, KeyError, NotImplementedError):
            raise typer.BadParameter(_("Use a calendar ID or country-subdivision and YYYY-MM.")) from None

    @calendar.command("add", help=_("Create a shared workday calendar."))
    def add(name: str, country: str = "DE", subdivision: str = "",
            workdays: str = "mon,tue,wed,thu,fri"):
        from app.models import WorkflowCalendar
        from app.services.workflows.schedule import WEEKDAYS, _holidays

        try:
            if (not name or len(name) > 80 or len(country) != 2 or len(subdivision) > 10 or
                    not workdays or len(workdays) > 30 or set(workdays.split(",")) - set(WEEKDAYS)):
                raise ValueError
            _holidays(country, subdivision or None, date.today().year)
        except (ValueError, KeyError, NotImplementedError):
            raise typer.BadParameter(_("Invalid workflow schedule or calendar")) from None
        with database().session() as session:
            row = WorkflowCalendar(name=name, country=country, subdivision=subdivision or None, workdays=workdays)
            session.add(row)
            session.commit()
            typer.echo(str(row.id))

    @calendar.command("day", help=_("Set or remove a calendar exception; kind is open, closed or remove."))
    def day(calendar_id: int, day: str, kind: str, note: str = ""):
        from app.models import WorkflowCalendar, WorkflowCalendarDay, WorkflowSchedule
        from app.services.workflows.schedule import next_fire
        from datetime import UTC

        try:
            instant = date.fromisoformat(day)
            if kind not in ("open", "closed", "remove") or len(note) > 100:
                raise ValueError
        except ValueError:
            raise typer.BadParameter(_("Use YYYY-MM-DD and open, closed or remove.")) from None
        with database().session() as session:
            # Tick and configuration use the same template locks.
            rows = list(session.scalars(select(WorkflowSchedule).where(WorkflowSchedule.calendar_id == calendar_id)))
            from app.models import WorkflowTemplate
            for template_id in sorted({r.template_id for r in rows}):
                session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template_id).with_for_update())
            cal = session.get(WorkflowCalendar, calendar_id)
            if cal is None:
                raise typer.BadParameter(_("Workflow calendar not found"))
            row = session.get(WorkflowCalendarDay, (calendar_id, instant))
            if kind == "remove":
                if row:
                    session.delete(row)
            elif row:
                row.kind, row.note = kind, note
            else:
                session.add(WorkflowCalendarDay(calendar_id=calendar_id, day=instant, kind=kind, note=note))
            session.flush()
            session.expire(cal, ["days"])
            now = datetime.now(UTC).replace(tzinfo=None)
            for schedule in rows:
                schedule.last_fire_at, schedule.next_fire_at = now, next_fire(schedule, now)
            session.commit()
        typer.echo(_("Calendar saved"))


def _show_month(cal, start, is_workday):
    for n in range(1, month_calendar.monthrange(start.year, start.month)[1] + 1):
        day = start.replace(day=n)
        if is_workday(cal, day):
            typer.echo(day.isoformat())
