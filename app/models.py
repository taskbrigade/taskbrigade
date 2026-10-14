"""SQLAlchemy models. See the SQL in spec section 7; only Alembic migrations create the schema."""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index,
    Integer, SmallInteger, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.mysql import BIGINT, CHAR, INTEGER, MEDIUMTEXT, SMALLINT, TINYINT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TABLE_OPTS = {
    "mariadb_engine": "InnoDB",
    "mariadb_charset": "utf8mb4",
    "mariadb_collate": "utf8mb4_unicode_ci",
}

UInt = Integer().with_variant(INTEGER(unsigned=True), "mariadb", "mysql")
UBigInt = BigInteger().with_variant(BIGINT(unsigned=True), "mariadb", "mysql")
USmallInt = SmallInteger().with_variant(SMALLINT(unsigned=True), "mariadb", "mysql")
UTinyInt = SmallInteger().with_variant(TINYINT(unsigned=True), "mariadb", "mysql")
MediumText = Text().with_variant(MEDIUMTEXT(), "mariadb", "mysql")


def Char(n: int):
    return String(n).with_variant(CHAR(n), "mariadb", "mysql")


NOW = text("CURRENT_TIMESTAMP")
NOW_ON_UPDATE = text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")

TASK_STATUSES = ("draft", "defined", "in_progress", "questions", "aborted", "for_review",
                 "changes_requested", "done", "cancelled")
PROJECT_STATUSES = ("idea", "active", "paused", "archived")
AGENT_KINDS = ("hermes", "openclaw", "codex", "claude_code", "opencode", "other")
# member: human with rights only in own projects; reviewer: agent that reviews tasks it is entered for as reviewer
# (`app/services/reviews.py`) and works on tasks like a worker.
ROLES = ("admin", "orchestrator", "worker", "member", "reviewer")
COMMENT_TYPES = ("comment", "question", "answer", "review", "system")
SPRINT_STATUSES = ("planned", "released", "closed")
MODEL_TIERS = ("simple", "standard", "complex")  # Task model tier; the agent maps it to a model.


class Base(DeclarativeBase):
    __table_args__ = TABLE_OPTS


