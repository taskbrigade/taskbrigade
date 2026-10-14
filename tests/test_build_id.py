"""Build identifier next to the version: changes rolled out without a version bump still reach the agents."""
import os
import subprocess
import time
import zipfile

import httpx
import pytest
from typer.testing import CliRunner

from app.main import BUILD_HEADER, VERSION_HEADER
from app.services import distribution
from cli import build, tm
from cli import client as client_mod
from tests.test_distribution import INSTALL, VERSION, WHEEL, _client, get, installed, run_install
from tests.test_distribution import agent_home, dist, identity  # noqa: F401

RECORD = [
    "app/__init__.py,sha256=AAA,0",
    "cli/tm.py,sha256=BBB,100",
    "cli/agent_files/hermes/taskbrigade/SKILL.md,sha256=CCC,50",
    "taskbrigade-9.8.7.dist-info/METADATA,sha256=DDD,10",
    "taskbrigade-9.8.7.dist-info/RECORD,,",
]


def write_wheel(path, record=RECORD):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("cli/tm.py", "x")
        z.writestr("taskbrigade-9.8.7.dist-info/RECORD", "\n".join(record) + "\n")
    return path


# --- Identifier ----------------------------------------------------------------------------------

def test_identifier_covers_package_files_only():
    installed_record = [  # What uv writes on install: same package lines plus scripts and its own dist-info files.
        "../../../bin/tbr,sha256=EEE,474",
        "taskbrigade-9.8.7.dist-info/INSTALLER,sha256=FFF,2",
        "taskbrigade-9.8.7.dist-info/direct_url.json,sha256=GGG,212",
        "cli/__pycache__/tm.cpython-312.pyc,,",
        *reversed(RECORD),
    ]
    ident = build.from_record(RECORD)
    assert len(ident) == build.LENGTH and build.from_record(installed_record) == ident
    assert build.from_record([*RECORD[:1], "cli/tm.py,sha256=XXX,100", *RECORD[2:]]) != ident
    assert build.from_record(RECORD[3:]) == "" and build.from_record([]) == ""


def test_label():
    assert build.label("0.6.0", "d6f1018a2b") == "0.6.0+d6f1018a2b"
    assert build.label("0.6.0", "") == "0.6.0" and build.label(None, "x") == "?"


def test_editable_install_has_no_identifier(tmp_path, monkeypatch):
    class Dist:
        def __init__(self, files):
            self.files = files

        def read_text(self, name):
            return self.files.get(name)

    import importlib.metadata

    record = "\n".join(RECORD)
    for files, expected in [
        ({"RECORD": record}, build.from_record(RECORD)),
        ({"RECORD": record, "direct_url.json": '{"url": "file:///src", "dir_info": {"editable": true}}'}, ""),
        ({"RECORD": record + "\n_editable_impl_taskbrigade.pth,sha256=HHH,151"}, ""),
    ]:
        monkeypatch.setattr(importlib.metadata, "distribution", lambda name, f=files: Dist(f))
        assert build.installed("taskbrigade") == expected


# --- Server ----------------------------------------------------------------------------------------

def test_server_reports_the_build_of_its_wheel(dist):
    write_wheel(dist / WHEEL)
    ident = build.from_record(RECORD)
    r = get("/healthz")
    assert r.headers[VERSION_HEADER] == VERSION and r.headers[BUILD_HEADER] == ident
    assert r.json()["build"] == ident
    assert distribution.version_label() == f"{VERSION}+{ident}"
    assert f'TBR_BUILD="{ident}"' in get("/install.sh").text
    assert get(f"/download/{WHEEL}").headers["cache-control"] == "no-cache"


def test_server_without_a_readable_wheel_reports_no_build(dist):
    r = get("/healthz")  # The fixture's wheel is not a zip file.
    assert BUILD_HEADER not in r.headers and r.json()["build"] is None
    assert distribution.version_label() == VERSION


def test_a_rebuilt_wheel_changes_the_build(dist):
    write_wheel(dist / WHEEL)
    before = distribution.build_id()
    write_wheel(dist / WHEEL, [*RECORD[:1], "cli/tm.py,sha256=NEW,101", *RECORD[2:]])
    os.utime(dist / WHEEL, (time.time() + 5, time.time() + 5))
    assert distribution.build_id() not in ("", before)


def test_the_newest_wheel_of_the_version_wins(dist, tmp_path):
    """deploy/update.sh builds into <source>/dist; an old lazily built wheel in TBR_DIST_DIR must not shadow it."""
    (tmp_path / "src" / "dist").mkdir(parents=True)
    fresh = tmp_path / "src" / "dist" / WHEEL
    fresh.write_bytes(b"fresh")
    os.utime(dist / WHEEL, (time.time() - 60, time.time() - 60))
    assert distribution.find_wheel() == fresh and get(f"/download/{WHEEL}").content == b"fresh"


# --- Version warning ---------------------------------------------------------------------------------

@pytest.mark.parametrize("local,server,local_build,server_build,update", [
    ("0.6.0", "0.6.0", "aaa", "bbb", True),  # Same version, other build.
    ("0.6.0", "0.6.0", "aaa", "aaa", False),  # Same build.
    ("0.6.0", "0.6.0", "", "bbb", False),  # Source checkout: build unknown.
    ("0.6.0", "0.6.0", "aaa", None, False),  # Server before 0.6 or without wheel.
    ("0.5.0", "0.6.0", "aaa", "aaa", True),  # Older version.
    ("0.7.0", "0.6.0", "aaa", "bbb", False),  # Newer CLI: no downgrade advice.
])
def test_needs_update(local, server, local_build, server_build, update):
    assert client_mod.needs_update(local, server, local_build, server_build) is update


