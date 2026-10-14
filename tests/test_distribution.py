"""tbr delivery from the server: wheel under /download/, /install.sh, version check, self-update, network policy."""
import os
import shutil
import subprocess
import sys

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.main import VERSION_HEADER, app
from app.services import distribution
from cli import client as client_mod
from cli import selfupdate, tm
from cli.mcp_server import TaskBrigadeMCP

VERSION = "9.8.7"
WHEEL = f"taskbrigade-{VERSION}-py3-none-any.whl"
LEGACY_WHEEL = f"taskmeister-{VERSION}-py3-none-any.whl"


@pytest.fixture
def dist(tmp_path, monkeypatch, settings):
    """Server version 9.8.7 with its wheel in TBR_DIST_DIR; building is never attempted for real."""
    monkeypatch.setattr(distribution, "app_version", lambda: VERSION)
    monkeypatch.setattr(distribution, "_last_failed_build", float("-inf"))
    monkeypatch.setattr(distribution, "SOURCE_ROOT", tmp_path / "src")
    monkeypatch.setattr(settings, "tbr_dist_dir", str(tmp_path / "dist"))
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / WHEEL).write_bytes(b"PK-wheel")
    return tmp_path / "dist"


def get(path: str, ip: str = "127.0.0.1"):
    with TestClient(app, client=(ip, 50000)) as c:
        return c.get(path, follow_redirects=False)


# --- Delivery ---------------------------------------------------------------------------------

def test_install_sh_names_server_version_and_wheel(dist):
    r = get("/install.sh")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-shellscript")
    body = r.text
    assert body.startswith("#!/bin/sh")
    assert 'TBR_URL="${TBR_URL:-${TM_URL:-http://testserver}}"' in body
    assert f'TBR_VERSION="{VERSION}"' in body and f'WHEEL="{WHEEL}"' in body
    assert "@URL@" not in body and "@VERSION@" not in body and "@WHEEL@" not in body
    assert r.headers["cache-control"] == "no-store"


def test_install_sh_is_valid_posix_sh(dist):
    if not shutil.which("sh"):
        pytest.skip("no sh")
    subprocess.run(["sh", "-n"], input=get("/install.sh").text, text=True, check=True)


def test_download_serves_exactly_the_server_wheel(dist):
    r = get(f"/download/{WHEEL}")
    assert r.status_code == 200 and r.content == b"PK-wheel"
    assert get("/download/taskmeister-0.0.1-py3-none-any.whl").status_code == 404
    assert get("/download/..%2F..%2Fmaster.key").status_code == 404
    assert WHEEL in get("/download/other.whl").text


def test_wheel_from_source_dist_dir(dist, tmp_path):
    (dist / WHEEL).unlink()
    (tmp_path / "src" / "dist").mkdir(parents=True)
    (tmp_path / "src" / "dist" / WHEEL).write_bytes(b"from-checkout")
    assert get(f"/download/{WHEEL}").content == b"from-checkout"


def test_missing_wheel_without_uv_is_503_and_not_retried_at_once(dist, monkeypatch):
    (dist / WHEEL).unlink()
    (dist.parent / "src").mkdir()
    (dist.parent / "src" / "pyproject.toml").write_text("[project]\n")
    calls = []
    monkeypatch.setattr(distribution.shutil, "which", lambda name: calls.append(name) and None)
    assert get("/install.sh").status_code == 503
    assert get(f"/download/{WHEEL}").status_code == 503
    assert calls == ["uv"]  # Back-off: the second request does not try again.


