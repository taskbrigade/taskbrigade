"""`tbr join <server> <code>`: exchange a one-time join code (Admin → Neuer Agent) for an API token and set up the
agent's work directory with `.taskbrigade.toml` (mode 600)."""
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from cli.client import LOCAL_CONFIG, ApiError, Client, Config, agent_config
from cli.i18n import _
from cli.setup import RUNNER_SECTIONS

class JoinError(RuntimeError):
    pass


@dataclass
class JoinResult:
    config_path: Path
    actor: dict
    git: str  # ok | missing | failed
    notes: list[str] = field(default_factory=list)


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_text(url: str, token: str, agent_kind: str | None, language: str | None = None) -> str:
    lines = [f"url = {_toml_str(url)}", f"token = {_toml_str(token)}"]
    if language:
        lines.append(f"language = {_toml_str(language)}")
    return "\n".join(lines) + "\n" + RUNNER_SECTIONS.get(agent_kind or "", "")


def write_private(path: Path, text: str) -> None:
    """Write with mode 600 from the start (the token is never world-readable, not even briefly)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)  # O_CREAT keeps the mode of an existing file (--force).


def redeem(server: str, code: str) -> dict:
    try:
        r = httpx.post(f"{server}/api/v1/join", json={"code": code.strip()}, timeout=30)
    except httpx.HTTPError as e:
        raise JoinError(_("TaskBrigade is not reachable: %(error)s", error=e)) from e
    if r.status_code != 200:
        try:
            err = r.json()["error"]
            message, code_ = err.get("message", ""), err.get("code", "error")
        except (ValueError, KeyError, TypeError):
            message, code_ = r.text[:200], "error"
        hint = {"insecure_channel": _("Use the HTTPS address (tailscale serve) or run tbr join on the TaskBrigade "
                                      "host with http://127.0.0.1")}.get(code_)
        raise JoinError(f"{r.status_code} {code_}: {message}" + (f"\n→ {hint}" if hint else ""))
    return r.json()


def check_git(client: Client) -> tuple[str, str | None]:
    """(status, detail): the Gitea token from /agents/me/git-credentials must be accepted by Gitea."""
    try:
        creds = client.get("/agents/me/git-credentials")
    except ApiError as e:
        if e.status == 404:
            return "missing", None
        return "failed", str(e)
    url = (creds.get("gitea_url") or "").rstrip("/")
    if not url:
        return "missing", None
    try:
        r = httpx.get(f"{url}/api/v1/user", headers={"Authorization": f"token {creds['token']}"}, timeout=15)
    except httpx.HTTPError as e:
        return "failed", _("Gitea at %(url)s is not reachable: %(error)s", url=url, error=type(e).__name__)
    if r.status_code != 200:
        return "failed", _("Gitea at %(url)s rejects the token (HTTP %(status)s)", url=url, status=r.status_code)
    return "ok", _("Gitea user %(user)s at %(url)s", user=creds.get("username"), url=url)


def credential_helper_set() -> bool:
    try:
        out = subprocess.run(["git", "config", "--global", "--get-all", "credential.helper"], capture_output=True,
                             text=True).stdout
    except OSError:
        return False
    return any(f"{name} git-credential" in out for name in ("tbr", "tm"))


def join(server: str, code: str, directory: Path, force: bool = False, language: str | None = None,
         setup_kind: str | None = None) -> JoinResult:
    from cli import i18n

    if language is not None:
        if i18n.normalize(language) is None:
            raise JoinError(_("Unknown language '%(lang)s' – allowed: %(langs)s", lang=language,
                              langs=", ".join(i18n.LANGUAGES)))
        language = i18n.normalize(language)
        i18n.configure(language)
    if setup_kind is not None:
        from cli.setup import SetupError, normalize_kind

        try:
            normalize_kind(setup_kind)  # Before redeeming, so a typo does not burn the code.
        except SetupError as e:
            raise JoinError(str(e)) from e
    server = server.strip().rstrip("/")
    if not server.startswith(("http://", "https://")):
        raise JoinError(_("The server address must start with https:// or http://"))
    directory = directory.expanduser()
    path = directory / LOCAL_CONFIG
    existing = agent_config(directory)
    if existing.exists() and not force:
        raise JoinError(_("%(path)s already exists – use --force to overwrite it (the join code is still unused)",
                          path=existing))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = redeem(server, code)
    actor = data["actor"]
    write_private(path, config_text(server, data["token"], actor.get("agent_kind"), language))
    if existing != path:  # --force over a configuration with the old name: keep it only as a backup.
        existing.replace(existing.with_name(existing.name + ".bak"))
    client = Client(Config(server, data["token"], language))
    try:
        me = client.get("/agents/me")
    except ApiError as e:
        raise JoinError(_("%(path)s was written, but tbr whoami fails: %(error)s", path=path, error=e)) from e
    git, detail = check_git(client)
    notes = [detail] if detail else []
    if git == "ok" and not credential_helper_set():
        notes.append(_('Git credential helper missing – run once: git config --global credential.helper '
                       '"!tbr git-credential"'))
    if actor.get("agent_kind") in ("hermes", "openclaw") and setup_kind is None:
        notes.append(_("Install skill and schedule with: tbr setup %(kind)s --work-root %(dir)s",
                       kind=actor["agent_kind"], dir=directory.resolve()))
    elif actor.get("agent_kind") not in RUNNER_SECTIONS and setup_kind is None:
        notes.append(_("For tbr run add a [runner] section to %(path)s (docs/cli-agents.md)", path=path))
    return JoinResult(path, me, git, notes)