class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(Enum("human", "agent", name="actor_type"))
    agent_kind: Mapped[str | None] = mapped_column(Enum(*AGENT_KINDS, name="agent_kind"))
    runtime: Mapped[str] = mapped_column(Enum("local", "cloud", name="runtime"), server_default="local")
    role: Mapped[str] = mapped_column(Enum(*ROLES, name="role"), server_default="worker")
    api_key_hash: Mapped[str | None] = mapped_column(Char(64))
    api_key_prefix: Mapped[str | None] = mapped_column(Char(8))
    api_key_created_at: Mapped[datetime | None] = mapped_column(DateTime)
    email: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    tailscale_login: Mapped[str | None] = mapped_column(String(200), unique=True)
    gitea_username: Mapped[str | None] = mapped_column(String(100), unique=True)
    gitea_token_enc: Mapped[str | None] = mapped_column(Text)
    gitea_token_last4: Mapped[str | None] = mapped_column(Char(4))
    gitea_token_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    gitea_email: Mapped[str | None] = mapped_column(String(200))
    # SFTP storage (`app/services/sftp.py`): own SFTP login per agent, password Fernet-encrypted like Gitea tokens.
    sftp_username: Mapped[str | None] = mapped_column(String(100), unique=True)
    sftp_password_enc: Mapped[str | None] = mapped_column(Text)
    sftp_password_last4: Mapped[str | None] = mapped_column(Char(4))
    sftp_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_minutes: Mapped[int] = mapped_column(USmallInt, server_default="120")
    max_parallel_tasks: Mapped[int] = mapped_column(UTinyInt, server_default="2")
    # Model choice for runner agents (`app/services/model_choice.py`): default model, model per tier (JSON object
    # tier → model) and the models a task may name explicitly (JSON list). NULL = none.
    model_default: Mapped[str | None] = mapped_column(String(100))
    model_tiers: Mapped[str | None] = mapped_column(Text)
    models_allowed: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    # Web UI display preference of a human (profile "Show all projects"): off = lists show only own projects.
    show_all_projects: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    # Agents: owning human and "public" (`access.may_use_agent`): assignment choices show a human only own and public
    # agents. Humans: admin profile switch "Show all agents" (display preference for the choices only).
    owner_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_actors_owner", ondelete="SET NULL"))
    is_public: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    owner: Mapped["Actor | None"] = relationship(remote_side=[id], foreign_keys=[owner_id])
    show_all_agents: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)

    @property
    def owner_name(self) -> str | None:
        return self.owner.name if self.owner_id and self.owner else None


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("gitea_owner", "gitea_repo", name="uq_projects_gitea"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    project_key: Mapped[str] = mapped_column(String(8), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Enum(*PROJECT_STATUSES, name="project_status"), server_default="idea")
    privacy: Mapped[str] = mapped_column(Enum("local_only", "cloud_ok", name="privacy"), server_default="local_only")
    gitea_owner: Mapped[str | None] = mapped_column(String(100))
    gitea_repo: Mapped[str | None] = mapped_column(String(100))
    gitea_url: Mapped[str | None] = mapped_column(String(500))
    default_branch: Mapped[str] = mapped_column(String(100), server_default="main")
    brain_path: Mapped[str] = mapped_column(String(200), server_default="brain.md")
    next_task_seq: Mapped[int] = mapped_column(UInt, server_default="1")
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_projects_created_by"))
    # Responsible human: approves, answers questions, and manages this project like an admin.
    owner_id: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_projects_owner"))
    # Per project: an agent claims nothing new here while one of its tasks waits for review (avoids
    # parallel branches touching the same files)
    serialize_reviews: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    # Reviewer new tasks get (human or reviewer agent); NULL = the owner/admins review as before.
    default_reviewer_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_projects_default_reviewer"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("projects.id", name="fk_pm_project", ondelete="CASCADE"), primary_key=True)
    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_pm_actor", ondelete="CASCADE"), primary_key=True)
    gitea_permission: Mapped[str] = mapped_column(Enum("read", "write", name="gitea_permission"),
                                                  server_default="write")
    gitea_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    sync_error: Mapped[str | None] = mapped_column(Text)


class AppSetting(Base):
    __tablename__ = "app_settings"

    setting_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    setting_value: Mapped[str | None] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    updated_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_settings_actor"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)


class Sprint(Base):
    """Optional level between project and task. Tasks in a `planned` sprint are drafts; releasing the sprint
    publishes every complete draft (`app/services/sprints.py`)."""
    __tablename__ = "sprints"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_sprints_project_name"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_sprints_project"))
    name: Mapped[str] = mapped_column(String(120))
    goal: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Enum(*SPRINT_STATUSES, name="sprint_status"), server_default="planned")
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_sprints_created_by"))
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("prio BETWEEN 1 AND 5", name="chk_tasks_prio"),
        Index("idx_tasks_pick", "assigned_to", "status", "prio", "created_at"),
        Index("idx_tasks_project_status", "project_id", "status"),
        Index("idx_tasks_lease", "status", "lease_expires_at"),
        Index("idx_tasks_sprint", "sprint_id", "status"),
        Index("idx_tasks_review", "reviewer_id", "status"),
        TABLE_OPTS,
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    task_nr: Mapped[str] = mapped_column(String(16), unique=True)
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_tasks_project"))
    parent_task_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_tasks_parent"))
    sprint_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("sprints.id", name="fk_tasks_sprint"))
    # Position in the sprint editor (display order); NULL = not ordered, sorted after ordered tasks.
    sprint_rank: Mapped[int | None] = mapped_column(USmallInt)
    # Follow-up task: created from the origin task, e.g. for open points left after a review.
    follow_up_of_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_tasks_follow_up"))
    # Duplicate: copied from the origin task as a new draft (e.g. for a second opinion from another agent).
    duplicate_of_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_tasks_duplicate"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(MediumText)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Enum(*TASK_STATUSES, name="task_status"), server_default="defined")
    prio: Mapped[int] = mapped_column(UTinyInt, server_default="3")
    assigned_to: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_tasks_assigned"))
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_tasks_creator"))
    detail_task_git_url: Mapped[str | None] = mapped_column(String(500))
    branch_name: Mapped[str | None] = mapped_column(String(200))
    pr_number: Mapped[int | None] = mapped_column(UInt)
    pr_url: Mapped[str | None] = mapped_column(String(500))
    result_commit_sha: Mapped[str | None] = mapped_column(Char(40))
    result_summary: Mapped[str | None] = mapped_column(Text)
    open_points: Mapped[str | None] = mapped_column(Text)  # JSON list of strings from submit
    due_date: Mapped[date | None] = mapped_column(Date)
    # Optional model choice: tier (mapped by the assigned agent) and/or a concrete model from its allow list.
    model_tier: Mapped[str | None] = mapped_column(Enum(*MODEL_TIERS, name="model_tier"))
    model: Mapped[str | None] = mapped_column(String(100))
    # Who reviews (`app/services/reviews.py`): a human or an agent with role reviewer; NULL = owner/admins. Owner and
    # admins can always review. A reviewer agent claims the review (review job) with its own lease.
    reviewer_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_tasks_reviewer"))
    review_claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Conscious waiting for a long background run (`tbr heartbeat --waiting`): valid until `waiting_until`; the runner
    # reports no stall meanwhile, `waiting_file` (on the agent machine) counts as activity.
    waiting_reason: Mapped[str | None] = mapped_column(String(300))
    waiting_until: Mapped[datetime | None] = mapped_column(DateTime)
    waiting_file: Mapped[str | None] = mapped_column(String(500))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)
    row_version: Mapped[int] = mapped_column(UInt, server_default="1")

    project: Mapped[Project] = relationship()
    assignee: Mapped[Actor | None] = relationship(foreign_keys=[assigned_to])

    def is_waiting(self, now: datetime) -> bool:
        return bool(self.waiting_reason and self.waiting_until and self.waiting_until > now)

    def clear_waiting(self) -> None:
        self.waiting_reason = self.waiting_until = self.waiting_file = None


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_dep_task", ondelete="CASCADE"), primary_key=True)
    depends_on_task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_dep_on", ondelete="CASCADE"), primary_key=True)