def test_missing_wheel_is_built_with_uv(dist, monkeypatch):
    (dist / WHEEL).unlink()
    (dist.parent / "src").mkdir()
    (dist.parent / "src" / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(distribution.shutil, "which", lambda name: "/usr/bin/uv")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        out = cmd[cmd.index("--out-dir") + 1]
        with open(os.path.join(out, WHEEL), "wb") as f:
            f.write(b"built")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(distribution.subprocess, "run", fake_run)
    assert get(f"/download/{WHEEL}").content == b"built"
    assert seen == [["/usr/bin/uv", "build", "--wheel", "--out-dir", str(dist), str(dist.parent / "src")]]
    assert get(f"/download/{WHEEL}").content == b"built" and len(seen) == 1


def test_unknown_version_serves_nothing(dist, monkeypatch):
    monkeypatch.setattr(distribution, "app_version", lambda: "")
    assert get("/install.sh").status_code == 503
    assert get(f"/download/{WHEEL}").status_code == 404


def test_responses_carry_the_server_version(dist):
    assert get("/healthz").headers[VERSION_HEADER] == VERSION
    assert get("/login").headers[VERSION_HEADER] == VERSION


# --- Network policy: only peers that may use the API ---------------------------------------------

@pytest.fixture
def policy(dist, monkeypatch, settings):
    monkeypatch.setattr(settings, "enforce_client_networks", True)
    monkeypatch.setattr(settings, "lan_ui_networks", "192.168.1.0/24")
    monkeypatch.setattr(settings, "api_networks", "192.168.1.0/28")
    monkeypatch.setattr(settings, "trusted_peer_networks", "172.31.250.1/32")
    return settings


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "172.31.250.1", "192.168.1.5"])
def test_api_peers_may_download(policy, ip):
    assert get("/install.sh", ip).status_code == 200
    assert get(f"/download/{WHEEL}", ip).status_code == 200


def test_lan_ui_only_peers_may_not_download(policy):
    assert get("/login", "192.168.1.50").status_code == 200  # UI yes …
    for path in ("/install.sh", f"/download/{WHEEL}"):
        r = get(path, "192.168.1.50")  # … but no tbr package: it only serves API users.
        assert r.status_code == 403 and "API" in r.text


@pytest.mark.parametrize("ip", ["10.0.0.8", "8.8.8.8", "172.17.0.2"])
def test_other_peers_may_not_download(policy, ip):
    assert get("/install.sh", ip).status_code == 403
    assert get(f"/download/{WHEEL}", ip).status_code == 403


# --- install.sh on the agent machine (fake uv, no network) ------------------------------------------

