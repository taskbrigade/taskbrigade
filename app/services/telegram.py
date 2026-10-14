"""Telegram Bot API client (only the calls TaskBrigade needs) and the bot settings.

The bot is TaskBrigade's own (token from @BotFather in *Admin → Einstellungen*, stored encrypted). Updates arrive by
long polling (`getUpdates`, `app/services/telegram_bot.py`), never by webhook, so the server needs no inbound
internet access. Tests replace `api_factory`; no test talks to the real API.
"""
import logging

import httpx
from sqlalchemy.orm import Session

from app.i18n import _
from app.services import settings_store

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_TEXT = 4096  # Telegram's limit for one message


class TelegramError(Exception):
    """A failed Bot API call. `retry_after` (seconds) is set for 429, `permanent` for errors a retry cannot fix
    (invalid token, chat not found, bot blocked by the user)."""

    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after

    @property
    def permanent(self) -> bool:
        return self.status in (400, 401, 403, 404)


class TelegramApi:
    def __init__(self, token: str, base: str = API_BASE, timeout: float = 15.0):
        self.token, self.base, self.timeout = token, base.rstrip("/"), timeout

    def _call(self, method: str, params: dict | None = None, timeout: float | None = None):
        url = f"{self.base}/bot{self.token}/{method}"
        try:
            r = httpx.post(url, json=params or {}, timeout=timeout or self.timeout)
        except httpx.HTTPError as e:
            # The URL contains the token: report only the error type.
            raise TelegramError(_("Telegram not reachable (%(error)s)", error=type(e).__name__)) from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200 and data.get("ok"):
            return data.get("result")
        retry = (data.get("parameters") or {}).get("retry_after")
        raise TelegramError(f"Telegram {r.status_code}: {str(data.get('description') or r.reason_phrase)[:150]}",
                            r.status_code, int(retry) if retry else None)

    def get_me(self) -> dict:
        return self._call("getMe")

    def set_commands(self, commands: list[tuple[str, str]]) -> dict:
        return self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None,
                     reply_to: int | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self._call("sendMessage", params)

    def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> dict:
        """Replace the inline buttons of a sent message (None removes them)."""
        return self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                     "reply_markup": reply_markup or {"inline_keyboard": []}})

    def answer_callback(self, callback_id: str, text: str | None = None) -> dict:
        """Confirm a button press (ends the spinner); `text` is shown briefly on the user's screen."""
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        return self._call("answerCallbackQuery", params)

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, timeout=timeout + 10) or []


api_factory = TelegramApi  # Tests replace this with a fake.

# Bot user name from the last successful getMe (for the t.me link in the profile); None = unknown.
bot_info: dict = {"username": None}


def token(session: Session) -> str | None:
    return settings_store.get(session, "telegram_bot_token")


def enabled(session: Session) -> bool:
    return settings_store.get_bool(session, "telegram_enabled") and bool(token(session))


def api(session: Session) -> TelegramApi | None:
    """API client when the bot is enabled and has a token, else None."""
    return api_factory(token(session)) if enabled(session) else None


def test_connection(session: Session) -> str:
    """`getMe` with the stored token (also while the bot is switched off); raises TelegramError."""
    t = token(session)
    if not t:
        raise TelegramError(_("No Telegram bot token set"))
    me = api_factory(t).get_me() or {}
    bot_info["username"] = me.get("username")
    return me.get("username") or str(me.get("id") or "?")
