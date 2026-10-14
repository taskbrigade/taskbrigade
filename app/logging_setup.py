"""Structured JSON logging to stdout (journald)."""
import json
import logging
import re
import sys
from datetime import UTC, datetime

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
# Telegram bot tokens sit in the request URL (api.telegram.org/bot<id>:<secret>/…) and thus in httpx messages,
# exception texts and tracebacks – never let one reach the log.
_BOT_TOKEN = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")


def redact(text: str) -> str:
    return _BOT_TOKEN.sub("bot<redacted>", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(data, default=str, ensure_ascii=False))


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("httpx", "httpcore"):   # INFO logs every request URL (Telegram: incl. the bot token)
        logging.getLogger(name).setLevel(logging.WARNING)


audit_log = logging.getLogger("admin_audit")


def audit(actor: str, action: str, **fields) -> None:
    """Log an admin action. Never pass secrets in `fields`."""
    audit_log.info(action, extra={"audit_actor": actor, "audit_action": action, **fields})
