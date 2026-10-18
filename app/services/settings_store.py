"""App settings: `app_settings` values override .env defaults.
Secret values are Fernet-encrypted in the DB."""
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import crypto
from app.config import get_settings
from app.errors import Unprocessable
from app.i18n import LANGUAGES, N_, gettext as _
from app.models import AppSetting


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    secret: bool = False
    kind: str = "str"  # str | int | bool | choice
    choices: tuple[str, ...] = ()
    max_int: int = 10080


DEFS: dict[str, SettingDef] = {d.key: d for d in [
    SettingDef("app_url", N_("TaskBrigade URL (for links in PRs/notifications)")),
    SettingDef("ui_language", N_("Default UI language"), kind="choice", choices=tuple(LANGUAGES)),
    SettingDef("gitea_url", N_("Gitea URL")),
    SettingDef("gitea_public_url", N_("Public Gitea URL (for agents and links; empty = Gitea URL)")),
    SettingDef("gitea_token", N_("Gitea service token"), secret=True),
    SettingDef("gitea_admin_token", N_("Gitea admin token (optional: creates Gitea users for new agents)"),
               secret=True),
    SettingDef("gitea_default_owner", N_("Default owner/org")),
    SettingDef("merge_style", N_("Merge style"), kind="choice", choices=("squash", "merge", "rebase", "rebase-merge")),
    SettingDef("branch_protection", N_("Branch protection for new repos"), kind="bool"),
    SettingDef("default_lease_minutes", N_("Default lease (minutes)"), kind="int"),
    SettingDef("join_code_minutes", N_("Join code validity (minutes)"), kind="int"),
    SettingDef("telegram_enabled", N_("Telegram bot enabled (notifications, linking in the profile)"), kind="bool"),
    SettingDef("telegram_bot_token", N_("Telegram bot token (from @BotFather)"), secret=True),
    SettingDef("privacy_enforce_model_location",
               N_("local_only only for agents with a local model (model location instead of runtime)"), kind="bool"),
    SettingDef("data_enabled", N_("Data store enabled (menu Data: encrypted dataset upload)"), kind="bool"),
    SettingDef("data_max_upload_mb", N_("Data store: maximum upload size (MB)"), kind="int", max_int=102400),
    SettingDef("data_retention_days", N_("Data store: default retention (days)"), kind="int", max_int=3650),
    SettingDef("runner_memory_mb", N_("Data runner: memory limit per job (MB)"), kind="int", max_int=262144),
    SettingDef("runner_cpu_percent", N_("Data runner: CPU limit per job (percent, 100 = one core)"), kind="int",
               max_int=6400),
    SettingDef("runner_timeout_s", N_("Data runner: time limit per job (seconds)"), kind="int", max_int=86400),
    # Output gate (`app/services/output_gate.py`, research/konzept-datenzonen.md §6)
    SettingDef("gate_min_group_size", N_("Output gate: minimum group size k (a dataset may set its own)"), kind="int",
               max_int=10000),
    SettingDef("gate_max_rows", N_("Output gate: maximum rows per result table"), kind="int", max_int=100000),
    SettingDef("gate_dominance_pct", N_("Output gate: dominance threshold (percent of a group's sum)"), kind="int",
               max_int=100),
    SettingDef("gate_dominance_hard", N_("Output gate: dominance is a hard check (refuse instead of warn)"),
               kind="bool"),
    SettingDef("data_four_eyes", N_("Output gate: four-eyes rule by default (projects can change it)"), kind="bool"),
    SettingDef("sftp_enabled", N_("SFTP storage enabled (agents upload results with tbr sftp)"), kind="bool"),
    SettingDef("sftp_host", N_("SFTP host (reachable from the agent machines)")),
    SettingDef("sftp_port", N_("SFTP port"), kind="int", max_int=65535),
    SettingDef("sftp_host_key", N_("SFTP host key fingerprint (SHA256:…; “Test SFTP connection” shows it)")),
    SettingDef("sftp_base_dir", N_("SFTP base folder (agents only reach paths inside it)")),
    SettingDef("sftp_path_pattern", N_("SFTP storage pattern per task ({base}, {project}, {task_nr})")),
    # Email notifications (`app/services/mail.py`): outbound SMTP only, TaskBrigade never reads the mailbox.
    SettingDef("email_enabled", N_("Email notifications enabled (outbound only, replies are not read)"), kind="bool"),
    SettingDef("smtp_host", N_("SMTP host (submission server of the mail provider)")),
    SettingDef("smtp_port", N_("SMTP port (587 STARTTLS, 465 TLS)"), kind="int", max_int=65535),
    SettingDef("smtp_security", N_("SMTP encryption"), kind="choice", choices=("starttls", "tls")),
    SettingDef("smtp_user", N_("SMTP user")),
    SettingDef("smtp_password", N_("SMTP password (app password)"), secret=True),
    SettingDef("mail_from_address", N_("Sender address")),
    SettingDef("mail_from_name", N_("Sender name")),
    SettingDef("mail_rate_per_hour", N_("Maximum mails per hour (stay below the provider's limit)"), kind="int",
               max_int=10000),
    SettingDef("mail_max_mb", N_("Result mails: maximum size per mail incl. attachments (MB)"), kind="int",
               max_int=50),
    SettingDef("mail_daily_limit_per_project", N_("Result mails: maximum send requests per project and day"),
               kind="int", max_int=1000),
    SettingDef("workflows_enabled", N_("Workflows enabled"), kind="bool"),
    SettingDef("claim_reminder_hours", N_("Workflow claim reminder (hours)"), kind="int"),
    SettingDef("workflow_paused_max_days", N_("Workflow waiting warning (days)"), kind="int", max_int=3650),
]}


