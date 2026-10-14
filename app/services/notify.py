"""Notifications. Telegram is deferred; log a no-op until then so callers can use the interface."""
import logging

log = logging.getLogger(__name__)


def notify(event: str, message: str, link: str | None = None, owner: str | None = None) -> None:
    """Never block or raise exceptions. `owner` is the recipient (project owner)."""
    try:
        log.info("notify", extra={"event": event, "text": message, "link": link, "owner": owner})
    except Exception:  # pragma: no cover
        pass
