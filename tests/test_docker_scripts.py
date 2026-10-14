"""Docker Compose backup/restore/update scripts without a Docker daemon (a fake `docker` on PATH)."""
import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "deploy/docker"
SCRIPTS = ("common.sh", "backup.sh", "restore.sh", "update.sh")

# Shape of `docker compose config --format json` (two-space indent); a network and a service share a name.
CONFIG = {
    "name": "taskbrigade",
    "networks": {"taskbrigade": {"name": "taskbrigade", "image": "not-a-service"}},
    "services": {
        "gitea": {"image": "gitea/gitea:1.24", "profiles": ["gitea"]},
        "mariadb": {"image": "mariadb:11.4"},
        "taskbrigade": {"build": {"context": "."}, "image": "taskbrigade:local", "pull_policy": "always"},
    },
}


def _fake_docker(tmp_path: Path, config: dict) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "config.json").write_text(json.dumps(config, indent=2))
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\ncat "{tmp_path / "config.json"}"\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _source(snippet: str, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    script = f"set -euo pipefail\nsource deploy/docker/common.sh\n{snippet}"
    return subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True, env=env)


def test_scripts_are_valid_bash():
    for name in SCRIPTS:
        result = subprocess.run(["bash", "-n", str(DOCKER_DIR / name)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_scripts_are_executable_and_common_is_sourced():
    for name in ("backup.sh", "restore.sh", "update.sh"):
        path = DOCKER_DIR / name
        assert os.access(path, os.X_OK), name
        assert 'source "$(dirname "$0")/common.sh"' in path.read_text()
    result = subprocess.run(["bash", str(DOCKER_DIR / "common.sh")], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Source" in result.stderr


def test_service_field_reads_the_right_service(tmp_path):
    bin_dir = _fake_docker(tmp_path, CONFIG)
    result = _source("tm_service_field taskbrigade image; tm_service_field gitea image; "
                     "tm_service_field mariadb image; tm_pull_policy", bin_dir)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["taskbrigade:local", "gitea/gitea:1.24", "mariadb:11.4", "always"]


def test_pull_policy_defaults_to_build(tmp_path):
    config = json.loads(json.dumps(CONFIG))
    del config["services"]["taskbrigade"]["pull_policy"]
    result = _source("tm_pull_policy", _fake_docker(tmp_path, config))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "build"


def test_restore_requires_an_archive():
    result = subprocess.run(["bash", str(DOCKER_DIR / "restore.sh")], capture_output=True, text=True)
    assert result.returncode == 1
    assert "usage" in result.stderr
    result = subprocess.run(["bash", str(DOCKER_DIR / "restore.sh"), "--yes", "/nonexistent.tar"],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_secrets_stay_off_command_lines():
    # The root password is read from the container environment, never passed as -p<password>.
    for name in ("backup.sh", "restore.sh"):
        text = (DOCKER_DIR / name).read_text()
        assert 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD"' in text
        assert "-p\"$MARIADB_ROOT_PASSWORD\"" not in text


def test_volume_names_match_compose():
    # backup.sh/restore.sh address the volumes by their fixed names from compose.yaml.
    compose = (ROOT / "compose.yaml").read_text()
    common = (DOCKER_DIR / "common.sh").read_text()
    for volume in ("taskbrigade-state", "taskbrigade-gitea"):
        assert f"name: {volume}" in compose
        assert f"={volume}" in common


def _echo_docker(tmp_path: Path, volume_exists: bool) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\n[ "$1" = volume ] && exit {0 if volume_exists else 1}\necho "$@"\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_dc_adds_gitea_profile_once_its_volume_exists(tmp_path):
    with_volume = _source("dc ps", _echo_docker(tmp_path / "a", volume_exists=True))
    without_volume = _source("dc ps", _echo_docker(tmp_path / "b", volume_exists=False))
    assert with_volume.stdout.split()[-3:] == ["--profile", "gitea", "ps"]
    assert "--profile" not in without_volume.stdout