@dataclass(frozen=True)
class SettingGroup:
    """A card on Admin → Settings (one per group navigation entry, `id="settings-<key>"`)."""
    key: str
    title: str
    hint: str
    rows: tuple[tuple[str, ...], ...]  # Setting keys per row; two keys in a row sit side by side.
    toggle: str = ""  # Bool setting that switches the area on; off → "off" pill in the group navigation


# Every key of DEFS sits in exactly one row of one group (tests/test_settings_groups.py).
GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup("general", N_("General"), N_("Values from the database override the .env defaults."),
                 (("app_url",), ("ui_language",))),
    SettingGroup("gitea", N_("Gitea"), N_("Repositories, pull requests and Git access of the agents."),
                 (("gitea_url", "gitea_public_url"), ("gitea_token",), ("gitea_admin_token",),
                  ("gitea_default_owner", "merge_style"), ("branch_protection",))),
    SettingGroup("agents", N_("Agents & leases"), N_("How long a claimed task stays reserved, joining new agents and "
                                                     "the model location rule for local_only projects."),
                 (("default_lease_minutes", "join_code_minutes"), ("privacy_enforce_model_location",))),
    SettingGroup("telegram", N_("Telegram"), N_("Create a bot with @BotFather, enter its token and enable it. Every "
                                                "human links their own chat under Profile."),
                 (("telegram_enabled",), ("telegram_bot_token",)), toggle="telegram_enabled"),
    SettingGroup("email", N_("Email (SMTP)"), N_("TaskBrigade only sends (SMTP submission with STARTTLS or TLS), it "
                                                 "never reads the mailbox. Use a mailbox of its own at the provider "
                                                 "and forward it to an admin for bounces. Every human confirms their "
                                                 "address under Profile."),
                 (("email_enabled",), ("smtp_host", "smtp_port"), ("smtp_security", "smtp_user"), ("smtp_password",),
                  ("mail_from_address", "mail_from_name"), ("mail_rate_per_hour",),
                  ("mail_max_mb", "mail_daily_limit_per_project")), toggle="email_enabled"),
    SettingGroup("data", N_("Data store"), N_("Encrypted dataset upload outside Gitea (menu Data)."),
                 (("data_enabled",), ("data_max_upload_mb", "data_retention_days")), toggle="data_enabled"),
    SettingGroup("runner", N_("Data runner"), N_("Limits for each sandboxed job that reads a dataset."),
                 (("runner_memory_mb", "runner_cpu_percent"), ("runner_timeout_s",))),
    SettingGroup("gate", N_("Output gate"), N_("Checks on result tables before they leave the data zone."),
                 (("gate_min_group_size", "gate_max_rows"), ("gate_dominance_pct",), ("gate_dominance_hard",),
                  ("data_four_eyes",))),
    SettingGroup("sftp", N_("SFTP"), N_("Agents upload results with <code>tbr sftp put</code> into the storage "
                                        "pattern below the base folder; every agent has its own SFTP login (Admin → "
                                        "Users & agents). The host key fingerprint is pinned: “Test SFTP connection” "
                                        "shows it for confirmation."),
                 (("sftp_enabled",), ("sftp_host", "sftp_port"), ("sftp_host_key",),
                  ("sftp_base_dir", "sftp_path_pattern")), toggle="sftp_enabled"),
    SettingGroup("workflows", N_("Workflows"), N_("Repeatable task chains with schedules and reminders."),
                 (("workflows_enabled",), ("claim_reminder_hours", "workflow_paused_max_days")),
                 toggle="workflows_enabled"),
)
GROUP_OF: dict[str, str] = {key: g.key for g in GROUPS for row in g.rows for key in row}


