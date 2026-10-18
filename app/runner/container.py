"""Entry point of the runner image (Dockerfile target `runner`, compose profile `data`; docs/docker.md → Data
zones).

- `init` (service `data-init`, root, one-shot): the Docker counterpart of `TBR_DATA_ZONES=1 deploy/install.sh` –
  folder layout of the data volumes with the ids baked into the image, and the KEK, created once and never
  overwritten. Idempotent; refuses without `TBR_DATA_ZONES=1`.
- `serve` (service `runner`, user `tbr-runner`): waits until `tbr-admin runner join <code>` has written the runner
  configuration into the state volume, then runs the claim loop with the Docker launcher.

The directories get the set-group-id bit: files the app writes into the store and files the runner writes into the
quarantine inherit the group `tbr-data`, so the other side can read them (the containers have no shared primary
group).
"""
import logging
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("taskbrigade.runner.container")

APP_UID, RUNNER_UID, DATA_GID = 10001, 10002, 10004
WAIT_HINT_S = 600


@dataclass(frozen=True)
class Entry:
    path: Path
    uid: int
    gid: int
    mode: int


def layout(data_dir: Path, key_file: Path, state_dir: Path, spool_dir: Path) -> list[Entry]:
    """Folders of the Docker data zones (concept §9 table; set-group-id where two containers share files). The
    tmpfs volumes for the job directories and the launcher socket get owner and mode from their mount options
    (compose.yaml), because a tmpfs starts empty on every mount."""
    sgid = stat.S_ISGID
    return [
        Entry(data_dir, APP_UID, DATA_GID, sgid | 0o750),
        Entry(data_dir / "store", APP_UID, DATA_GID, sgid | 0o750),
        Entry(data_dir / "incoming", APP_UID, DATA_GID, sgid | 0o750),
        Entry(data_dir / "quarantine", RUNNER_UID, DATA_GID, sgid | 0o750),
        Entry(key_file.parent, 0, DATA_GID, 0o750),
        Entry(state_dir, RUNNER_UID, RUNNER_UID, 0o700),
        # Spool of the SFTP accounts (profile data-sftp): written by the app, read by the SFTP container.
        Entry(spool_dir, APP_UID, APP_UID, 0o755),
    ]


def _apply(entry: Entry) -> None:
    entry.path.mkdir(parents=True, exist_ok=True)
    st = entry.path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise SystemExit(f"{entry.path} is not a directory")
    if (st.st_uid, st.st_gid) != (entry.uid, entry.gid):
        os.chown(entry.path, entry.uid, entry.gid, follow_symlinks=False)
    os.chmod(entry.path, entry.mode)


def _regroup(root: Path, gid: int) -> int:
    """Files written before the set-group-id bit existed: give them the data group (no other change)."""
    changed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode) or st.st_gid == gid:
                continue
            os.chown(path, -1, gid, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_ISGID)
            changed += 1
    return changed


def init(env=os.environ) -> int:
    from app import data_crypto
    from app.config import get_settings

    if env.get("TBR_DATA_ZONES") != "1":
        print("data-init: TBR_DATA_ZONES=1 is not set in .env – the data zones stay off (docs/docker.md → Data "
              "zones)", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        print("data-init must run as root (compose service data-init)", file=sys.stderr)
        return 1
    settings = get_settings()
    from app.runner.config import DEFAULT_TOKEN_FILE

    data_dir, key_file = Path(settings.tbr_data_dir), Path(settings.tbr_data_key_file)
    for entry in layout(data_dir, key_file, DEFAULT_TOKEN_FILE.parent, Path(settings.tbr_sftp_spool).parent):
        _apply(entry)
    if data_crypto.create_kek(key_file):
        print(f"data-init: data store key created at {key_file} – back it up separately (docs/docker.md)")
    os.chown(key_file, 0, DATA_GID)
    os.chmod(key_file, 0o640)
    data_crypto.load_kek(key_file)  # Fails loudly on a broken key instead of at the first job
    fixed = sum(_regroup(data_dir / name, DATA_GID) for name in ("store", "incoming", "quarantine"))
    print(f"data-init: layout ok ({data_dir}, {key_file}; {fixed} file group(s) fixed)")
    return 0


def serve(env=os.environ, sleep=time.sleep) -> int:
    from app.runner import config
    from app.runner.service import serve_forever

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    path = config.config_path()
    waited = None
    while not path.is_file():
        if waited is None or waited >= WAIT_HINT_S:
            log.info("waiting for %s – create a runner agent (Admin → New agent, kind runner) and run: docker "
                     "compose exec runner tbr-admin runner join <code>", path)
            waited = 0
        sleep(10)
        waited += 10
    try:
        cfg = config.load(path)
    except config.RunnerConfigError as e:
        log.error("%s", e)
        return 1
    serve_forever(cfg)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "serve"
    if command == "init":
        return init()
    if command == "serve":
        return serve()
    if command == "launcher":
        from app.runner.docker import main as launcher_main

        return launcher_main()
    print(f"unknown command {command!r} (init, serve, launcher)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
