"""Installation script prerequisite checks without root or a real installation."""
import ipaddress
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("deploy/prereq.sh", "deploy/install.sh", "deploy/update.sh", "deploy/backup.sh",
           "deploy/sftp/check-inbox.sh")
# Internal infrastructure must not appear in deploy files or public instructions: only documented example
# addresses and hosts are allowed. Organization-specific markers stay in a local file outside the repository
# (TBR_PRIVATE_PATTERNS_FILE, one regular expression per line, as for export-public.sh --patterns-file).
EXAMPLE_NETWORKS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/32", "127.0.0.0/8", "192.168.1.0/24", "10.0.0.0/24", "172.17.0.0/16",
    "172.31.250.0/24", "172.31.251.0/24", "172.31.252.0/24", "172.31.253.0/24", "255.0.0.0/8",
)]
IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
INTERNAL_HOST = re.compile(r"\b[a-z0-9][a-z0-9.-]*\.(?:local|internal|ts\.net)\b", re.I)
# Every Settings variable from app/config.py, plus script and unit operation variables.
ENV_KEYS = (
    "TBR_STATE_DIR", "TBR_DB_HOST", "TBR_DB_PORT", "TBR_DB_NAME", "TBR_DB_USER", "DATABASE_URL",
    "SECRET_KEY", "BOOTSTRAP_ADMIN_PASSWORD_HASH", "BOOTSTRAP_ADMIN_ACTOR", "DEFAULT_PROJECT_OWNER",
    "UI_ALLOWED_LOGINS",
    "TBR_BIND_HOST", "LAN_UI_NETWORKS", "ENFORCE_CLIENT_NETWORKS", "TZ_DISPLAY",
    "GITEA_URL", "GITEA_TOKEN", "GITEA_DEFAULT_OWNER",
    "NOTIFY_TELEGRAM_BOT_TOKEN", "NOTIFY_TELEGRAM_CHAT_ID", "SCHEDULER_ENABLED",
    "TBR_MAX_ATTACHMENT_MB", "TBR_BACKUP_DIR", "TBR_BACKUP_KEEP_DAYS", "TBR_ENV_FILE",
    "TBR_DATA_ZONES", "TBR_DATA_SFTP", "TBR_RUNNER_VENV", "TBR_SFTP_DIR", "TBR_BACKUP_DATA",
)


def _bash(script: str, prepend_path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if prepend_path is not None:
        env["PATH"] = prepend_path + os.pathsep + env.get("PATH", "")
    return subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True, env=env)


DATA_ZONE_SCRIPTS = ("deploy/prereq.sh", "deploy/install.sh", "deploy/update.sh", "deploy/backup.sh")