def _env_defaults() -> dict[str, str]:
    s = get_settings()
    return {
        "workflows_enabled": "false",
        "claim_reminder_hours": "4", "workflow_paused_max_days": "30",
        "app_url": "", "ui_language": s.ui_language,
        "gitea_url": s.gitea_url, "gitea_public_url": s.gitea_public_url, "gitea_token": s.gitea_token, "gitea_default_owner": s.gitea_default_owner,
        "gitea_admin_token": "",
        "merge_style": "squash", "branch_protection": "true", "default_lease_minutes": "120", "join_code_minutes": "15",
        "telegram_enabled": "false", "telegram_bot_token": s.notify_telegram_bot_token,
        "privacy_enforce_model_location": "true",  # Existing installations: migration 0020 stores "false".
        "data_enabled": "false", "data_max_upload_mb": "200", "data_retention_days": "90",
        "runner_memory_mb": "4096", "runner_cpu_percent": "200", "runner_timeout_s": "900",
        "gate_min_group_size": "5", "gate_max_rows": "1000", "gate_dominance_pct": "85",
        "gate_dominance_hard": "false", "data_four_eyes": "true",
        "sftp_enabled": "false", "sftp_host": "", "sftp_port": "22", "sftp_host_key": "",
        "sftp_base_dir": "/agent exchange", "sftp_path_pattern": "{base}/{project}/{task_nr}",
        "email_enabled": "false", "smtp_host": "", "smtp_port": "587", "smtp_security": "starttls", "smtp_user": "",
        "smtp_password": "", "mail_from_address": "", "mail_from_name": "TaskBrigade", "mail_rate_per_hour": "60",
        "mail_max_mb": "10", "mail_daily_limit_per_project": "20",
    }


def _fernet():
    return crypto.load_fernet(get_settings().tbr_state_dir)


def get(session: Session, key: str) -> str | None:
    """Get a decrypted value for server-side use only."""
    row = session.get(AppSetting, key)
    if row is not None and row.setting_value is not None:
        return crypto.decrypt(_fernet(), row.setting_value) if row.is_secret else row.setting_value
    return _env_defaults().get(key) or None


def get_bool(session: Session, key: str) -> bool:
    return (get(session, key) or "").lower() in ("1", "true", "yes", "on")


def public_view(session: Session) -> dict[str, dict]:
    """Represent settings for UI/API without exposing secrets in plain text."""
    out = {}
    for key, d in DEFS.items():
        value = get(session, key)
        row = session.get(AppSetting, key)
        source = "db" if row is not None and row.setting_value is not None else ("env" if value else None)
        out[key] = {"label": d.label, "secret": d.secret, "kind": d.kind, "choices": list(d.choices), "group": GROUP_OF[key],
                    "value": ("…" + value[-4:] if value and len(value) > 8 else _("set")) if (d.secret and value)
                    else value, "is_set": bool(value), "source": source}
    return out


def _sftp_host(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9.:\[\]_-]+", value):
        raise ValueError
    return value


def _sftp(name: str):
    def check(value: str) -> str:
        from cli import sftp

        return getattr(sftp, name)(value)
    return check