class TaskComment(Base):
    __tablename__ = "task_comments"
    __table_args__ = (Index("idx_comments_task", "task_id", "created_at"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_comments_task", ondelete="CASCADE"))
    author_id: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_comments_author"))
    comment_type: Mapped[str] = mapped_column(Enum(*COMMENT_TYPES, name="comment_type"), server_default="comment")
    body: Mapped[str] = mapped_column(MediumText)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("idx_events_task", "task_id", "created_at"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_events_task", ondelete="CASCADE"))
    actor_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_events_actor"))
    event_type: Mapped[str] = mapped_column(String(40))
    from_value: Mapped[str | None] = mapped_column(String(100))
    to_value: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


class TaskAttachment(Base):
    """Task file stored in the project repo under tasks/<task_nr>/<filename> on the default branch."""
    __tablename__ = "task_attachments"
    __table_args__ = (UniqueConstraint("task_id", "filename", name="uq_attachment_name"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_attachments_task", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(200))
    repo_path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(UInt)
    commit_sha: Mapped[str | None] = mapped_column(Char(40))
    uploaded_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_attachments_actor"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    color: Mapped[str] = mapped_column(Char(7), server_default="#888888")


class TaskLabel(Base):
    __tablename__ = "task_labels"

    task_id: Mapped[int] = mapped_column(UInt, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    label_id: Mapped[int] = mapped_column(UInt, ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True)


class JoinCode(Base):
    """One-time code for `tbr join`: exchanged once for a fresh API token of `actor_id`. Only the SHA-256 is stored."""
    __tablename__ = "join_codes"

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_join_codes_actor", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(Char(64), unique=True)
    created_by: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_join_codes_creator", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_from: Mapped[str | None] = mapped_column(String(64))


class TelegramLink(Base):
    """Telegram chat of a human (`/start <code>` to the bot). One chat per human; unlinking deletes the row."""
    __tablename__ = "telegram_links"

    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_telegram_links_actor", ondelete="CASCADE"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    chat_name: Mapped[str | None] = mapped_column(String(100))
    linked_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


class TelegramLinkCode(Base):
    """One-time code from the profile for `/start <code>`. Only the SHA-256 is stored."""
    __tablename__ = "telegram_link_codes"

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_telegram_codes_actor", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(Char(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationPref(Base):
    """Notification settings of a human. No row = defaults (all events, no quiet hours, not muted)."""
    __tablename__ = "notification_prefs"

    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_notification_prefs_actor", ondelete="CASCADE"), primary_key=True)
    events: Mapped[str | None] = mapped_column(Text)  # JSON list of enabled events; NULL = all
    quiet_start: Mapped[int | None] = mapped_column(USmallInt)  # Minutes after midnight (TZ_DISPLAY)
    quiet_end: Mapped[int | None] = mapped_column(USmallInt)
    # Muted until this time; `notifications.FOREVER` = permanently. Separate for all messages and review requests.
    mute_all_until: Mapped[datetime | None] = mapped_column(DateTime)
    mute_review_until: Mapped[datetime | None] = mapped_column(DateTime)
    all_projects: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))  # Admins: every project
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)


NOTIFICATION_STATUSES = ("queued", "held", "sent", "failed", "skipped")


class Notification(Base):
    """Outbox and delivery log. `text` is cleared once the message is sent or given up – the log keeps no
    content."""
    __tablename__ = "notifications"
    __table_args__ = (Index("idx_notifications_due", "status", "next_attempt_at"),
                      Index("idx_notifications_created", "created_at"),
                      Index("idx_notifications_message", "actor_id", "telegram_message_id"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_notifications_actor", ondelete="CASCADE"))
    event: Mapped[str] = mapped_column(String(40))
    task_nr: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(Enum(*NOTIFICATION_STATUSES, name="notification_status"),
                                        server_default="queued")
    text: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(UTinyInt, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Telegram message id once sent: a reply to a question message answers that question.
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)


class TelegramAction(Base):
    """Token behind an inline button (callback data `tb:<token>`, only the SHA-256 is stored) or a pending reply
    (rejection reason). All tokens of one keyboard share `keyboard`; using one uses them all, so double clicks and
    a second button of the same message do nothing."""
    __tablename__ = "telegram_actions"
    __table_args__ = (Index("idx_telegram_actions_keyboard", "keyboard"),
                      Index("idx_telegram_actions_actor", "actor_id", "kind", "used_at"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(Char(64), unique=True)
    keyboard: Mapped[str] = mapped_column(Char(16))
    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_telegram_actions_actor", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    task_nr: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[str | None] = mapped_column(String(200))


class QuestionRead(Base):
    """A human opened an agent question in the browser (local_only questions can be answered from Telegram only
    after that, because Telegram never shows their text)."""
    __tablename__ = "question_reads"

    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_question_reads_actor", ondelete="CASCADE"), primary_key=True)
    comment_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("task_comments.id", name="fk_question_reads_comment", ondelete="CASCADE"),
        primary_key=True)
    read_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
