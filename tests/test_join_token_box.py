"""Setup page: the fresh join code is also shown on its own in an orange box."""
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.i18n import use_language
from app.web.common import templates

NOW = datetime(2026, 10, 13, 21, 35, tzinfo=UTC)
CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.css"


def _render(lang: str, code: str | None) -> str:
    actor = SimpleNamespace(name="bot", agent_kind="other", runtime="local", role="worker", is_active=True,
                            gitea_username=None, gitea_token_last4=None, gitea_token_checked_at=None)
    ctx = {"actor": actor, "created": True, "code": code, "expires": NOW, "commands": {"join": "tbr join X tmj_abc",
           "install": "curl"}, "pending": SimpleNamespace(expires_at=NOW) if not code else None, "gitea_auto": False,
           "gitea_configured": False, "join_minutes": 15, "gitea_error": None, "csrf": "x",
           "request": SimpleNamespace(url=SimpleNamespace(path="/admin/agents/bot/setup"), query_params={}, cookies={})}
    with use_language(lang):
        return templates.get_template("admin/agent_setup.html").render(ctx)


def test_fresh_code_shown_in_token_box():
    en = _render("en", "tmj_abc")
    assert 'id="join-token-box"' in en and "Temporary sign-in token:" in en
    assert '<code id="join-token">tmj_abc</code>' in en and "Copy token" in en
    assert en.index("join-token-box") < en.index('id="join-cmd"')  # The command stays below the box.
    de = _render("de", "tmj_abc")
    assert "Temporärer Anmeldetoken:" in de and "Token kopieren" in de


def test_no_box_without_fresh_code():
    page = _render("en", None)
    assert "join-token" not in page and "it is not shown again" in page


def _lum(hex_color: str) -> float:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_token_box_contrast_meets_wcag_aa():
    css = CSS.read_text()
    bg = re.search(r"--tb-token-bg:\s*(#[0-9A-Fa-f]{6})", css).group(1)
    fg = re.search(r"--tb-token-fg:\s*(#[0-9A-Fa-f]{6})", css).group(1)
    hi, lo = sorted((_lum(bg), _lum(fg)), reverse=True)
    assert (hi + 0.05) / (lo + 0.05) >= 4.5
