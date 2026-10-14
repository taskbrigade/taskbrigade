#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/screenshots/ from a throwaway Docker demo.

    python3 deploy/release/screenshots.py            # stack up → demo data → screenshots → stack removed
    python3 deploy/release/screenshots.py --keep     # keep the stack to look around (URL and login printed)

Steps:
1. `docker compose --profile gitea up` as its own Compose project (`tbr-shots`, own ports, volumes and network,
   like deploy/docker/smoke-test.sh), so a TaskBrigade stack on the same host is not touched. Throwaway passwords.
2. `deploy/release/screenshot_seed.py` inside the app container fills it with neutral demo data (project DEMO,
   sprints, tasks in every status, agents with generic names).
3. Playwright in the container `mcr.microsoft.com/playwright/python` (host network) signs in as `admin` and saves
   the pages in the light theme at 1440×900 (plus 390×844 phone pages) as palette PNGs (`capture` subcommand of this script).

Requirements: Docker with Compose v2.24+ and Python 3 on the host; nothing else is installed on the host.
Only invented demo data is visible – check the images before committing them anyway.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "tbr-shots"
PLAYWRIGHT_VERSION = "1.63.0"
PLAYWRIGHT_IMAGE = f"mcr.microsoft.com/playwright/python:v{PLAYWRIGHT_VERSION}-noble"
VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}  # Smartphone, portrait.
MAX_HEIGHT = 1500  # Long pages (task detail) are cut here.

# file name → (path, full page?, description); "{editor}" is filled in from the seed output.
SHOTS = {
    "inbox.png": ("/", False, "Inbox: questions and reviews waiting for a human"),
    "backlog.png": ("/backlog", False, "Backlog with filters"),
    "kanban.png": ("/kanban", False, "Kanban board"),
    "task-detail.png": ("/tasks/DEMO-0001", True, "Task detail with thread and pull request"),
    "sprint-editor.png": ("{editor}", False, "Sprint editor"),
    "agents.png": ("/admin/users", False, "Users and agents"),
}
# Same demo data at phone size (touch, mobile layout); same format as SHOTS.
MOBILE_SHOTS = {
    "inbox-mobile.png": ("/", False, "Inbox on a smartphone"),
    "backlog-mobile.png": ("/backlog", False, "Backlog as cards on a smartphone"),
    "kanban-mobile.png": ("/kanban", False, "Kanban board with the status column picker on a smartphone"),
    "task-detail-mobile.png": ("/tasks/DEMO-0001", False, "Task detail on a smartphone"),
}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), file=sys.stderr, flush=True)
    kw.setdefault("check", True)
    return subprocess.run(cmd, **kw)


class Stack:
    def __init__(self, work: Path, app_port: int, gitea_port: int, ssh_port: int):
        self.url = f"http://127.0.0.1:{app_port}"
        self.password = secrets.token_urlsafe(24)
        gateway = "172.31.251.1"  # The smoke test's documented subnet; do not run both at once.
        self.env = {**os.environ,
                    "TBR_DB_PASSWORD": secrets.token_urlsafe(24),
                    "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(24),
                    "TBR_INITIAL_ADMIN_PASSWORD": self.password,
                    "TBR_PUBLISH": f"127.0.0.1:{app_port}",
                    "TBR_PUBLIC_URL": self.url,
                    "TBR_GITEA_PUBLISH": f"127.0.0.1:{gitea_port}",
                    "TBR_GITEA_SSH_PUBLISH": f"127.0.0.1:{ssh_port}",
                    "GITEA_PUBLIC_URL": f"http://localhost:{gitea_port}",
                    "TBR_DOCKER_SUBNET": "172.31.251.0/24",
                    "TBR_DOCKER_GATEWAY": gateway,
                    "TRUSTED_PEER_NETWORKS": f"{gateway}/32",
                    "UI_LANGUAGE": "en"}
        self.override = work / "compose.shots.yaml"
        # Network and volume names are fixed in compose.yaml; rename them for this project.
        self.override.write_text(
            "services:\n  taskbrigade:\n    environment:\n"
            "      TBR_INITIAL_ADMIN_PASSWORD: ${TBR_INITIAL_ADMIN_PASSWORD}\n      UI_LANGUAGE: en\n"
            f"networks:\n  taskbrigade:\n    name: {PROJECT}\n"
            f"volumes:\n  taskbrigade-db:\n    name: {PROJECT}-db\n  taskbrigade-state:\n    name: {PROJECT}-state\n"
            f"  gitea-data:\n    name: {PROJECT}-gitea\n")

    def compose(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return run(["docker", "compose", "-p", PROJECT, "--project-directory", str(ROOT), "-f",
                    str(ROOT / "compose.yaml"), "-f", str(self.override), "--profile", "gitea", *args],
                   env=self.env, **kw)

    def up(self) -> None:
        self.compose("up", "-d", "--build", "--quiet-pull")
        self.compose("wait", "gitea-init", stdout=subprocess.DEVNULL, check=False)
        cid = self.compose("ps", "-aq", "gitea-init", capture_output=True, text=True).stdout.strip()
        rc = run(["docker", "inspect", "-f", "{{.State.ExitCode}}", cid], capture_output=True, text=True).stdout
        if rc.strip() != "0":
            self.compose("logs", "--no-color", "--tail", "60", "gitea-init", check=False)
            raise SystemExit(f"gitea-init exited with {rc.strip()}")
        for _attempt in range(60):
            try:
                urllib.request.urlopen(f"{self.url}/healthz", timeout=3)
                return
            except OSError:
                time.sleep(2)
        raise SystemExit(f"{self.url}/healthz does not answer")

    def seed(self) -> dict:
        with open(ROOT / "deploy/release/screenshot_seed.py", "rb") as f:
            out = self.compose("exec", "-T", "taskbrigade", "python", "-", stdin=f, stdout=subprocess.PIPE,
                               text=True).stdout
        print(out, end="", file=sys.stderr)
        line = next(x for x in out.splitlines() if x.startswith("SEED "))
        return json.loads(line[5:])

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     check=False)


