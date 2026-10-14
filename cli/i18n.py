"""Translation layer for agent-facing texts: TASK.md instructions, runner prompts, MCP server, `tbr` output.

English source strings are the message ids; other languages live in `cli/locales/<lang>.json` (same format as
the web UI catalog `app/locales`). The CLI runs on agent machines, so its language is chosen there, independent
of the web UI language:

    1. environment variable `TBR_LANGUAGE` (`en` | `de`; the old `TM_LANGUAGE` is still read)
    2. `language = "de"` in the agent's `.taskbrigade.toml` (or the config file `tbr` uses)
    3. English

Usage: `_("Task %(nr)s submitted", nr=nr)` – placeholders in `%(name)s` form, a literal `%` as `%%` when
variables are passed.
"""
import json
import os
import tomllib
from functools import cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
SOURCE_LANGUAGE = "en"
LANGUAGES = ("en", "de")
ENV_VAR = "TBR_LANGUAGE"
LEGACY_ENV_VAR = "TM_LANGUAGE"  # Before TaskBrigade 0.8, still read.

_configured: str | None = None


def normalize(lang: str | None) -> str | None:
    """`de-DE`, `DE` → `de`; unknown languages → None."""
    code = str(lang or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGUAGES else None


def configure(lang: str | None) -> None:
    """Language from an explicitly loaded config file (`load_config`); None keeps the automatic lookup."""
    global _configured
    _configured = normalize(lang)


@cache
def _from_config_file() -> str | None:
    from cli.client import find_config

    try:
        path = find_config()
        return normalize(tomllib.loads(path.read_text()).get("language")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def language() -> str:
    return normalize(os.environ.get(ENV_VAR) or os.environ.get(LEGACY_ENV_VAR)) or _configured or _from_config_file() or SOURCE_LANGUAGE


@cache
def catalog(lang: str) -> dict[str, str]:
    if lang == SOURCE_LANGUAGE:
        return {}
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def gettext(msgid: str, **variables) -> str:
    lang = language()
    text = msgid
    if lang != SOURCE_LANGUAGE:
        entry = catalog(lang).get(msgid)
        if isinstance(entry, str) and entry:
            text = entry
    return text % variables if variables else text


_ = gettext


def N_(msgid: str) -> str:  # noqa: N802 – gettext convention
    """Flags a string for extraction; translate it with `_()` where it is used."""
    return msgid
