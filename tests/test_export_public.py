"""The public export must discard source history and fail closed on scan findings."""

import fnmatch
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/release/export-public.sh"
# Built at runtime: this file is exported too, and the export scan must not find these values in it.
PRIVATE_IP = ".".join(("10", "7", "8", "9"))
PRIVATE_HOST = "private" + ".local"


def run(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    assert run("git", "init", "-q", str(repo)).returncode == 0
    (repo / "README.md").write_text("Public content\n")
    assert run("git", "add", "README.md", cwd=repo).returncode == 0
    assert run(
        "git", "-c", "user.name=Source Person", "-c", "user.email=source@example.com",
        "commit", "-q", "-m", "Private source commit", cwd=repo,
    ).returncode == 0
    return repo


@pytest.fixture
def environment(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake = tools / "gitleaks"
    fake.write_text("#!/bin/sh\n[ -n \"$FAKE_GITLEAKS_ARGS\" ] && printf '%s\\n' \"$@\" > \"$FAKE_GITLEAKS_ARGS\"\n"
                    "exit \"${FAKE_GITLEAKS_EXIT:-0}\"\n")
    fake.chmod(0o755)
    return {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}


def export(source, tmp_path, environment, *extra):
    destination = tmp_path / "public"
    result = run(
        "bash", str(SCRIPT), "--source", str(source), "--output", str(destination),
        "--author-name", "Release Bot", "--author-email", "release@example.com",
        *extra, env=environment,
    )
    return result, destination


def test_creates_single_commit_with_explicit_identity(source, tmp_path, environment):
    result, destination = export(source, tmp_path, environment)
    assert result.returncode == 0, result.stderr
    assert (destination / "README.md").read_text() == "Public content\n"
    log = run("git", "log", "--format=%an <%ae>|%cn <%ce>|%s", cwd=destination)
    assert log.stdout.strip() == (
        "Release Bot <release@example.com>|Release Bot <release@example.com>|Initial public release"
    )
    assert len(run("git", "rev-list", "--all", cwd=destination).stdout.splitlines()) == 1


@pytest.mark.parametrize("content", [f"server={PRIVATE_IP}\n", f"host={PRIVATE_HOST}\n"])
def test_rejects_internal_patterns(source, tmp_path, environment, content):
    (source / "README.md").write_text(content)
    assert run("git", "add", "README.md", cwd=source).returncode == 0
    assert run(
        "git", "-c", "user.name=Source Person", "-c", "user.email=source@example.com",
        "commit", "-q", "-m", "Add test content", cwd=source,
    ).returncode == 0
    result, destination = export(source, tmp_path, environment)
    assert result.returncode != 0
    assert not destination.exists()
    assert content.strip() not in result.stderr


def test_rejects_gitleaks_findings(source, tmp_path, environment):
    environment["FAKE_GITLEAKS_EXIT"] = "1"
    result, destination = export(source, tmp_path, environment)
    assert result.returncode != 0
    assert not destination.exists()


def test_rejects_internal_pattern_in_binary_asset(source, tmp_path, environment):
    (source / "asset.bin").write_bytes(b"\0host=" + PRIVATE_HOST.encode() + b"\0")
    assert run("git", "add", "asset.bin", cwd=source).returncode == 0
    assert run(
        "git", "-c", "user.name=Source Person", "-c", "user.email=source@example.com",
        "commit", "-q", "-m", "Add binary asset", cwd=source,
    ).returncode == 0
    result, destination = export(source, tmp_path, environment)
    assert result.returncode != 0
    assert "internal hostname" in result.stderr
    assert PRIVATE_HOST not in result.stderr
    assert not destination.exists()


def test_rejects_custom_pattern(source, tmp_path, environment):
    (source / "README.md").write_text("private marker: DEMO_SECRET_MARKER\n")
    assert run("git", "add", "README.md", cwd=source).returncode == 0
    assert run(
        "git", "-c", "user.name=Source Person", "-c", "user.email=source@example.com",
        "commit", "-q", "-m", "Add test content", cwd=source,
    ).returncode == 0
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("DEMO_SECRET_MARKER\n")
    result, destination = export(source, tmp_path, environment, "--patterns-file", str(patterns))
    assert result.returncode != 0
    assert "custom pattern" in result.stderr
    assert "DEMO_SECRET_MARKER" not in result.stderr
    assert not destination.exists()


def test_rejects_uncommitted_tracked_changes(source, tmp_path, environment):
    (source / "README.md").write_text("Changed\n")
    result, destination = export(source, tmp_path, environment)
    assert result.returncode != 0
    assert not destination.exists()


def commit_files(repo, files):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    assert run("git", "add", "--all", cwd=repo).returncode == 0
    assert run(
        "git", "-c", "user.name=Source Person", "-c", "user.email=source@example.com",
        "commit", "-q", "-m", "Add internal material", cwd=repo,
    ).returncode == 0


def test_excludes_internal_material(source, tmp_path, environment):
    # Internal notes may contain markers the scan would reject; excluded files must not even be scanned.
    commit_files(source, {
        "research/naming.md": f"host={PRIVATE_HOST}\n".encode(),
        "docs/naming/report.md": f"server={PRIVATE_IP}\n".encode(),
        "docs/guide.md": b"Public guide\n",
        "media/mockup.zip": b"PK\0\0",
        "mediaassets/ChatGPT-Bild 8. Okt. 2026, 22_52_12.png": b"\x89PNG",
        "mediaassets/logo.png": b"\x89PNG",
        "tasks/DEMO-0001/mockup.png": b"\x89PNG",
    })
    result, destination = export(source, tmp_path, environment)
    assert result.returncode == 0, result.stderr
    files = {p.relative_to(destination).as_posix() for p in destination.rglob("*")
             if p.is_file() and ".git" not in p.relative_to(destination).parts}
    assert files == {"README.md", "docs/guide.md", "mediaassets/logo.png"}
    tracked = run("git", "ls-files", cwd=destination).stdout.split("\n")
    assert not any(name.startswith(("research", "docs/naming", "media/", "tasks/")) for name in tracked)


def excluded_globs():
    text = SCRIPT.read_text()
    block = text[text.index("EXCLUDED_PATHS=(") + len("EXCLUDED_PATHS=("):]
    block = block[:block.index(")")]
    return [line.strip().strip("'") for line in block.splitlines() if line.strip()]


def test_exclusion_list_covers_internal_material_of_this_repository():
    globs = excluded_globs()
    assert globs and all(not any(c.isspace() for c in g) for g in globs)
    root = SCRIPT.parents[2]
    tracked = run("git", "ls-files", cwd=root).stdout.splitlines()
    internal = [name for name in tracked
                if name.startswith(("research/", "Marketing/", "Produktvideo/", "docs/naming/", "docs/ui-redesign/", "docs/konzept-email.md", "media/", "tasks/"))
                or name.startswith("mediaassets/ChatGPT-Bild")]

    def excluded(name):
        return any(fnmatch.fnmatchcase(name, g) or name.startswith(g + "/") for g in globs)

    assert all(excluded(name) for name in internal), internal
    # The brand sources and generated assets stay public.
    assert not excluded("mediaassets/Taskbrigade_logo.png") and not excluded("mediaassets/build_brand.py")
    assert not excluded("docs/release-checklist.md")


@pytest.mark.parametrize("content", [
    "bridge 172.31.250.1, smoke 172.31.251.0/24, LAN 192.168.1.20, peer 10.0.0.5, mask 255.255.255.0\n",
    "gitea.example.internal:3001 taskbrigade.example.ts.net .claude/settings.local.json\n",
])
def test_accepts_documented_examples(source, tmp_path, environment, content):
    commit_files(source, {"docs/example.md": content.encode()})
    result, _ = export(source, tmp_path, environment)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("content", [".".join(("192", "168", "2", "5")), "gitea.myexample" + ".internal"])
def test_rejects_values_next_to_documented_examples(source, tmp_path, environment, content):
    commit_files(source, {"docs/example.md": content.encode()})
    result, destination = export(source, tmp_path, environment)
    assert result.returncode != 0 and not destination.exists()


def test_gitleaks_scans_the_staged_tree_as_a_positional_path(source, tmp_path, environment):
    # gitleaks 8.19+ `dir` takes the path as an argument; `--source` is rejected as an unknown flag.
    args_file = tmp_path / "gitleaks-args"
    environment["FAKE_GITLEAKS_ARGS"] = str(args_file)
    result, _ = export(source, tmp_path, environment)
    assert result.returncode == 0, result.stderr
    args = args_file.read_text().splitlines()
    assert args[0] == "dir" and "--source" not in args and "--redact" in args
    assert Path(args[-1]).name.startswith(".public-export.")
