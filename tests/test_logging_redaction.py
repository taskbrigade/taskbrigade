"""Telegram bot tokens never reach the log (httpx request lines, exception texts, tracebacks)."""
import json
import logging

from app.logging_setup import JsonFormatter, redact, setup_logging

TOKEN = "12345678:" + "Ab_-" * 9          # shape of a bot token, built so secret scanners do not flag it
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"


def _format(msg: str, *args, exc: BaseException | None = None, **extra) -> str:
    record = logging.LogRecord("test", logging.WARNING, __file__, 1, msg, args, None)
    record.__dict__.update(extra)
    if exc is not None:
        try:
            raise exc
        except BaseException as e:   # noqa: BLE001 – capture exc_info like log.exception
            record.exc_info = (type(e), e, e.__traceback__)
    return JsonFormatter().format(record)


def test_redact_replaces_bot_token():
    assert redact(f"GET {URL}") == "GET https://api.telegram.org/bot<redacted>/getUpdates"
    assert redact("robot 42: nothing to hide") == "robot 42: nothing to hide"


def test_formatter_redacts_message_extra_fields_and_traceback():
    line = _format("HTTP Request: POST %s", URL, error=f"Client error for url '{URL}'",
                   exc=RuntimeError(f"failed: {URL}"))
    assert TOKEN not in line
    assert "bot<redacted>" in line
    assert json.loads(line)["logger"] == "test"     # still valid JSON


def test_setup_logging_silences_httpx_request_lines():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    previous = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    try:
        setup_logging()
        for name in ("httpx", "httpcore"):
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
        assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, lvl in previous.items():
            logging.getLogger(name).setLevel(lvl)