def test_data_zone_switch_and_paths(tmp_path):
    """The opt-in switch and the path helpers of the data-zone routines (concept §9)."""
    envdir = tmp_path / "data"
    result = _bash(f"""
set -euo pipefail
source deploy/prereq.sh
if tbr_data_zones_enabled; then echo 'on by default'; exit 1; fi
if TBR_DATA_ZONES=0 tbr_data_zones_enabled; then echo '0 counts as on'; exit 1; fi
[[ "$(tbr_data_dir_path)" == "/srv/taskbrigade-data" ]]
[[ "$(tbr_data_key_path)" == "/etc/taskbrigade/data.key" ]]
[[ "$(TBR_DATA_DIR={envdir} tbr_data_dir_path)" == "{envdir}" ]]
[[ "$(tbr_data_group)" == "tbr-data" ]]
[[ "$(TBR_DATA_GROUP=other tbr_data_group)" == "other" ]]
TBR_DATA_ZONES=1 tbr_data_zones_enabled
# tm_version_ge is reused by the systemd check.
tm_version_ge 255 249
if tm_version_ge 245 249; then echo 'old systemd accepted'; exit 1; fi
echo OK
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_data_zone_checks_are_only_run_when_enabled():
    """install.sh/update.sh guard every data-zone routine behind the switch."""
    for name in ("deploy/install.sh", "deploy/update.sh"):
        text = (ROOT / name).read_text()
        assert "tbr_data_zones_enabled" in text, name
        assert "tm_require_data_zone_host" in text, name
    update = (ROOT / "deploy/update.sh").read_text()
    # The switch from the environment wins over the key stored earlier.
    assert 'tm_env_get "$ETC" TBR_DATA_ZONES' in update


def test_data_zone_polkit_rule_is_narrow():
    rule = (ROOT / "deploy/polkit/60-taskbrigade-runner.rules").read_text()
    assert "org.freedesktop.systemd1.manage-units" in rule
    assert "tbr-runner" in rule
    assert 'indexOf("taskbrigade-job@")' in rule
    assert "polkit.Result.YES" in rule


def test_deploy_scripts_are_valid_bash():
    for name in SCRIPTS:
        result = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_prereq_is_sourced_not_executed():
    result = subprocess.run(["bash", str(ROOT / "deploy/prereq.sh")], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Source" in result.stderr
    for name in ("deploy/install.sh", "deploy/update.sh", "deploy/backup.sh"):
        text = (ROOT / name).read_text()
        assert "source" in text and "prereq.sh" in text
        assert "tm_require_" in text or "tm_check_" in text


def test_version_compare_and_mariadb_banner():
    result = _bash(r"""
set -euo pipefail
source deploy/prereq.sh
tm_version_ge 10.5 10.5
tm_version_ge 10.11.14 10.5
tm_version_ge 11.4.2 10.5
if tm_version_ge 10.4.30 10.5; then echo 'old accepted'; exit 1; fi
if tm_version_ge 10.5.0 10.11; then echo 'minor accepted'; exit 1; fi
v=$(tm_mariadb_version_string "mariadb  Ver 15.1 Distrib 10.11.14-MariaDB, for debian-linux-gnu")
[[ "$v" == "10.11.14" ]]
v=$(tm_mariadb_version_string "mariadb from 10.5.23-MariaDB, client 15.2 for debian-linux-gnu")
[[ "$v" == "10.5.23" ]]
if tm_mariadb_version_string "mysql  Ver 8.0.36 for Linux on x86_64 (MySQL Community Server)"; then
  echo 'mysql accepted'; exit 1
fi
echo OK
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def _fake_client(tmp_path: Path, banner: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("mariadb", "mariadb-dump"):
        path = bindir / name
        path.write_text(f"#!/bin/sh\necho '{banner}'\n")
        path.chmod(0o755)
    return bindir


def test_mysql_client_is_rejected(tmp_path):
    bindir = _fake_client(tmp_path, "mysql  Ver 8.0.36 for Linux on x86_64 (MySQL Community Server)")
    result = _bash("source deploy/prereq.sh; tm_require_mariadb_client", str(bindir))
    assert result.returncode != 0
    assert "not a MariaDB" in result.stderr


def test_old_mariadb_client_is_rejected(tmp_path):
    bindir = _fake_client(tmp_path, "mariadb  Ver 15.1 Distrib 10.4.30-MariaDB, for debian-linux-gnu")
    result = _bash("source deploy/prereq.sh; tm_require_mariadb_client", str(bindir))
    assert result.returncode != 0
    assert "older than" in result.stderr


def test_mariadb_10_11_client_passes(tmp_path):
    bindir = _fake_client(tmp_path, "mariadb  Ver 15.1 Distrib 10.11.14-MariaDB, for debian-linux-gnu")
    result = _bash("source deploy/prereq.sh; tm_require_mariadb_client", str(bindir))
    assert result.returncode == 0, result.stderr
    assert "10.11.14 OK" in result.stderr


def test_gitea_placeholder_warns_and_concrete_url_must_answer():
    warned = _bash('source deploy/prereq.sh; tm_check_gitea "http://gitea.example.internal:3001" warn')
    assert warned.returncode == 0, warned.stderr
    assert "not configured" in warned.stderr
    required = _bash('source deploy/prereq.sh; tm_check_gitea "http://gitea.example.org:3001" required')
    assert required.returncode != 0
    assert "Gitea is required" in required.stderr
    down = _bash('source deploy/prereq.sh; tm_check_gitea "http://127.0.0.1:1" warn')
    assert down.returncode != 0
    assert "Gitea check failed" in down.stderr
    reported = _bash('source deploy/prereq.sh; tm_check_gitea "http://127.0.0.1:1" report')
    assert reported.returncode == 0, reported.stderr
    assert "warning:" in reported.stderr


def test_env_set_replaces_and_ignores_empty(tmp_path):
    envfile = tmp_path / "taskbrigade.env"
    envfile.write_text("GITEA_URL=http://gitea.example.internal:3001\nSECRET_KEY=\n")
    result = _bash(f"""
set -euo pipefail
source deploy/prereq.sh
TBR_PY=$(command -v python3)
export TBR_PY
tm_env_set {envfile} GITEA_URL http://127.0.0.1:3001
tm_env_set {envfile} TBR_DB_PORT ""
got=$(tm_env_get {envfile} GITEA_URL)
[[ "$got" == "http://127.0.0.1:3001" ]]
echo "$got"
""")
    assert result.returncode == 0, result.stderr
    text = envfile.read_text()
    assert "GITEA_URL=http://127.0.0.1:3001\n" in text
    assert "TBR_DB_PORT=" not in text
    assert "SECRET_KEY=\n" in text


def test_env_example_documents_every_setting_without_empty_typed_values():
    text = (ROOT / ".env.example").read_text()
    for key in ENV_KEYS:
        assert key in text, key
    # Empty bools/ints would break pydantic when it loads the EnvironmentFile.
    for key in ("ENFORCE_CLIENT_NETWORKS", "SCHEDULER_ENABLED", "TBR_DB_PORT", "TBR_MAX_ATTACHMENT_MB",
                "TBR_BACKUP_KEEP_DAYS"):
        for line in text.splitlines():
            if line.startswith(f"{key}="):
                assert line.split("=", 1)[1] != "", key


def test_public_install_docs_have_no_internal_hosts():
    paths = [ROOT / name for name in (*SCRIPTS, ".env.example", "docs/quickstart.md", "deploy/mariadb-setup.sql",
                                               "llm-install-prompt.md")]
    patterns_file = os.environ.get("TBR_PRIVATE_PATTERNS_FILE")
    custom = [re.compile(line) for line in Path(patterns_file).read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")] if patterns_file else []
    for path in paths:
        text = path.read_text()
        for value in IP.findall(text):
            address = ipaddress.ip_address(value)
            if address.is_private:
                assert any(address in n for n in EXAMPLE_NETWORKS), f"{path.name} contains a private IP address"
        for match in INTERNAL_HOST.finditer(text):
            host = match.group().lower()
            assert host == "example.internal" or host.endswith((".example.internal", ".example.ts.net")), \
                f"{path.name} names an internal host"
        for pattern in custom:
            assert not pattern.search(text), f"{path.name} matches a private pattern"


# --- Native SFTP inbox (concept §4) --------------------------------------------------------------------------

def test_sftp_drop_in_is_restrictive():
    """The sshd drop-in chroots the group, forces internal-sftp and forbids shell/forwarding."""
    text = (ROOT / "deploy/ssh/60-taskbrigade-sftp.conf").read_text()
    assert "Match Group tbr-sftp" in text
    assert "ChrootDirectory /srv/taskbrigade-sftp/%u" in text
    assert "ForceCommand internal-sftp" in text
    for wanted in ("AllowTcpForwarding no", "X11Forwarding no", "PermitTTY no", "PasswordAuthentication no",
                   "AuthenticationMethods publickey"):
        assert wanted in text, wanted


def test_sftp_install_helper_uses_root_and_reloads_only_after_check():
    result = _bash(r"""
set -euo pipefail
source deploy/prereq.sh
[[ "$(tbr_sftp_dir_path)" == "/srv/taskbrigade-sftp" ]]
[[ "$(TBR_SFTP_DIR=/tmp/x tbr_sftp_dir_path)" == "/tmp/x" ]]
declare -f tm_install_sftp_inbox >/dev/null
declare -f tm_install_sftp_inbox | grep -q 'sshd -t'
declare -f tm_install_sftp_inbox | grep -q 'taskbrigade-data-import'
echo OK
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_install_and_update_wire_the_sftp_inbox():
    for name in ("deploy/install.sh", "deploy/update.sh"):
        text = (ROOT / name).read_text()
        assert "tm_install_sftp_inbox" in text, name
        assert "TBR_DATA_SFTP" in text, name
    # Both scripts still guard the data-zone routines behind the opt-in switch.
    assert "tbr_data_zones_enabled" in (ROOT / "deploy/install.sh").read_text()


def test_data_import_units_are_valid_systemd_units():
    service = (ROOT / "deploy/systemd/taskbrigade-data-import.service").read_text()
    path = (ROOT / "deploy/systemd/taskbrigade-data-import.path").read_text()
    assert "tbr-admin data sftp-user import" in service
    assert "User=taskbrigade" in service and "SupplementaryGroups=tbr-data" in service
    assert "PathExistsGlob=/srv/taskbrigade-sftp/*/upload/*" in path
    assert "Unit=taskbrigade-data-import.service" in path