# Like uv: tool environments under $TOOLS/<package>, executables as symlinks in $BIN; `--force` relinks.
FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$UV_LOG"
TOOLS="$HOME/.local/share/uv/tools"
BIN="$HOME/.local/bin"
case "$1 $2" in
  "tool dir") if [ "${3:-}" = "--bin" ]; then echo "$BIN"; else echo "$TOOLS"; fi ;;
  "tool list") for d in "$TOOLS"/*; do [ -f "$d/version" ] && echo "$(basename "$d") v$(cat "$d/version")"; done
    true ;;
  "tool install")
    mkdir -p "$TOOLS/taskbrigade/bin"
    echo "$TBR_FAKE_VERSION" > "$TOOLS/taskbrigade/version"
    for n in tbr tbr-admin tm tm-admin; do
      printf '#!/bin/sh\\necho "tbr %s"\\n' "${TBR_FAKE_LABEL:-$TBR_FAKE_VERSION}" > "$TOOLS/taskbrigade/bin/$n"
      chmod +x "$TOOLS/taskbrigade/bin/$n"
      [ "$n" = "${FAKE_UV_SKIP:-}" ] || ln -sf "$TOOLS/taskbrigade/bin/$n" "$BIN/$n"
    done ;;
esac
"""


@pytest.fixture
def agent_home(tmp_path):
    if not shutil.which("sh"):
        pytest.skip("no sh")
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    uv = home / ".local" / "bin" / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(0o755)
    return home


def old_installation(home: Path) -> Path:
    """uv tool `taskmeister` 0.4.0 from before the rename, providing tm and tm-admin."""
    env = home / ".local" / "share" / "uv" / "tools" / "taskmeister"
    (env / "bin").mkdir(parents=True)
    (env / "version").write_text("0.4.0\n")
    for name in ("tm", "tm-admin"):
        (env / "bin" / name).write_text('#!/bin/sh\necho "tm 0.4.0"\n')
        (env / "bin" / name).chmod(0o755)
        (home / ".local" / "bin" / name).symlink_to(env / "bin" / name)
    return env


def run_install(script: str, home, **extra):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "UV_LOG": str(home / "uv.log"), "TBR_FAKE_VERSION": VERSION,
           **extra}
    r = subprocess.run(["sh"], input=script, text=True, capture_output=True, env=env)
    log = (home / "uv.log").read_text() if (home / "uv.log").exists() else ""
    return r, log


def test_install_script_installs_from_server_and_is_idempotent(agent_home):
    script = distribution.install_script("https://taskbrigade.example.ts.net/", VERSION)
    r, log = run_install(script, agent_home)
    assert r.returncode == 0, r.stderr
    assert (f"uv tool install --force --reinstall-package taskbrigade --python 3.12 "
            f"https://taskbrigade.example.ts.net/download/{WHEEL}" in log)
    assert f"tbr {VERSION}" in r.stdout and "tbr whoami" in r.stdout
    assert "is not on your PATH yet" in r.stdout  # PATH=/usr/bin:/bin lacks ~/.local/bin.

    (agent_home / "uv.log").unlink()
    r, log = run_install(script, agent_home)
    assert r.returncode == 0 and "already installed" in r.stdout
    assert "tool install" not in log


def test_install_script_replaces_the_old_package_without_a_gap(agent_home):
    """tm keeps existing throughout: install --force relinks it, then only the old environment is deleted."""
    legacy = old_installation(agent_home)
    r, log = run_install(distribution.install_script("http://testserver", VERSION), agent_home)
    assert r.returncode == 0, r.stderr
    assert "uninstall" not in log and "Removed the old package taskmeister" in r.stdout
    assert not legacy.exists()
    bin_dir = agent_home / ".local" / "bin"
    for name in ("tbr", "tbr-admin", "tm", "tm-admin"):
        assert "/tools/taskbrigade/" in os.readlink(bin_dir / name)
    assert subprocess.run([bin_dir / "tm"], capture_output=True, text=True).stdout.strip() == f"tbr {VERSION}"


def test_install_script_keeps_the_old_package_while_it_is_linked(agent_home):
    legacy = old_installation(agent_home)
    r, _out = run_install(distribution.install_script("http://testserver", VERSION), agent_home,
                          FAKE_UV_SKIP="tm-admin")
    assert r.returncode == 0, r.stderr
    assert legacy.exists() and "still provides tm-admin" in r.stdout


def test_install_script_quiet_about_path_when_tbr_is_found(agent_home):
    script = distribution.install_script("http://testserver", VERSION)
    env = {"HOME": str(agent_home), "PATH": f"{agent_home}/.local/bin:/usr/bin:/bin",
           "UV_LOG": str(agent_home / "uv.log"), "TBR_FAKE_VERSION": VERSION}
    r = subprocess.run(["sh"], input=script, text=True, capture_output=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "PATH" not in r.stdout


def test_install_script_warns_about_another_tbr_first_on_path(agent_home, tmp_path):
    other = tmp_path / "cargo" / "bin"  # e.g. the ebook reader tbr from crates.io
    other.mkdir(parents=True)
    (other / "tbr").write_text("#!/bin/sh\n")
    (other / "tbr").chmod(0o755)
    script = distribution.install_script("http://testserver", VERSION)
    env = {"HOME": str(agent_home), "PATH": f"{other}:{agent_home}/.local/bin:/usr/bin:/bin",
           "UV_LOG": str(agent_home / "uv.log"), "TBR_FAKE_VERSION": VERSION}
    r = subprocess.run(["sh"], input=script, text=True, capture_output=True, env=env)
    assert r.returncode == 0, r.stderr
    assert f"another tbr comes first on PATH: {other}/tbr" in r.stdout and "crates.io" in r.stdout


@pytest.mark.parametrize("var", ["TBR_URL", "TM_URL"])  # TM_URL: name before TaskBrigade 0.8, still honoured
def test_install_script_honours_url_override(agent_home, var):
    script = distribution.install_script("http://testserver", VERSION)
    env_script = f'{var}="https://taskbrigade.example.ts.net"\nexport {var}\n' + script.split("\n", 1)[1]
    r, log = run_install(env_script, agent_home)
    assert r.returncode == 0, r.stderr
    assert f"https://taskbrigade.example.ts.net/download/{WHEEL}" in log


# --- Version comparison and warning -----------------------------------------------------------

@pytest.mark.parametrize("local,server,older", [
    ("0.4.0", "0.5.0", True), ("0.4.0", "0.4.1", True), ("0.9.0", "0.10.0", True), ("0.4", "0.4.0", False),
    ("0.5.0", "0.4.9", False), ("0.5.0", "0.5.0", False), ("0.5.0rc1", "0.5.0", False),
    (None, "0.5.0", False), ("0.4.0", None, False), ("0.4.0", "unbekannt", False),
])
def test_is_older(local, server, older):
    assert client_mod.is_older(local, server) is older


def _client(handler, monkeypatch, local="0.4.0"):
    monkeypatch.setattr(client_mod, "local_version", lambda: local)
    monkeypatch.setattr(client_mod, "_warned", False)
    c = client_mod.Client(client_mod.Config("http://tm.example.internal", "tm_x"))
    c._http = httpx.Client(base_url="http://tm.example.internal/api/v1", transport=httpx.MockTransport(handler))
    return c


def test_client_warns_once_per_call_when_older(monkeypatch, capsys):
    c = _client(lambda req: httpx.Response(200, json={"items": []}, headers={VERSION_HEADER: "0.5.0"}), monkeypatch)
    c.get("/tasks")
    c.get("/tasks")
    err = capsys.readouterr()
    assert err.err.count("tbr self-update") == 1 and "0.4.0" in err.err and "0.5.0" in err.err
    assert err.out == ""  # stdout belongs to git credentials and the MCP protocol.


def test_client_silent_when_current_or_unknown(monkeypatch, capsys):
    for server in ("0.4.0", None):
        headers = {VERSION_HEADER: server} if server else {}
        _client(lambda req: httpx.Response(200, json={}, headers=headers), monkeypatch).get("/agents/me")
    assert capsys.readouterr().err == ""


def test_mcp_list_tasks_shows_the_warning(monkeypatch, tmp_path):
    c = _client(lambda req: httpx.Response(200, json={"items": []}, headers={VERSION_HEADER: "0.5.0"}), monkeypatch)
    out = TaskBrigadeMCP(c, tmp_path).list_tasks()
    assert out.startswith("Warning: tbr 0.4.0 is older") and "tbr self-update" in out and "No tasks for you." in out
    c = _client(lambda req: httpx.Response(200, json={"items": []}, headers={VERSION_HEADER: "0.4.0"}), monkeypatch)
    assert TaskBrigadeMCP(c, tmp_path).list_tasks() == "No tasks for you."


# --- tbr self-update -----------------------------------------------------------------------------

@pytest.fixture
def identity(tmp_path, monkeypatch):
    (tmp_path / ".taskbrigade.toml").write_text('url = "https://taskbrigade.example.ts.net/"\ntoken = "tm_x"\n')
    monkeypatch.chdir(tmp_path)
    for var in ("TBR_URL", "TBR_TOKEN", "TBR_CONFIG", "TM_URL", "TM_TOKEN", "TM_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(httpx, "get",
                        lambda url, **kw: httpx.Response(503, json={"version": "0.5.0", "db": "unavailable"},
                                                         headers={VERSION_HEADER: "0.5.0"},
                                                         request=httpx.Request("GET", url)))
    runs = []
    monkeypatch.setattr(tm.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(tm, "find_uv", lambda: "/home/agent/.local/bin/uv")
    tools, bin_dir = tmp_path / "tools", tmp_path / "bin"
    tools.mkdir(), bin_dir.mkdir()
    monkeypatch.setattr(selfupdate, "_uv_dir", lambda uv, *args: bin_dir if args == ("--bin",) else tools)
    return runs


def installed(monkeypatch, package: str, version: str):
    monkeypatch.setattr(client_mod, "installed_distribution", lambda: (package, version))


INSTALL = ["/home/agent/.local/bin/uv", "tool", "install", "--force", "--reinstall-package", "taskbrigade",
           "--python", "3.12",
           "https://taskbrigade.example.ts.net/download/taskbrigade-0.5.0-py3-none-any.whl"]


def test_self_update_installs_the_server_version(identity, monkeypatch):
    installed(monkeypatch, "taskbrigade", "0.4.0")
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 0, r.output
    assert identity == [INSTALL]
    assert "tbr 0.5.0 installed." in r.output


def test_self_update_noop_when_current(identity, monkeypatch):
    installed(monkeypatch, "taskbrigade", "0.5.0")
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 0 and "up to date" in r.output and identity == []
    assert CliRunner().invoke(tm.app, ["self-update", "--force"]).exit_code == 0 and len(identity) == 1


def legacy_env(tmp_path: Path, linked: tuple[str, ...]) -> Path:
    """Old uv tool `taskmeister`; `linked` commands in the bin directory still point into it."""
    env = tmp_path / "tools" / "taskmeister"
    (env / "bin").mkdir(parents=True)
    new = tmp_path / "tools" / "taskbrigade" / "bin"
    new.mkdir(parents=True)
    for name in selfupdate.COMMANDS:
        (tmp_path / "bin" / name).symlink_to((env if name in linked else new.parent) / "bin" / name)
    return env


def test_self_update_replaces_the_old_package_even_at_the_same_version(identity, monkeypatch, tmp_path):
    """`tm self-update` of an old installation got the bridge wheel (package taskmeister, same version)."""
    installed(monkeypatch, "taskmeister", "0.5.0")
    env = legacy_env(tmp_path, linked=())
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 0, r.output
    assert identity == [INSTALL] and not env.exists()
    assert "Old package taskmeister removed" in r.output and "tbr 0.5.0 installed." in r.output


def test_self_update_keeps_the_old_package_while_a_command_points_into_it(identity, monkeypatch, tmp_path):
    installed(monkeypatch, "taskbrigade", "0.5.0")
    env = legacy_env(tmp_path, linked=("tm-admin",))
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 0, r.output
    assert identity == [] and env.exists() and "still provides tm-admin" in r.output


def test_self_update_without_uv_points_to_install_sh(identity, monkeypatch):
    installed(monkeypatch, "taskbrigade", "0.4.0")
    monkeypatch.setattr(tm, "find_uv", lambda: None)
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 2 and "curl -fsSL https://taskbrigade.example.ts.net/install.sh | sh" in r.output


def test_self_update_reports_uv_failure(identity, monkeypatch):
    installed(monkeypatch, "taskbrigade", "0.4.0")
    monkeypatch.setattr(tm.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3))
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 1 and "exit code 3" in r.output


def test_wheel_names_match_between_server_and_cli():
    assert client_mod.wheel_name("1.2.3") == distribution.wheel_name("1.2.3")
    assert sys.modules["cli.client"].VERSION_HEADER == VERSION_HEADER


@pytest.mark.db
def test_install_sh_uses_the_configured_app_url(dist, ready_db):
    from app.services import settings_store

    with ready_db.session() as s:
        settings_store.update(s, {"app_url": "https://taskbrigade.example.ts.net/"}, None)
        s.commit()
    assert 'TBR_URL="${TBR_URL:-${TM_URL:-https://taskbrigade.example.ts.net}}"' in get("/install.sh").text
