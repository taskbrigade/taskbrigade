"""Translation layer for the web UI and service/API messages.

English source strings are the message ids; other languages live in `app/locales/<lang>.json`
(`"msgid": "translation"`, plural entries as a list of forms keyed by the singular msgid).
The language of the current request is kept in a context variable (`LanguageMiddleware`); without a
request the configured default applies.

Python:  `_("Saved.")`, `_("Hello %(name)s", name=n)`, `ngettext("%(num)d task", "%(num)d tasks", n)`,
         `N_("…")` only marks a string for translation (translated later with `_()`).
Jinja:   `{{ _("Saved.") }}`, `{{ _("Hello %(name)s", name=n) }}`, `{{ ngettext("…", "…", n) }}` –
         Jinja's i18n extension (newstyle) formats and escapes; a literal `%` must be written as `%%`.
"""
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from http.cookies import SimpleCookie
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
SOURCE_LANGUAGE = "en"
LANGUAGES = {"en": "English", "de": "Deutsch"}
LANG_COOKIE = "tm_lang"

# Plural rule per language: n → index into the list of forms (gettext semantics)
PLURAL_RULES: dict[str, Callable[[int], int]] = {
    "en": lambda n: 0 if n == 1 else 1,
    "de": lambda n: 0 if n == 1 else 1,
}

# Named date/time formats for the `localtime` filter
DATE_FORMATS = {
    "en": {"datetime": "%Y-%m-%d %H:%M", "short": "%Y-%m-%d %H:%M", "date": "%Y-%m-%d", "daytime": "%m-%d %H:%M"},
    "de": {"datetime": "%d.%m.%Y %H:%M", "short": "%d.%m.%y %H:%M", "date": "%d.%m.%Y", "daytime": "%d.%m. %H:%M"},
}
DECIMAL_SEPARATOR = {"en": ".", "de": ","}

_current: ContextVar[str | None] = ContextVar("tm_language", default=None)
_missing: ContextVar[set | None] = ContextVar("tm_missing_translations", default=None)


@cache
def catalog(lang: str) -> dict[str, str | list[str]]:
    if lang == SOURCE_LANGUAGE:
        return {}
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def normalize(lang: str | None) -> str | None:
    """`de-DE`, `DE` → `de`; unknown languages → None."""
    code = (lang or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGUAGES else None


# --- default language: admin setting (DB) → .env `UI_LANGUAGE` → English --------------------------

_DEFAULT_TTL = 30.0
_default_cache: tuple[float, str] | None = None


def clear_default_cache() -> None:
    global _default_cache
    _default_cache = None


def _load_default() -> str:
    from app.config import get_settings
    from app.db import db

    lang = None
    if db.ready:
        try:
            from app.services import settings_store

            with db.session() as s:
                lang = settings_store.get(s, "ui_language")
        except Exception:  # noqa: BLE001 – a broken DB must not take the UI language down with it
            lang = None
    return normalize(lang) or normalize(get_settings().ui_language) or SOURCE_LANGUAGE


def default_language() -> str:
    global _default_cache
    now = time.monotonic()
    if _default_cache is None or _default_cache[0] < now:
        _default_cache = (now + _DEFAULT_TTL, _load_default())
    return _default_cache[1]


def get_language() -> str:
    return _current.get() or default_language()


def set_language(lang: str | None):
    """Sets the language for the current context; returns a token for `reset_language`."""
    return _current.set(normalize(lang))


def reset_language(token) -> None:
    _current.reset(token)


@contextmanager
def use_language(lang: str | None) -> Iterator[None]:
    token = set_language(lang)
    try:
        yield
    finally:
        reset_language(token)


# --- lookup ----------------------------------------------------------------------------------------

def _record_missing(msgid: str) -> None:
    missing = _missing.get()
    if missing is not None:
        missing.add(msgid)


@contextmanager
def track_missing() -> Iterator[set[str]]:
    """Collects msgids without a translation in the active language (used by the tests)."""
    found: set[str] = set()
    token = _missing.set(found)
    try:
        yield found
    finally:
        _missing.reset(token)


def translate(msgid: str) -> str:
    """Translation without formatting (Jinja formats itself)."""
    lang = get_language()
    if lang == SOURCE_LANGUAGE:
        return msgid
    entry = catalog(lang).get(msgid)
    if isinstance(entry, str) and entry:
        return entry
    _record_missing(msgid)
    return msgid


def translate_plural(singular: str, plural: str, n: int) -> str:
    lang = get_language()
    index = PLURAL_RULES.get(lang, PLURAL_RULES[SOURCE_LANGUAGE])(n)
    if lang != SOURCE_LANGUAGE:
        entry = catalog(lang).get(singular)
        if isinstance(entry, list) and len(entry) > index:
            return entry[index]
        _record_missing(singular)
    return singular if index == 0 else plural


def gettext(msgid: str, **variables) -> str:
    text = translate(msgid)
    return text % variables if variables else text


def ngettext(singular: str, plural: str, n: int, **variables) -> str:
    variables.setdefault("num", n)
    return translate_plural(singular, plural, n) % variables


_ = gettext


def N_(msgid: str) -> str:  # noqa: N802 – gettext convention
    """Flags a string for extraction; translate it with `_()` where it is shown."""
    return msgid


# --- formatting helpers ------------------------------------------------------------------------------

def date_format(name: str) -> str:
    return DATE_FORMATS.get(get_language(), DATE_FORMATS[SOURCE_LANGUAGE])[name]


def decimal(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", DECIMAL_SEPARATOR.get(get_language(), "."))


# --- per-request language ------------------------------------------------------------------------------

def language_from_cookie_header(header: str | None) -> str | None:
    if not header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:  # noqa: BLE001 – malformed cookies → default language
        return None
    morsel = cookie.get(LANG_COOKIE)
    return normalize(morsel.value) if morsel else None


class LanguageMiddleware:
    """Pure ASGI middleware: request language = cookie `tm_lang` (personal choice) or the default."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        header = next((v.decode("latin-1") for k, v in scope.get("headers", ()) if k == b"cookie"), None)
        token = set_language(language_from_cookie_header(header))  # None → default, resolved on first use
        try:
            await self.app(scope, receive, send)
        finally:
            reset_language(token)