def capture_in_container(url: str, password: str, out: Path, paths: dict) -> None:
    """Run `capture` in the Playwright image: the host's Chromium often lacks system libraries."""
    out.mkdir(parents=True, exist_ok=True)
    script = (f"pip install --quiet --user playwright=={PLAYWRIGHT_VERSION} pillow && "
              f"python /src/deploy/release/screenshots.py capture --url {url} --out /out "
              f"--paths '{json.dumps(paths)}'")
    run(["docker", "run", "--rm", "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}",
         "-e", "HOME=/tmp", "-e", "TBR_SHOTS_PASSWORD", "-v", f"{ROOT}:/src:ro", "-v", f"{out.resolve()}:/out",
         PLAYWRIGHT_IMAGE, "bash", "-c", script], env={**os.environ, "TBR_SHOTS_PASSWORD": password})


def capture(url: str, out: Path, paths: dict) -> None:
    """Sign in and save every page of SHOTS (runs inside the Playwright container)."""
    from io import BytesIO

    from PIL import Image
    from playwright.sync_api import sync_playwright

    def save(page, name: str, full: bool) -> None:
        png = page.screenshot(full_page=full)
        img = Image.open(BytesIO(png)).convert("RGB")
        if img.height > MAX_HEIGHT:
            img = img.crop((0, 0, img.width, MAX_HEIGHT))
        # 256-colour palette without dithering: UI screenshots stay sharp at a fraction of the size.
        img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(
            out / name, optimize=True)
        print(f"{name}: {(out / name).stat().st_size // 1024} KiB", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for shots, ctx_args in ((SHOTS, {"viewport": VIEWPORT}),
                                (MOBILE_SHOTS, {"viewport": MOBILE_VIEWPORT, "is_mobile": True, "has_touch": True,
                                                "device_scale_factor": 2})):
            context = browser.new_context(color_scheme="light", locale="en-US", **ctx_args)
            page = context.new_page()
            page.goto(f"{url}/language?lang=en")
            page.goto(f"{url}/login")
            page.fill("input[name=username]", "admin")
            page.fill("input[name=password]", os.environ["TBR_SHOTS_PASSWORD"])
            page.press("input[name=password]", "Enter")
            page.wait_for_url(f"{url}/")
            for name, (path, full, _desc) in shots.items():
                page.goto(url + path.format(**paths))
                page.wait_for_load_state("networkidle")
                save(page, name, full)
            context.close()
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    ap.add_argument("--app-port", type=int, default=18090)
    ap.add_argument("--gitea-port", type=int, default=13300)
    ap.add_argument("--ssh-port", type=int, default=12222)
    ap.add_argument("--keep", action="store_true", help="keep the stack running afterwards")
    sub = ap.add_subparsers(dest="cmd")
    cap = sub.add_parser("capture", help="internal: take the screenshots (inside the Playwright container)")
    cap.add_argument("--url", required=True)
    cap.add_argument("--out", type=Path, required=True)
    cap.add_argument("--paths", default="{}")
    args = ap.parse_args()
    if args.cmd == "capture":
        capture(args.url, args.out, json.loads(args.paths))
        return
    with tempfile.TemporaryDirectory() as work:
        stack = Stack(Path(work), args.app_port, args.gitea_port, args.ssh_port)
        try:
            stack.up()
            paths = stack.seed()
            capture_in_container(stack.url, stack.password, args.out, paths)
        finally:
            if args.keep:
                print(f"Stack {PROJECT} kept: {stack.url} (admin / {stack.password}); remove it with\n"
                      f"  docker compose -p {PROJECT} down && docker volume rm {PROJECT}-db {PROJECT}-state {PROJECT}-gitea",
                      file=sys.stderr)
            else:
                stack.down()


if __name__ == "__main__":
    main()