def _mail_address(value: str) -> str:
    from app.services import mail

    return mail.check_address(value)


# Value check and the hint shown when it fails (the CLI module's own texts follow the CLI language).
MAIL_CHECKS = {
    "smtp_host": (_sftp_host, N_("expected a host name or IP address")),
    "mail_from_address": (_mail_address, N_("expected an email address like taskbrigade@example.org")),
}
SFTP_CHECKS = {
    "sftp_host": (_sftp_host, N_("expected a host name or IP address")),
    "sftp_host_key": (_sftp("normalize_fingerprint"),
                      N_("expected a SHA-256 fingerprint like SHA256:AbC… (ssh-keygen -lf <key>)")),
    "sftp_base_dir": (_sftp("normalize_base"), N_("must be an absolute path without '..', e.g. /agent exchange")),
    "sftp_path_pattern": (_sftp("check_pattern"),
                          N_("must start with {base} and may only use {base}, {project} and {task_nr}")),
}


def _validate(d: SettingDef, value: str) -> str:
    value = value.strip()
    if d.kind == "int":
        if not value.isdigit() or not 1 <= int(value) <= d.max_int:
            raise Unprocessable(_("%(label)s: expected an integer from 1 to %(max)s", label=_(d.label), max=d.max_int))
    elif d.kind == "bool":
        value = "true" if value.lower() in ("1", "true", "yes", "on") else "false"
    elif d.kind == "choice" and value not in d.choices:
        raise Unprocessable(_("%(label)s: allowed values are %(choices)s", label=_(d.label), choices=", ".join(d.choices)))
    elif d.key in ("gitea_url", "gitea_public_url", "app_url") and value and not value.startswith(("http://", "https://")):
        raise Unprocessable(_("%(label)s: must start with http:// or https://", label=_(d.label)))
    elif d.key in (SFTP_CHECKS.keys() | MAIL_CHECKS.keys()) and value:
        check, hint = SFTP_CHECKS.get(d.key) or MAIL_CHECKS[d.key]
        try:
            value = check(value)
        except ValueError as e:
            raise Unprocessable(_("%(label)s: %(error)s", label=_(d.label), error=_(hint))) from e
    return value


def update(session: Session, values: dict[str, str | None], actor_id: int | None) -> list[str]:
    """Set values. `None` deletes an entry and restores its .env default. Return changed keys."""
    changed = []
    for key, raw in values.items():
        d = DEFS.get(key)
        if d is None:
            raise Unprocessable(_("Unknown setting: %(key)s", key=key))
        row = session.get(AppSetting, key)
        if raw is None:
            if row is not None:
                session.delete(row)
                changed.append(key)
            continue
        value = _validate(d, raw)
        stored = crypto.encrypt(_fernet(), value) if d.secret else value
        if row is None:
            row = AppSetting(setting_key=key)
            session.add(row)
        row.setting_value, row.is_secret, row.updated_by = stored, d.secret, actor_id
        changed.append(key)
    session.flush()
    if "ui_language" in changed:
        from app import i18n

        i18n.clear_default_cache()
    return changed


def gitea_client(session: Session):
    from app.services.gitea import GiteaClient

    return GiteaClient(get(session, "gitea_url") or "", get(session, "gitea_token"))


def gitea_public_url(session: Session) -> str:
    """Gitea base URL for agents and browsers: `gitea_public_url`, falling back to `gitea_url`."""
    return (get(session, "gitea_public_url") or get(session, "gitea_url") or "").rstrip("/")


def to_public_url(url: str | None, internal: str | None, public: str | None) -> str | None:
    """Rewrite a URL below the internal Gitea address to the public one; leave everything else unchanged."""
    internal, public = (internal or "").rstrip("/"), (public or "").rstrip("/")
    if not url or not internal or not public or internal == public:
        return url
    if url == internal or url.startswith(internal + "/"):
        return public + url[len(internal):]
    return url


def public_gitea_link(session: Session, url: str | None) -> str | None:
    """`to_public_url` with the configured addresses (repo and PR links returned by the Gitea API)."""
    return to_public_url(url, get(session, "gitea_url"), get(session, "gitea_public_url"))
