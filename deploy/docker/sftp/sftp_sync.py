"""Entry point of the TaskBrigade SFTP container (compose profile `data-sftp`, docs/docker.md → Data zones).

Runs as root inside the container, stdlib only. It applies the spool file the app writes for every change of the
deliverer accounts (`app/services/data_sftp.py`, `TBR_SFTP_SPOOL`, format `tbr-sftp-accounts/1`) and supervises
`sshd -D`:

- each account becomes a user without shell in the group `tbr-sftp` with the fixed uid from the spool;
- chroot `<root>/<user>` `root:root 0755`, `upload/` `<user>:tbr-data 2770` (set-group-id: deliveries get the data
  group, so the app container – member of `tbr-data` – can list, read and delete them), `.ssh/authorized_keys`
  `root:root 0644` (empty while the account is disabled);
- users of `tbr-sftp` missing from the spool are deleted, their folder is kept; names under `purge` lose the
  folder too.

The spool is re-read when it changes and every minute; sshd exiting ends the container (restart policy).
"""
import grp
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("TBR_SFTP_DIR", "/srv/taskbrigade-sftp"))
SPOOL = Path(os.environ.get("TBR_SFTP_SPOOL", "/var/lib/taskbrigade-sftp/accounts.json"))
KEYS = Path("/etc/ssh/keys")
FORMAT = "tbr-sftp-accounts/1"
SFTP_GROUP, DATA_GROUP = "tbr-sftp", "tbr-data"
USERNAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
MIN_UID = 20000
POLL_S, RESYNC_S = 5, 60


def log(message: str) -> None:
    print(f"sftp-sync: {message}", flush=True)


def run(*argv: str) -> None:
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)}: {done.stderr.strip()}")


def host_keys() -> None:
    for kind, extra in (("ed25519", []), ("rsa", ["-b", "3072"])):
        path = KEYS / f"ssh_host_{kind}_key"
        if not path.exists():
            run("ssh-keygen", "-q", "-t", kind, *extra, "-N", "", "-f", str(path))
            log(f"host key {kind} created")


def ensure_dir(path: Path, uid: int, gid: int, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"{path} is not a directory")
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, mode)


def write_file(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chown(tmp, 0, 0)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def sftp_users() -> dict[str, int]:
    gid = grp.getgrnam(SFTP_GROUP).gr_gid
    return {p.pw_name: p.pw_uid for p in pwd.getpwall() if p.pw_gid == gid}


def load_spool() -> dict | None:
    try:
        state = json.loads(SPOOL.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"format": FORMAT, "accounts": [], "purge": []}
    except (OSError, ValueError) as e:
        log(f"spool {SPOOL} unreadable, keeping the current users: {e}")
        return None
    if not isinstance(state, dict) or state.get("format") != FORMAT:
        log(f"spool {SPOOL} has an unknown format, keeping the current users")
        return None
    return state


def valid(account) -> bool:
    return (isinstance(account, dict) and isinstance(account.get("username"), str)
            and USERNAME.match(account["username"]) is not None
            and isinstance(account.get("uid"), int) and account["uid"] >= MIN_UID)


def sync(state: dict) -> None:
    data_gid = grp.getgrnam(DATA_GROUP).gr_gid
    ensure_dir(ROOT, 0, 0, 0o755)
    existing = sftp_users()
    wanted = {}
    for account in state.get("accounts") or []:
        if not valid(account):
            log(f"skipped an invalid account entry: {str(account)[:80]}")
            continue
        wanted[account["username"]] = account
    for name, account in wanted.items():
        uid, home = account["uid"], ROOT / name
        try:
            info = pwd.getpwnam(name)
        except KeyError:
            info = None
        if info is None:
            # Password `*`: none at all, but not locked – sshd without PAM refuses locked (`!`) accounts even for keys.
            run("useradd", "--uid", str(uid), "--gid", SFTP_GROUP, "--home-dir", str(home), "--no-create-home",
                "--shell", "/usr/sbin/nologin", "--password", "*", name)
            log(f"user {name} created (uid {uid})")
        elif info.pw_gid != grp.getgrnam(SFTP_GROUP).gr_gid:
            log(f"{name} exists outside {SFTP_GROUP} – skipped")
            continue
        elif info.pw_uid != uid:
            run("usermod", "--uid", str(uid), name)
        ensure_dir(home, 0, 0, 0o755)
        ensure_dir(home / "upload", uid, data_gid, 0o2770)
        ensure_dir(home / ".ssh", 0, 0, 0o755)
        keys = account.get("authorized_keys") if account.get("enabled") else ""
        write_file(home / ".ssh" / "authorized_keys", keys if isinstance(keys, str) else "")
    for name in sorted(set(existing) - set(wanted)):
        run("userdel", name)
        keys = ROOT / name / ".ssh" / "authorized_keys"
        if keys.is_file():
            write_file(keys, "")
        log(f"user {name} removed (folder kept)")
    for name in state.get("purge") or []:
        if not isinstance(name, str) or not USERNAME.match(name) or name in wanted:
            continue
        folder = ROOT / name
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
            log(f"folder of {name} purged")


def main() -> int:
    host_keys()
    run("/usr/sbin/sshd", "-t")
    state = load_spool()
    if state is not None:
        sync(state)
    sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e"])

    def stop(signum, _frame):
        sshd.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    seen = SPOOL.stat().st_mtime_ns if SPOOL.exists() else None
    last = time.monotonic()
    while sshd.poll() is None:
        time.sleep(POLL_S)
        current = SPOOL.stat().st_mtime_ns if SPOOL.exists() else None
        if current != seen or time.monotonic() - last >= RESYNC_S:
            seen, last = current, time.monotonic()
            state = load_spool()
            if state is not None:
                try:
                    sync(state)
                except (OSError, RuntimeError, KeyError) as e:
                    log(f"sync failed: {e}")
    return sshd.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
