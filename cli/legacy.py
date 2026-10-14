"""Old command names `tm` and `tm-admin` during the transition to `tbr` and `tbr-admin` (TaskBrigade 0.5).

Same commands, same behaviour; only one rename notice per call on stderr. `TBR_NO_RENAME_NOTICE=1` turns it off.
No notice for machine interfaces whose stderr nobody reads or that an app may show as an error: the git credential
helper, the MCP server and the wake gate of `tm prepare` (Hermes/OpenClaw scripts installed before the rename).
"""
import os
import sys

from cli.i18n import _

NOTICE_ENV = "TBR_NO_RENAME_NOTICE"
QUIET_COMMANDS = ("git-credential", "mcp")


def wants_notice(argv: list[str], environ=os.environ) -> bool:
    if environ.get(NOTICE_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    args = argv[1:]
    command = next((a for a in args if not a.startswith("-")), "")
    return command not in QUIET_COMMANDS and not (command == "prepare" and "--wake-gate" in args)


def notice(old: str, new: str) -> str:
    return _("Note: %(old)s is now called %(new)s (TaskBrigade) – %(old)s keeps working during the transition. "
             "Hide this note with %(env)s=1.", old=old, new=new, env=NOTICE_ENV)


def _notify(old: str, new: str) -> None:
    if wants_notice(sys.argv):
        print(notice(old, new), file=sys.stderr)


def tm() -> None:
    _notify("tm", "tbr")
    from cli.tm import app

    app()


def tm_admin() -> None:
    _notify("tm-admin", "tbr-admin")
    from cli.tm_admin import app

    app()


# --- Names from before TaskBrigade 0.8 (configuration file, TM_* variables) ---------------------------------------

ENV_PREFIX = "TBR_"
LEGACY_ENV_PREFIX = "TM_"
_noted: set[str] = set()


def note_once(key: str, text: str) -> None:
    """A rename note on stderr, once per process and `key`; same quiet rules as the command notice."""
    if key in _noted or not wants_notice(sys.argv):
        return
    _noted.add(key)
    print(text, file=sys.stderr)


def getenv(name: str, default: str | None = None, environ=None) -> str | None:
    """`TBR_<name>`, else the old `TM_<name>` (with a note); empty counts as unset."""
    environ = os.environ if environ is None else environ
    value = environ.get(ENV_PREFIX + name)
    if value:
        return value
    old = environ.get(LEGACY_ENV_PREFIX + name)
    if old:
        note_once(LEGACY_ENV_PREFIX + name, _(
            "Note: %(old)s is now called %(new)s (TaskBrigade) – %(old)s is still read during the transition.",
            old=LEGACY_ENV_PREFIX + name, new=ENV_PREFIX + name))
        return old
    return default
