"""HTTP client for `tbr`.

Configuration (first match wins, TBR_URL/TBR_TOKEN override individually):
1. environment variables TBR_URL / TBR_TOKEN
2. file from TBR_CONFIG
3. `.taskbrigade.toml` in the current or a parent directory – so every agent on a machine has its own identity,
   bound to its work directory (also for the Git credential helper)
4. ~/.config/taskbrigade/config.toml

Names from before TaskBrigade 0.8 are still read during the transition, with a note on stderr: `TM_URL`, `TM_TOKEN`,
`TM_CONFIG`, `.taskmeister.toml` (per directory the new name wins) and ~/.config/taskmeister/config.toml.
`tbr setup` and `tbr doctor` rename the file (cli/migrate.py).

Optional `language = "de"` in that file switches agent-facing texts to German (`cli/i18n.py`).
"""
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx

from cli import i18n
from cli.i18n import N_, _

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "taskbrigade" / "config.toml"
LOCAL_CONFIG = ".taskbrigade.toml"
LEGACY_LOCAL_CONFIG = ".taskmeister.toml"  # Before TaskBrigade 0.8; read with a note, renamed by tbr setup/doctor.


class ConfigError(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


@dataclass
class Config:
    url: str
    token: str
    language: str | None = None


def agent_config(work_root: Path) -> Path:
    """The configuration file in an agent's work directory: `.taskbrigade.toml`, or the old name while only that
    exists. Neither there → the new name (error messages, `tbr join`)."""
    path = work_root / LOCAL_CONFIG
    legacy = work_root / LEGACY_LOCAL_CONFIG
    return legacy if not path.is_file() and legacy.is_file() else path


def find_config(start: Path | None = None) -> Path:
    from cli.legacy import getenv

    if getenv("CONFIG"):
        return Path(getenv("CONFIG"))
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        candidate = agent_config(d)
        if candidate.is_file():
            return candidate
    legacy = legacy_global_config()
    return legacy if not CONFIG_PATH.is_file() and legacy.is_file() else CONFIG_PATH


def legacy_global_config() -> Path:
    """~/.config/taskmeister/config.toml – the global file before TaskBrigade 0.8, still read."""
    return CONFIG_PATH.parent.parent / "taskmeister" / CONFIG_PATH.name


def legacy_config_note(path: Path) -> None:
    """Note (once per call) that a configuration file still has its name from before TaskBrigade 0.8."""
    from cli.legacy import note_once

    if path.name == LEGACY_LOCAL_CONFIG:
        new = path.with_name(LOCAL_CONFIG)
    elif path == legacy_global_config():
        new = CONFIG_PATH
    else:
        return
    note_once("config", _("Note: %(path)s still has its name from before TaskBrigade 0.8 and is read during the "
                          "transition; tbr setup or tbr doctor renames it to %(new)s.", path=path, new=new))


def load_config(path: Path | None = None) -> Config:
    from cli.legacy import getenv

    path = path or find_config()
    data = {}
    if path.exists():
        data = tomllib.loads(path.read_text())
        legacy_config_note(path)
    url = getenv("URL") or data.get("url")
    token = getenv("TOKEN") or data.get("token")
    if not url or not token:
        raise ConfigError(_("Set TBR_URL/TBR_TOKEN or create %(path)s with url and token", path=path))
    if data.get("language"):
        i18n.configure(data["language"])
    return Config(url.rstrip("/"), token, data.get("language"))


# --- Version check (server headers X-TaskBrigade-Version/-Build, app/main.py) ----------------

VERSION_HEADER = "X-TaskBrigade-Version"
BUILD_HEADER = "X-TaskBrigade-Build"  # Build identifier of the delivered wheel (cli/build.py); servers ≥ 0.6.
# Servers before TaskBrigade 0.8 send only these; newer servers send both, so older clients keep their check.
LEGACY_VERSION_HEADER = "X-Taskmeister-Version"
LEGACY_BUILD_HEADER = "X-Taskmeister-Build"
_warned = False


DISTRIBUTION = "taskbrigade"
LEGACY_DISTRIBUTION = "taskmeister"  # Package name before TaskBrigade 0.5; `tbr self-update` replaces it.


def installed_distribution() -> tuple[str, str] | None:
    """(package name, version) of the installed CLI – the new name first; None in a plain source tree."""
    from importlib.metadata import PackageNotFoundError, version

    for name in (DISTRIBUTION, LEGACY_DISTRIBUTION):
        try:
            return name, version(name)
        except PackageNotFoundError:
            continue
    return None


def local_version() -> str | None:
    found = installed_distribution()
    return found[1] if found else None


def local_build() -> str:
    """Build identifier of the installed CLI ("" in a source checkout, cli/build.py)."""
    from cli import build

    found = installed_distribution()
    return build.installed(found[0]) if found else ""


def _version_key(value: str | None) -> tuple[int, ...] | None:
    """Leading numeric release part (`0.4.0`, `0.5.0rc1` → (0, 5, 0)); None if there is none."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", value or "")
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_older(local: str | None, server: str | None) -> bool:
    """True only if both versions are known and the local one is older."""
    a, b = _version_key(local), _version_key(server)
    return a is not None and b is not None and a < b


def other_build(local: str | None, server: str | None, local_build_id: str | None, server_build: str | None) -> bool:
    """True if both run the same version but different known builds (a change rolled out without a version bump)."""
    return bool(local and local == server and local_build_id and server_build and local_build_id != server_build)


def needs_update(local: str | None, server: str | None, local_build_id: str | None = None,
                 server_build: str | None = None) -> bool:
    """Older version, or same version with another build. Used by the warning and by `tbr self-update`."""
    return is_older(local, server) or other_build(local, server, local_build_id, server_build)


def version_warning(server: str | None, local: str | None = None, server_build: str | None = None,
                    local_build_id: str | None = None) -> str | None:
    from cli.build import label

    local = local if local is not None else local_version()
    if is_older(local, server):
        return _("Warning: tbr %(local)s is older than the TaskBrigade server (%(server)s) – update with: tbr "
                 "self-update", local=local, server=server)
    if not (server_build and local and local == server):
        return None
    local_build_id = local_build_id if local_build_id is not None else local_build()
    if not other_build(local, server, local_build_id, server_build):
        return None
    return _("Warning: tbr %(local)s is another build than the TaskBrigade server (%(server)s) – update with: tbr "
             "self-update", local=label(local, local_build_id), server=label(server, server_build))


def server_headers(r: httpx.Response) -> tuple[str | None, str | None]:
    """(version, build) of the server from a response, also from a server before TaskBrigade 0.8."""
    return (r.headers.get(VERSION_HEADER) or r.headers.get(LEGACY_VERSION_HEADER),
            r.headers.get(BUILD_HEADER) or r.headers.get(LEGACY_BUILD_HEADER))


def wheel_name(version: str) -> str:
    """Name of the wheel the server delivers under /download/ (app/services/distribution.py)."""
    return f"{DISTRIBUTION}-{version}-py3-none-any.whl"


class Client:
    server_version: str | None = None  # From the last response's X-TaskBrigade-Version header.
    server_build: str | None = None  # From X-TaskBrigade-Build (missing on servers before 0.6).

    def __init__(self, config: Config):
        self._http = httpx.Client(base_url=f"{config.url}/api/v1", timeout=30,
                                  headers={"Authorization": f"Bearer {config.token}"})

    def version_warning(self) -> str | None:
        return version_warning(self.server_version, server_build=self.server_build)

    def _note_version(self, r: httpx.Response) -> None:
        global _warned
        server, build = server_headers(r)
        if not server or (server, build) == (self.server_version, self.server_build):
            return
        self.server_version, self.server_build = server, build
        warning = self.version_warning()
        if warning and not _warned:  # Once per call; stderr, stdout carries git credentials or the MCP protocol.
            _warned = True
            print(warning, file=sys.stderr)

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ApiError(0, "connection_error", _("TaskBrigade is not reachable: %(error)s", error=e)) from e
        self._note_version(r)
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                raise ApiError(r.status_code, err.get("code", "error"), err.get("message", ""))
            except (ValueError, KeyError, TypeError):
                raise ApiError(r.status_code, "error", r.text[:200]) from None
        return r

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw).json()


def agent_scope(actor: dict) -> str:
    """Suffix for agent lists (`tbr agents`, MCP `list_agents`): public, or the owning human. Older servers send
    neither field – then nothing."""
    if actor.get("actor_type") != "agent":
        return ""
    if actor.get("is_public"):
        return ", " + _("public")
    return ", " + _("owner %(name)s", name=actor["owner"]) if actor.get("owner") else ""


# --- Claim and submit helpers (tbr claim/prepare/submit, MCP start_task/submit_task, runner) ---

def parallel_limit_note(client: "Client") -> str | None:
    """Explain an empty claim when the agent already has `max_parallel_tasks` tasks in progress, else None.
    Diagnostic only: errors return None."""
    try:
        limit = client.get("/agents/me").get("max_parallel_tasks") or 1
        running = [t["task_nr"] for t in client.get("/agents/me/tasks", params={"status": "in_progress"})
                   .get("items") or []]
    except Exception:  # noqa: BLE001
        return None
    try:  # Review jobs of reviewer agents count too (older servers: none).
        running += [t["task_nr"] + " (review)" for t in client.get("/agents/me/reviews").get("items") or []
                    if t.get("review_claimed")]
    except Exception:  # noqa: BLE001
        pass
    if len(running) < limit:
        return None
    return _("Limit reached: you may work on %(max)s task(s) at a time and have %(running)s in progress. Finish, "
             "submit or release one of them first.", max=limit, running=", ".join(running))


OPEN_POINTS_HEADING = re.compile(r"^#{1,6}\s*(open points|offene punkte)\s*:?\s*$", re.IGNORECASE)


def split_open_points(summary: str) -> tuple[str, list[str]]:
    """Split a trailing `## Open points` section (bullet list) off a summary: (summary, points).
    Used for `.tm-submit.md`; without the heading the summary is returned unchanged."""
    lines = summary.splitlines()
    for i, line in enumerate(lines):
        if OPEN_POINTS_HEADING.match(line.strip()):
            points, rest = [], []
            for item in lines[i + 1:]:
                m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*)$", item)
                if m and m.group(1).strip():
                    points.append(m.group(1).strip())
                elif item.strip() and points and item.startswith((" ", "\t")):
                    points[-1] += " " + item.strip()  # Continuation line of the previous point
                elif item.strip():
                    rest.append(item)
            head = "\n".join(lines[:i] + ([""] + rest if rest else [])).strip()
            return head, points
    return summary, []


# --- Orchestrator calls (tbr update/cancel/reset, MCP update_task/cancel_task) ----------------

def parse_due(value: str | None) -> str | None:
    """Validate a YYYY-MM-DD due date. Return ISO date; "none"/"-" removes the date (None)."""
    from datetime import date

    if value is None or value.strip().lower() in ("none", "-", ""):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as e:
        raise ValueError(_("Due date '%(value)s' is invalid – format YYYY-MM-DD", value=value)) from e


def parse_depends_on(value: "list[str] | str | None") -> list[str]:
    """Task numbers a task depends on, from a list or a comma-separated string (upper case, no duplicates).
    "none"/"-" alone (or an empty value) → [] (removes all dependencies); mixed with numbers → ValueError."""
    items = [value] if isinstance(value, str) else list(value or [])
    nrs = [p.strip().upper() for item in items for p in str(item).replace(";", ",").split(",") if p.strip()]
    none = [n for n in nrs if n.lower() in ("none", "-")]
    if none and len(none) != len(nrs):
        raise ValueError(_("depends_on: 'none' cannot be combined with task numbers"))
    return [] if none else list(dict.fromkeys(nrs))


WAIT_MAX_MINUTES = 24 * 60


def parse_usage(value: str | None) -> dict | None:
    """Parse `--usage input=123,output=456,cost_cents=12,model=foo` into the API usage body (None when empty).

    `input`/`output` become `input_tokens`/`output_tokens`; `model` is free text."""
    if not value or not value.strip():
        return None
    data: dict = {}
    for part in value.split(","):
        key, sep, val = part.strip().partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(_("Usage '%(value)s' is invalid – format input=123,output=456,cost_cents=12,model=foo",
                               value=value))
        if key == "model":
            data["model"] = val.strip() or None
            continue
        if key in ("input", "output"):
            key = {"input": "input_tokens", "output": "output_tokens"}[key]
        elif key not in ("cost_cents",):
            raise ValueError(_("Usage '%(value)s' is invalid – format input=123,output=456,cost_cents=12,model=foo",
                               value=value))
        try:
            data[key] = int(val.strip())
        except ValueError as e:
            raise ValueError(_("Usage '%(value)s': %(key)s must be an integer", value=value, key=key)) from e
    if not any(k in data for k in ("input_tokens", "output_tokens", "cost_cents")):
        raise ValueError(_("Usage needs at least one of input=, output= or cost_cents="))
    return data


def parse_duration(value: str | None) -> int | None:
    """Minutes from `90`, `45m`, `2h`, `1h30m` or `1d` (a bare number = minutes); None/"" = not given."""
    import re

    text = (value or "").strip().lower().replace(" ", "")
    if not text:
        return None
    if text.isdigit():
        minutes = int(text)
    else:
        m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", text)
        if not m or not any(m.groups()):
            raise ValueError(_("Duration '%(value)s' is invalid – examples: 90m, 2h, 1h30m", value=value))
        d, h, mi = (int(g or 0) for g in m.groups())
        minutes = d * 1440 + h * 60 + mi
    if minutes < 1:
        raise ValueError(_("Duration '%(value)s' is invalid – examples: 90m, 2h, 1h30m", value=value))
    return minutes


def heartbeat_task(client: "Client", task_nr: str, waiting: str | None = None, duration: str | None = None,
                   watch_file: str | None = None, done_waiting: bool = False, usage: dict | None = None) -> dict:
    """POST /tasks/{nr}/heartbeat; with `waiting` the task shows "waiting for a background run" (no stall report).
    A relative `watch_file` is made absolute here: the runner watching it runs on this machine, not in its cwd."""
    body: dict = {}
    if waiting and waiting.strip():
        body["waiting"] = waiting.strip()
        minutes = parse_duration(duration)
        if minutes:
            body["for_minutes"] = minutes
        if watch_file and watch_file.strip():
            body["watch_file"] = os.path.abspath(os.path.expanduser(watch_file.strip()))
    elif duration or watch_file:
        raise ValueError(_("--for and --watch-file need --waiting"))
    if done_waiting:
        if waiting:
            raise ValueError(_("--waiting and --done-waiting exclude each other"))
        body["done_waiting"] = True
    if usage is not None:
        body["usage"] = usage
    return client.request("POST", f"/tasks/{task_nr.upper()}/heartbeat", json=body or None).json()


def waiting_report(r: dict) -> str:
    """One line for the heartbeat result."""
    line = _("Lease of %(nr)s until %(until)s UTC", nr=r["task_nr"], until=r["lease_expires_at"])
    w = r.get("waiting")
    if w:
        line += "\n" + _("Waiting for a background run: %(reason)s (until %(until)s UTC) – extend it before it ends "
                          "with tbr heartbeat --waiting …", reason=w["reason"], until=w["until"])
    return line


def explain(e: ApiError, action: str) -> str:
    """Explain an API error with next steps for orchestrator calls."""
    hint = {
        403: _("No permission for %(action)s – only allowed with role orchestrator or admin, or as in the web UI "
               "(your own role: tbr whoami)", action=action),
        404: _("Task not found"),
        409: (_("Version conflict – the task was changed in the meantime; check with tbr show and run again")
              if e.code == "version_conflict" else _("Not possible in the current state (tbr show shows the state)")),
    }.get(e.status)
    return f"{e.status} {e.code}: {e.message}" + (f"\n→ {hint}" if hint else "")


def patch_task(client: "Client", task_nr: str, changes: dict, version: int | None = None) -> dict:
    """PATCH /tasks/{nr} with If-Match. Without `version`, use the just-read row_version so a
    concurrent edit causes a conflict instead of being silently overwritten."""
    if version is None:
        version = client.get(f"/tasks/{task_nr}")["row_version"]
    return client.request("PATCH", f"/tasks/{task_nr}", json=changes, headers={"If-Match": str(version)}).json()


# `tbr review` / MCP `review_task` decision → API decision; `release` gives a review job back undecided.
REVIEW_DECISIONS = {"approve": "approve", "reject": "reject", "conflict": "reject_conflict",
                    "reject_conflict": "reject_conflict"}


def review_task(client: "Client", task_nr: str, decision: str, comment: str | None = None) -> dict:
    """Review decision (approve/reject/conflict) or `release` of a review job; ValueError for an invalid decision or
    a rejection/release without text."""
    nr, value, text = task_nr.strip().upper(), decision.strip().lower(), (comment or "").strip()
    if value == "release":
        if not text:
            raise ValueError(_("Give a reason: why the review job goes back"))
        return client.request("POST", f"/tasks/{nr}/review/release", json={"reason": text}).json()
    if value not in REVIEW_DECISIONS:
        raise ValueError(_("Decision must be approve, reject, conflict or release"))
    if value == "reject" and not text:
        raise ValueError(_("A rejection needs a comment: what is missing or wrong"))
    body = {"decision": REVIEW_DECISIONS[value], **({"comment": text} if text else {})}
    return client.request("POST", f"/tasks/{nr}/review", json=body).json()


def duplicate_task(client: "Client", task_nr: str, body: dict) -> dict:
    """POST /tasks/{nr}/duplicate (`assign`, `title` optional) → the new draft."""
    return client.request("POST", f"/tasks/{task_nr.strip().upper()}/duplicate", json=body).json()


# Model tier of a task (`tbr create/update --tier`, MCP `tier`): English and German names, "none" removes it.
TIERS = {"simple": "simple", "einfach": "simple", "easy": "simple", "standard": "standard", "normal": "standard",
         "medium": "standard", "complex": "complex", "komplex": "complex", "hard": "complex", "schwer": "complex"}


def parse_tier(value: str | None) -> str | None:
    """Tier name → `simple`|`standard`|`complex`; "none"/"-"/"" → None (removes the tier); unknown → ValueError."""
    key = (value or "").strip().lower()
    if key in ("", "none", "-"):
        return None
    if key not in TIERS:
        raise ValueError(_("Unknown model tier '%(value)s' – allowed: simple, standard, complex (or none)",
                           value=value))
    return TIERS[key]


DATA_CLASSES = ("cloud_ok", "local_only", "sealed")


def parse_data_class(value: str | None) -> str | None:
    """`--data-class`/MCP `data_class`: cloud_ok, local_only or sealed; "inherit"/"none"/"-" → "inherit" (the task
    takes the project's privacy again); empty → None (unchanged); unknown → ValueError."""
    key = (value or "").strip().lower().replace("-", "_")
    if not key:
        return None
    if key in ("inherit", "none", "_"):
        return "inherit"
    if key not in DATA_CLASSES:
        raise ValueError(_("Unknown data class '%(value)s' – allowed: cloud_ok, local_only, sealed (or inherit)",
                           value=value))
    return key


def data_class_line(t: dict) -> str | None:
    """`- Data class: …` for TASK.md/show: the effective class and whether the task sets it or inherits it."""
    eff = t.get("data_class_effective")
    if not eff:
        return None
    if t.get("data_class"):
        return _("- Data class: %(cls)s (set on the task)", cls=eff)
    return _("- Data class: %(cls)s (inherited from the project)", cls=eff)


def data_refs_lines(t: dict) -> list[str]:
    """TASK.md/show section "Data references" (task_data_refs, DZ 9): what the task refers to – never content."""
    refs = t.get("data_refs") or []
    if not refs:
        return []
    lines = ["", _("## Data references"), ""]
    for r in refs:
        if r["type"] == "dataset":
            lines.append(_("- %(purpose)s: dataset %(label)s – raw data, only the runner reads it", purpose=r["purpose"],
                           label=r["label"]))
        else:
            lines.append(_("- %(purpose)s: %(label)s (%(status)s)", purpose=r["purpose"], label=r["label"],
                           status=r["status"]))
    return lines


def model_value(value: str | None) -> str | None:
    """`--model`/MCP `model`: "none"/"-"/"" → None (removes the concrete model)."""
    value = (value or "").strip()
    return None if value.lower() in ("", "none", "-") else value


def model_line(t: dict) -> str | None:
    """TASK.md/`tbr show` line about the task's model, or None without any model choice."""
    eff = t.get("model_effective") or {}
    if not (t.get("model_tier") or t.get("model") or eff.get("model")):
        return None
    model = f"`{eff['model']}`" if eff.get("model") else _("the agent's default")
    parts = [_("tier %(tier)s", tier=_(TIER_LABELS[t["model_tier"]]))] if t.get("model_tier") in TIER_LABELS else []
    if t.get("model"):
        parts.append(_("chosen explicitly"))
    line = _("- Model: %(model)s", model=model) + (f" ({', '.join(parts)})" if parts else "")
    if eff and not eff.get("applies", True):
        line += _(" – ignored by this agent (it chooses its model itself)")
    return line


TIER_LABELS = {"simple": N_("simple"), "standard": N_("standard"), "complex": N_("complex")}


def transition(client: "Client", task_nr: str, to: str, comment: str | None = None) -> dict:
    """Generic status change (POST /tasks/{nr}/transition); the server checks UI-equivalent permissions."""
    body = {"to": to, **({"comment": comment} if comment and comment.strip() else {})}
    return client.request("POST", f"/tasks/{task_nr}/transition", json=body).json()


# --- Sprints (tbr sprint …, tbr create/update/list --sprint, MCP sprint tools) ---------------

NO_SPRINT = ("none", "-")
BATCH_FORMAT = "Title | Agent | Prio | Reviewer"


def is_no_sprint(value: str) -> bool:
    return value.strip().lower() in NO_SPRINT


def resolve_sprint(client: "Client", project: str | None, value: str) -> int:
    """Sprint id from an id or a sprint name of `project` (names are unique per project, compared without case).
    Raise ValueError if the name is unknown or a name is given without a project."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    if not project:
        raise ValueError(_("Sprint '%(sprint)s': give the project too, or use the sprint id", sprint=value))
    sprints = client.get(f"/projects/{project.strip().upper()}/sprints")
    for s in sprints:
        if s["name"].casefold() == value.casefold():
            return s["id"]
    known = ", ".join(f"{s['id']} {s['name']}" for s in sprints) or "–"
    raise ValueError(_("Project %(project)s has no sprint '%(sprint)s' (sprints: %(known)s)",
                       project=project.strip().upper(), sprint=value, known=known))


def parse_batch(text: str) -> tuple[list[dict], list[str]]:
    """Batch lines as in the web UI: one task per non-empty line, `Title` or `Title | Agent | Prio | Reviewer` (agent,
    prio and reviewer may be empty – an empty reviewer means the project default; lines starting with # are
    comments). Returns the entries and one error per invalid line; the server checks the actors."""
    entries, errors = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in raw.split("|")]
        title, agent, prio, reviewer = (parts + ["", "", ""])[:4]
        if len(parts) > 4:
            errors.append(_("Line %(n)d: too many fields – use %(format)s", n=n, format=BATCH_FORMAT))
        elif not title:
            errors.append(_("Line %(n)d: title is missing", n=n))
        elif len(title) > 200:
            errors.append(_("Line %(n)d: title longer than 200 characters", n=n))
        elif prio and not (prio.isdigit() and 1 <= int(prio) <= 5):
            errors.append(_("Line %(n)d: prio must be between 1 and 5", n=n))
        else:
            entries.append({"title": title, "assigned_to": agent or None, "prio": int(prio) if prio else 3,
                            **({"reviewer": reviewer} if reviewer else {})})
    return entries, errors


def sprint_line(s: dict) -> str:
    """One-line sprint summary: id, name, status, dates, task counts."""
    counts = s.get("task_counts") or {}
    total = sum(counts.values())
    done = counts.get("done", 0) + counts.get("cancelled", 0)
    dates = f"{s.get('start_date') or '?'} – {s.get('end_date') or '?'}" if s.get("start_date") or s.get(
        "end_date") else "–"
    return _("%(id)s %(name)s [%(status)s] %(dates)s · %(done)s/%(total)s tasks finished", id=s["id"],
             name=s["name"], status=s["status"], dates=dates, done=done, total=total)


def release_report(result: dict) -> str:
    """Text for a sprint release: released tasks, drafts that stay drafts with their missing fields and released
    tasks waiting for a late dependency."""
    lines = [_("Sprint '%(sprint)s' released: %(num)s task(s) set to defined", sprint=result["sprint"]["name"],
               num=len(result["released"]))]
    lines += [f"  + {t['task_nr']} {t['title']}" for t in result["released"]]
    if result["blocked"]:
        lines.append(_("%(num)s draft(s) stay drafts – complete them and release again:", num=len(result["blocked"])))
        lines += [_("  - %(nr)s %(title)s (missing: %(missing)s)", nr=t["task_nr"], title=t["title"],
                    missing=", ".join(t["missing"])) for t in result["blocked"]]
    if result.get("waiting"):  # Older servers do not send it.
        lines.append(_("Warning: %(num)s released task(s) wait for a task that is not done and not planned before "
                       "this sprint:", num=len(result["waiting"])))
        lines += [_("  ! %(nr)s %(title)s waits for %(deps)s", nr=t["task_nr"], title=t["title"],
                    deps=", ".join(_dep_where(d) for d in t["waits_for"])) for t in result["waiting"]]
    return "\n".join(lines)


def _dep_where(dep: dict) -> str:
    if dep.get("sprint") is None:
        return _("%(nr)s (no sprint)", nr=dep["task_nr"])
    return f"{dep['task_nr']} ({dep['sprint_name']}, {dep['sprint_status']})"


def close_report(result: dict, target_name: str | None = None) -> str:
    name = result["sprint"]["name"]
    if result["moved"]:
        return _("Sprint '%(sprint)s' closed: %(num)s open task(s) moved to sprint %(target)s (%(nrs)s)", sprint=name,
                 num=len(result["moved"]), target=target_name or result["moved_to"], nrs=", ".join(result["moved"]))
    if result["detached"]:
        return _("Sprint '%(sprint)s' closed: %(num)s open task(s) detached from the sprint (%(nrs)s)", sprint=name,
                 num=len(result["detached"]), nrs=", ".join(result["detached"]))
    return _("Sprint '%(sprint)s' closed.", sprint=name)


def reopen_report(result: dict) -> str:
    """Text for a sprint reopen: restored tasks and the ones that were skipped."""
    name = result["sprint"]["name"]
    lines = [_("Sprint '%(sprint)s' reopened – status is '%(status)s' again.", sprint=name,
               status=result["sprint"]["status"])]
    if result.get("restored"):
        lines.append(_("%(num)s task(s) moved back into the sprint:", num=len(result["restored"])))
        lines += [f"  + {nr}" for nr in result["restored"]]
    if result.get("skipped"):
        lines.append(_("%(num)s task(s) stay where they are (moved again later or finished):",
                       num=len(result["skipped"])))
        lines += [f"  - {nr}" for nr in result["skipped"]]
    return "\n".join(lines)


# --- Notifications (tbr notify …, MCP notify) ----------------------------------------------------

NOTIFY_ACTIONS = ("status", "pause", "resume", "reviews_off", "reviews_on")


def notify_request(client: "Client", action: str, until: str | None = None, user: str | None = None) -> dict:
    """GET (status) or POST /notifications/settings. `user`: another human (orchestrators and admins only)."""
    user = (user or "").strip() or None
    if action == "status":
        return client.get("/notifications/settings", params={"user": user} if user else None)
    body = {"action": action, "until": (until or "").strip() or None, "user": user}
    return client.request("POST", "/notifications/settings", json={k: v for k, v in body.items() if v}).json()


def _mute_line(info: dict, tz: str) -> str:
    if not info.get("muted"):
        return _("on")
    if info.get("forever"):
        return _("muted until switched back on")
    return _("muted until %(time)s (%(tz)s)", time=info.get("until_local") or info.get("until"), tz=tz)


def notify_report(data: dict) -> str:
    """Notification settings of one human as short text (CLI and MCP)."""
    tz = data.get("timezone") or "UTC"
    changed = {"pause": _("All messages muted."), "resume": _("Messages unmuted."),
               "reviews_off": _("Review requests muted."), "reviews_on": _("Review requests unmuted.")}
    lines = [changed[data["changed"]]] if data.get("changed") in changed else []
    lines.append(_("Notifications of %(name)s:", name=data.get("user")))
    lines.append("  " + _("All messages: %(state)s", state=_mute_line(data.get("all") or {}, tz)))
    lines.append("  " + _("Review requests: %(state)s", state=_mute_line(data.get("reviews") or {}, tz)))
    if data.get("quiet_hours"):
        lines.append("  " + _("Quiet hours: %(hours)s (%(tz)s)", hours=data["quiet_hours"], tz=tz))
    if not data.get("telegram_linked"):
        lines.append("  " + _("Not linked with Telegram – nothing is sent until the profile links a chat."))
    elif not data.get("bot_enabled"):
        lines.append("  " + _("The Telegram bot is switched off on the server – nothing is sent."))
    return "\n".join(lines)


# --- Result mails (`tbr mail`, MCP send_email/list_emails/list_mail_recipients) ------------------------------------

MAIL_STATUS = {"pending_approval": N_("waiting for a human's approval"), "queued": N_("queued for sending"),
               "sent": N_("sent"), "failed": N_("failed"), "rejected": N_("rejected")}


def send_email(client: "Client", task_nr: str, to: list[str], subject: str, body: str, cc: list[str] | None = None,
               attachments: list[str] | None = None, on_approve: bool = False) -> dict:
    """File a send request; the server checks recipients, data class, size and limits and sends it later."""
    payload = {"to": [a for a in to if a.strip()], "cc": [a for a in cc or [] if a.strip()], "subject": subject,
               "body": body, "attachments": [a for a in attachments or [] if a.strip()], "on_approve": on_approve}
    return client.request("POST", f"/tasks/{task_nr.strip().upper()}/emails", json=payload).json()


def email_line(m: dict) -> str:
    """One request: `#id status – subject → recipients`, plus error or Message-ID."""
    line = _("#%(id)s %(status)s – %(subject)s → %(to)s", id=m.get("id"),
             status=_(MAIL_STATUS.get(m.get("status"), m.get("status") or "?")), subject=m.get("subject"),
             to=", ".join((m.get("to") or []) + (m.get("cc") or [])))
    if m.get("status") == "pending_approval" and m.get("on_approve"):
        line += " " + _("(sent when the task is approved)")
    if m.get("smtp_error"):
        line += "\n    " + _("Error: %(error)s", error=m["smtp_error"])
    if m.get("message_id"):
        line += "\n    " + _("Message-ID: %(id)s", id=m["message_id"])
    for a in m.get("attachments") or []:
        line += "\n    " + _("Attachment %(name)s (%(size)s bytes, SHA-256 %(sha)s)", name=a.get("name"),
                             size=a.get("size"), sha=a.get("sha256"))
    return line


def email_report(m: dict) -> str:
    """Answer to a send request: status and whether a human must approve it."""
    head = (_("Email request saved – a human must approve it before it is sent.") if m.get("status") ==
            "pending_approval" else _("Email request saved and queued – the server sends it shortly."))
    return head + "\n" + email_line(m)


def emails_report(data: dict) -> str:
    items = data.get("items") or []
    if not items:
        return _("No email requests for %(nr)s.", nr=data.get("task_nr"))
    return "\n".join([_("Email requests of %(nr)s:", nr=data.get("task_nr")), *[f"  {email_line(m)}" for m in items]])


def recipients_report(data: dict) -> str:
    lines = [_("Allowed email recipients of %(project)s:", project=data.get("project"))]
    for r in data.get("recipients") or []:
        mode = _("needs approval") if r.get("approval") else _("without approval")
        lines.append(f"  {r['address']}" + (f" ({r['label']})" if r.get("label") else "") + f" – {mode}")
    if not data.get("recipients"):
        lines.append("  " + _("(none on the list)"))
    lines.append(_("Humans with a TaskBrigade account and a confirmed address may be sent to without approval."))
    if data.get("data_class") != "cloud_ok":
        lines.append(_("Note: results of local-only tasks cannot be sent by email yet."))
    return "\n".join(lines)
