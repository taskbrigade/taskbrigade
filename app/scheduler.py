"""APScheduler in the app process. Jobs check DB readiness (setup mode pauses them)."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.i18n import _
from app.config import get_settings
from app.db import db
from app.services.notify import notify

log = logging.getLogger(__name__)


def db_health() -> None:
    was_ready = db.ready
    if db.engine is None:
        db.init_from_settings(get_settings())  # For example, after `tbr-admin set-db-password`.
    else:
        db.recheck()
    if was_ready and not db.ready:
        log.error("db_lost", extra={"error": db.last_probe.error})
        notify("db_unavailable", _("TaskBrigade: database unreachable – %(error)s", error=db.last_probe.error))
    elif not was_ready and db.ready:
        log.info("db_restored")


def _with_session(job_name: str, fn) -> None:
    """Run a job only when the DB is ready; log errors without raising them."""
    if not db.ready:
        return
    try:
        with db.session() as session:
            result = fn(session)
            session.commit()
        if result:
            log.info(job_name, extra={"result": result})
    except Exception:
        log.exception(f"{job_name}_failed")


def lease_sweep() -> None:
    from app.services.tasks import sweep_expired_leases

    _with_session("lease_sweep", sweep_expired_leases)


def collaborator_sync() -> None:
    from app.services.members import sync_pending

    _with_session("collaborator_sync", sync_pending)


def telegram_deliver() -> None:
    """Send queued Telegram notifications – only in the process that holds the bot lock (one sender per server)."""
    from app.services import notifications, telegram_bot

    if telegram_bot.owns_lock():
        _with_session("telegram_deliver", lambda s: {k: v for k, v in notifications.deliver(s).items() if v})


gitea_status: dict = {"ok": None, "error": None}


def gitea_health() -> None:
    from app.services import settings_store
    from app.services.gitea import GiteaError

    def check(session):
        try:
            settings_store.gitea_client(session).version()
            gitea_status.update(ok=True, error=None)
        except GiteaError as e:
            if gitea_status.get("ok") is not False:
                log.warning("gitea_unreachable", extra={"error": str(e)})
            gitea_status.update(ok=False, error=str(e))

    _with_session("gitea_health", check)


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    opts = {"coalesce": True, "max_instances": 1}
    scheduler.add_job(db_health, "interval", minutes=1, id="db_health", **opts)
    scheduler.add_job(lease_sweep, "interval", minutes=1, id="lease_sweep", **opts)
    scheduler.add_job(gitea_health, "interval", minutes=5, id="gitea_health", **opts)
    scheduler.add_job(telegram_deliver, "interval", seconds=15, id="telegram_deliver", **opts)
    scheduler.add_job(collaborator_sync, "interval", minutes=15, id="collaborator_sync", **opts)
    return scheduler