def test_client_warns_about_another_build(monkeypatch, capsys):
    monkeypatch.setattr(client_mod, "local_build", lambda: "aaaaaaaaaa")
    headers = {VERSION_HEADER: "0.6.0", BUILD_HEADER: "bbbbbbbbbb"}
    c = _client(lambda req: httpx.Response(200, json={}, headers=headers), monkeypatch, local="0.6.0")
    c.get("/agents/me")
    c.get("/agents/me")
    err = capsys.readouterr().err
    assert err.count("tbr self-update") == 1
    assert "0.6.0+aaaaaaaaaa is another build" in err and "(0.6.0+bbbbbbbbbb)" in err


def test_client_silent_for_the_same_build(monkeypatch, capsys):
    monkeypatch.setattr(client_mod, "local_build", lambda: "aaaaaaaaaa")
    for server_build in ("aaaaaaaaaa", None):
        headers = {VERSION_HEADER: "0.6.0", **({BUILD_HEADER: server_build} if server_build else {})}
        _client(lambda req: httpx.Response(200, json={}, headers=headers), monkeypatch, local="0.6.0").get("/x")
    assert capsys.readouterr().err == ""


# --- tbr self-update / tbr version ---------------------------------------------------------------------

def serve(monkeypatch, version="0.5.0", server_build="bbbbbbbbbb"):
    headers = {VERSION_HEADER: version, **({BUILD_HEADER: server_build} if server_build else {})}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        200, json={"version": version, "build": server_build, "db": "ok"}, headers=headers,
        request=httpx.Request("GET", url)))


def local_build(monkeypatch, value):
    monkeypatch.setattr(build, "installed", lambda package: value)
    monkeypatch.setattr(client_mod, "local_build", lambda: value)


def test_self_update_installs_another_build_of_the_same_version(identity, monkeypatch):
    installed(monkeypatch, "taskbrigade", "0.5.0")
    local_build(monkeypatch, "aaaaaaaaaa")
    serve(monkeypatch)
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 0, r.output
    assert identity == [INSTALL]
    assert "Installing tbr 0.5.0+bbbbbbbbbb (installed: 0.5.0+aaaaaaaaaa)" in r.output
    assert "tbr 0.5.0+bbbbbbbbbb installed." in r.output


def test_self_update_noop_for_the_same_build(identity, monkeypatch):
    installed(monkeypatch, "taskbrigade", "0.5.0")
    local_build(monkeypatch, "bbbbbbbbbb")
    serve(monkeypatch)
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 0 and identity == []
    assert "tbr 0.5.0+bbbbbbbbbb is up to date" in r.output


@pytest.mark.parametrize("mine,theirs", [("", "bbbbbbbbbb"), ("aaaaaaaaaa", None)])
def test_self_update_noop_when_a_build_is_unknown(identity, monkeypatch, mine, theirs):
    """Source checkout (no own build) or a server without build information: the version decides as before."""
    installed(monkeypatch, "taskbrigade", "0.5.0")
    local_build(monkeypatch, mine)
    serve(monkeypatch, server_build=theirs)
    r = CliRunner().invoke(tm.app, ["self-update"])
    assert r.exit_code == 0 and identity == [] and "up to date" in r.output


def test_version_shows_version_and_build(identity, monkeypatch):
    installed(monkeypatch, "taskbrigade", "0.5.0")
    monkeypatch.setattr(client_mod, "local_version", lambda: "0.5.0")
    local_build(monkeypatch, "aaaaaaaaaa")
    serve(monkeypatch)
    r = CliRunner().invoke(tm.app, ["version"])
    assert r.exit_code == 0, r.output
    assert "tbr 0.5.0+aaaaaaaaaa" in r.output
    assert "Server 0.5.0+bbbbbbbbbb (https://taskbrigade.example.ts.net)" in r.output
    assert "another build" in r.output


# --- install.sh ----------------------------------------------------------------------------------------

def test_install_script_reinstalls_another_build_of_the_same_version(agent_home):
    script = distribution.install_script("http://testserver", VERSION, "bbbbbbbbbb")
    r, log = run_install(script, agent_home, TBR_FAKE_LABEL=f"{VERSION}+aaaaaaaaaa")
    assert r.returncode == 0, r.stderr
    assert "tool install" in log
    (agent_home / "uv.log").unlink()
    r, log = run_install(script, agent_home, TBR_FAKE_LABEL=f"{VERSION}+aaaaaaaaaa")  # Still the old build.
    assert "tool install" in log and f"Installing tbr {VERSION}+bbbbbbbbbb" in r.stdout
    (agent_home / "uv.log").unlink()
    r, log = run_install(script, agent_home, TBR_FAKE_LABEL=f"{VERSION}+bbbbbbbbbb")
    assert r.returncode == 0, r.stderr
    (agent_home / "uv.log").unlink()
    r, log = run_install(script, agent_home, TBR_FAKE_LABEL=f"{VERSION}+bbbbbbbbbb")
    assert "tool install" not in log and "already installed" in r.stdout
    subprocess.run(["sh", "-n"], input=script, text=True, check=True)
