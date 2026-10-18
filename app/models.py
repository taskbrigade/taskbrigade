"""SQLAlchemy models. See the SQL in spec section 7; only Alembic migrations create the schema."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index,
    Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, INTEGER, MEDIUMTEXT, SMALLINT, TINYINT, VARBINARY
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
AGENT_KINDS = ("hermes", "openclaw", "codex", "claude_code", "opencode", "other", "runner")
# member: human with rights only in own projects; reviewer: agent that reviews tasks it is entered for as reviewer
# (`app/services/reviews.py`) and works on tasks like a worker.
ROLES = ("admin", "orchestrator", "worker", "member", "reviewer")
COMMENT_TYPES = ("comment", "question", "answer", "review", "system")
SPRINT_STATUSES = ("planned", "released", "closed")
MODEL_TIERS = ("simple", "standard", "complex")  # Task model tier; the agent maps it to a model.
TASK_KINDS = ("work", "profile", "run", "approval")
DATA_CLASSES = ("cloud_ok", "local_only", "sealed")


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
    # Where content goes for inference (`app/services/zones.py`); `runtime` is only where the process runs.
    model_location: Mapped[str] = mapped_column(Enum("cloud", "local", "none", name="model_location"),
                                                server_default="cloud")
    role: Mapped[str] = mapped_column(Enum(*ROLES, name="role"), server_default="worker")
    api_key_hash: Mapped[str | None] = mapped_column(Char(64))
    api_key_prefix: Mapped[str | None] = mapped_column(Char(8))
    api_key_created_at: Mapped[datetime | None] = mapped_column(DateTime)
    email: Mapped[str | None] = mapped_column(String(200))
    # Set by the profile's one-time link (`app/services/mail.py`); cleared when `email` changes. No mail without it.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
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
    # Humans: data right (research/konzept-datenzonen.md §3) – upload datasets as project member, work on `sealed`
    # tasks, emergency download (admins). Set under Admin → Users & agents; changes are written to `data_audit`.
    data_access: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
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
    # Output gate (research/konzept-datenzonen.md §6): a result needs a releaser who neither created the run task nor
    # merged the script. NULL = the admin default `data_four_eyes`.
    data_four_eyes: Mapped[bool | None] = mapped_column(Boolean)
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
    # JSON `{ids: [...], target: <sprint id or null>}`: the tasks `close` moved out of this sprint (to `target`,
    # null = detached). `reopen` moves exactly those tasks back – a sprint name is only unique per project, and a
    # task moved out by hand before the close must not be restored. NULL for sprints closed before migration 0039
    # (reopen then falls back to the `sprint` task events). Cleared by `reopen` (`app/services/sprints.py`).
    moved_task_ids: Mapped[str | None] = mapped_column(Text)
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
        Index("idx_tasks_workflow_run_step", "workflow_run_step_id"),
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
    workflow_run_step_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("workflow_run_steps.id", name="fk_tasks_workflow_run_step", use_alter=True))
    task_kind: Mapped[str] = mapped_column(Enum(*TASK_KINDS, name="task_kind"), server_default="work")
    # Own data class (`app/services/zones.py`); NULL inherits the project's privacy.
    data_class: Mapped[str | None] = mapped_column(Enum(*DATA_CLASSES, name="data_class"))
    # Runner tasks (`task_kind` run, `app/services/runner_jobs.py`): merged commit and path of the script, input
    # dataset (until `task_data_refs` exists). Never shown to cloud agents beyond the dataset key.
    run_commit_sha: Mapped[str | None] = mapped_column(Char(40))
    run_script_path: Mapped[str | None] = mapped_column(String(300))
    run_dataset_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("datasets.id", name="fk_tasks_run_dataset", ondelete="SET NULL", use_alter=True))
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
    merge_commit_sha: Mapped[str | None] = mapped_column(Char(40))
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
    workflow_run_step: Mapped["WorkflowRunStep | None"] = relationship(foreign_keys=[workflow_run_step_id])

    def is_waiting(self, now: datetime) -> bool:
        return bool(self.waiting_reason and self.waiting_until and self.waiting_until > now)

    def clear_waiting(self) -> None:
        self.waiting_reason = self.waiting_until = self.waiting_file = None


class WorkflowTemplate(Base):
    __tablename__ = "workflow_templates"
    __table_args__ = (UniqueConstraint("project_id", "workflow_key", name="uq_workflow_templates_key"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_wft_project"))
    workflow_key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wft_owner"))
    current_version_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("workflow_template_versions.id", name="fk_wft_current_version", use_alter=True))
    status: Mapped[str] = mapped_column(Enum("active", "paused", "archived", name="workflow_template_status"),
                                      server_default="active")
    default_budget_tokens: Mapped[int | None] = mapped_column(UInt)
    default_budget_cents: Mapped[int | None] = mapped_column(UInt)
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wft_creator"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)

    project: Mapped[Project] = relationship()
    owner: Mapped[Actor] = relationship(foreign_keys=[owner_id])
    current_version: Mapped["WorkflowTemplateVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True)
    versions: Mapped[list["WorkflowTemplateVersion"]] = relationship(
        back_populates="template", foreign_keys="WorkflowTemplateVersion.template_id", passive_deletes="all")


class WorkflowScriptPin(Base):
    __tablename__ = "workflow_script_pins"
    __table_args__ = (Index("idx_wsp_active", "template_id", "step_key", "retired_at"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(UInt, ForeignKey("workflow_templates.id", name="fk_wsp_template"))
    step_key: Mapped[str] = mapped_column(String(40))
    repo_path: Mapped[str] = mapped_column(String(300))
    commit_sha: Mapped[str] = mapped_column(Char(40))
    schema_fingerprint: Mapped[str | None] = mapped_column(Char(64))
    profile_id: Mapped[int] = mapped_column(UInt, ForeignKey("dataset_profiles.id", name="fk_wsp_profile"))
    source_task_id: Mapped[int] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_wsp_task"))
    pinned_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wsp_actor"))
    pinned_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)


class WorkflowTemplateVersion(Base):
    __tablename__ = "workflow_template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_workflow_versions_number"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("workflow_templates.id", name="fk_wfv_template", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(USmallInt)
    state: Mapped[str] = mapped_column(Enum("draft", "published", "retired", name="workflow_version_state"),
                                     server_default="draft")
    definition_sha256: Mapped[str | None] = mapped_column(Char(64))
    change_note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wfv_creator"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wfv_publisher"))

    template: Mapped[WorkflowTemplate] = relationship(back_populates="versions", foreign_keys=[template_id])
    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="version", order_by="WorkflowStep.position", passive_deletes="all")
    params: Mapped[list["WorkflowParam"]] = relationship(back_populates="version", passive_deletes="all")


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("version_id", "step_key", name="uq_workflow_steps_key"),
        UniqueConstraint("version_id", "position", name="uq_workflow_steps_position"), TABLE_OPTS,
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("workflow_template_versions.id", name="fk_wfs_version", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(USmallInt)
    step_key: Mapped[str] = mapped_column(String(40))
    step_type: Mapped[str] = mapped_column(
        Enum("agent", "script", "runner", "approval", "gate", "notify", name="workflow_step_type"))
    title_tpl: Mapped[str] = mapped_column(String(200))
    description_tpl: Mapped[str | None] = mapped_column(MediumText)
    acceptance_tpl: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wfs_assignee"))
    agent_role: Mapped[str | None] = mapped_column(String(40))
    data_class: Mapped[str | None] = mapped_column(Enum(*DATA_CLASSES, name="workflow_data_class"))
    review_mode: Mapped[str] = mapped_column(Enum("manual", "agent", "auto", name="workflow_review_mode"),
                                          server_default="manual")
    reviewer_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wfs_reviewer"))
    model_tier: Mapped[str | None] = mapped_column(Enum(*MODEL_TIERS, name="model_tier"))
    timeout_minutes: Mapped[int | None] = mapped_column(UInt)
    wait_reminder_hours: Mapped[int | None] = mapped_column(USmallInt)
    max_retries: Mapped[int] = mapped_column(UTinyInt, server_default="0")
    retry_backoff_s: Mapped[int] = mapped_column(UInt, server_default="300")
    optional: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    parallel_group: Mapped[str | None] = mapped_column(String(40))
    condition_tpl: Mapped[str | None] = mapped_column(Text)
    inputs_json: Mapped[str | None] = mapped_column(Text)
    outputs_json: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[str | None] = mapped_column(Text)

    version: Mapped[WorkflowTemplateVersion] = relationship(back_populates="steps")
    assignee: Mapped[Actor | None] = relationship(foreign_keys=[assignee_id])
    reviewer: Mapped[Actor | None] = relationship(foreign_keys=[reviewer_id])


class WorkflowParam(Base):
    __tablename__ = "workflow_params"
    __table_args__ = (UniqueConstraint("version_id", "name", name="uq_workflow_params_name"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("workflow_template_versions.id", name="fk_wfp_version", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(40))
    param_type: Mapped[str] = mapped_column(
        Enum("string", "text", "int", "decimal", "bool", "date", "month", "enum", "dataset", "task",
             name="workflow_param_type"))
    required: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    default_expr: Mapped[str | None] = mapped_column(String(200))
    enum_values: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(120))
    help: Mapped[str | None] = mapped_column(String(300))

    version: Mapped[WorkflowTemplateVersion] = relationship(back_populates="params")


class WorkflowRoleBinding(Base):
    __tablename__ = "workflow_role_bindings"
    __table_args__ = (
        UniqueConstraint("project_id", "role", "actor_id", name="uq_workflow_role_bindings_actor"), TABLE_OPTS,
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_wfrb_project"))
    role: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wfrb_actor"))
    rank: Mapped[int] = mapped_column(UTinyInt, server_default="1")

    project: Mapped[Project] = relationship()
    actor: Mapped[Actor] = relationship()


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("template_id", "run_seq", name="uq_workflow_runs_seq"),
        UniqueConstraint("schedule_id", "scheduled_for", name="uq_workflow_runs_slot"),
        Index("idx_workflow_runs_status", "status"),
        Index("idx_workflow_runs_project_created", "project_id", "created_at"), TABLE_OPTS,
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(UInt, ForeignKey("workflow_templates.id", name="fk_wfr_template"))
    version_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("workflow_template_versions.id", name="fk_wfr_version"))
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_wfr_project"))
    run_seq: Mapped[int] = mapped_column(UInt)
    trigger_kind: Mapped[str] = mapped_column(
        Enum("manual", "api", "schedule", "event", "workflow", name="workflow_trigger_kind"))
    schedule_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("workflow_schedules.id", name="fk_wfr_schedule"))
    schedule_timezone: Mapped[str | None] = mapped_column(String(40))
    trigger_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("workflow_triggers.id", name="fk_wfr_trigger", ondelete="SET NULL"))
    parent_run_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("workflow_runs.id", name="fk_wfr_parent"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    started_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wfr_starter"))
    params_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Enum("queued", "running", "waiting", "paused", "succeeded", "failed", "cancelled", name="workflow_run_status"))
    current_position: Mapped[int | None] = mapped_column(USmallInt)
    budget_tokens: Mapped[int | None] = mapped_column(UInt)
    budget_cents: Mapped[int | None] = mapped_column(UInt)
    used_tokens: Mapped[int] = mapped_column(UInt, server_default="0")
    used_cents: Mapped[int] = mapped_column(UInt, server_default="0")
    pause_reason: Mapped[str | None] = mapped_column(String(40))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)

    template: Mapped[WorkflowTemplate] = relationship()
    version: Mapped[WorkflowTemplateVersion] = relationship()
    project: Mapped[Project] = relationship()
    parent_run: Mapped["WorkflowRun | None"] = relationship(remote_side=[id])
    steps: Mapped[list["WorkflowRunStep"]] = relationship(
        back_populates="run", order_by="WorkflowRunStep.position, WorkflowRunStep.attempt", passive_deletes="all")


class WorkflowRunStep(Base):
    __tablename__ = "workflow_run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "attempt", name="uq_workflow_run_steps_attempt"), TABLE_OPTS,
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("workflow_runs.id", name="fk_wfrs_run", ondelete="CASCADE"))
    step_id: Mapped[int] = mapped_column(UInt, ForeignKey("workflow_steps.id", name="fk_wfrs_step"))
    position: Mapped[int] = mapped_column(USmallInt)
    attempt: Mapped[int] = mapped_column(UTinyInt, server_default="1")
    status: Mapped[str] = mapped_column(
        Enum("pending", "active", "waiting", "succeeded", "failed", "skipped", "cancelled",
             name="workflow_run_step_status"))
    task_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_wfrs_task"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    work_us: Mapped[int] = mapped_column(UBigInt, server_default="0")
    work_started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    wait_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    idle_since: Mapped[datetime | None] = mapped_column(DateTime)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime)
    idle_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    assignee_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    output_json: Mapped[str | None] = mapped_column(MediumText)
    output_class: Mapped[str | None] = mapped_column(Enum(*DATA_CLASSES, name="workflow_output_class"))
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_detail: Mapped[str | None] = mapped_column(String(500))

    run: Mapped[WorkflowRun] = relationship(back_populates="steps")
    step: Mapped[WorkflowStep] = relationship()
    task: Mapped[Task | None] = relationship(foreign_keys=[task_id])


USAGE_SOURCES = ("agent", "runner_estimate", "manual")


class TaskUsage(Base):
    """One usage report for a task (research/konzept-workflows.md §1.5/§7.4): tokens and cost reported by an agent
    (or estimated by the runner / entered by a human). Rows count towards the task's workflow run budget."""
    __tablename__ = "task_usage"
    __table_args__ = (Index("idx_task_usage_task", "task_id", "reported_at"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_task_usage_task", ondelete="CASCADE"))
    actor_id: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_task_usage_actor"))
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(UInt, server_default="0")
    output_tokens: Mapped[int] = mapped_column(UInt, server_default="0")
    cost_cents: Mapped[int | None] = mapped_column(UInt)
    source: Mapped[str] = mapped_column(Enum(*USAGE_SOURCES, name="task_usage_source"), server_default="agent")
    reported_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_dep_task", ondelete="CASCADE"), primary_key=True)
    depends_on_task_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_dep_on", ondelete="CASCADE"), primary_key=True)


DATA_REF_TYPES = ("dataset", "profile", "result")
DATA_REF_PURPOSES = ("input", "schema", "output")


class TaskDataRef(Base):
    """Reference of a task to a data artifact instead of its content (research/konzept-datenzonen.md §1,
    `app/services/data_refs.py`): `dataset` (input, sealed tasks only), `profile` (schema) or `result` (output).
    `ref_id` points to `datasets`/`dataset_profiles`/`run_results` by `ref_type`, so it has no foreign key."""
    __tablename__ = "task_data_refs"
    __table_args__ = (UniqueConstraint("task_id", "ref_type", "ref_id", name="uq_task_data_refs"),
                      Index("idx_task_data_refs_ref", "ref_type", "ref_id"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_task_data_refs_task",
                                                          ondelete="CASCADE"))
    ref_type: Mapped[str] = mapped_column(Enum(*DATA_REF_TYPES, name="data_ref_type"))
    ref_id: Mapped[int] = mapped_column(UInt)
    purpose: Mapped[str] = mapped_column(Enum(*DATA_REF_PURPOSES, name="data_ref_purpose"))
    created_by: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_task_data_refs_creator", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


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
    # JSON {"events": {event: [channel, …]}, "bundle": minutes}; NULL or a missing event = defaults
    # (`notifications.channels_for`).
    channels_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)


NOTIFICATION_STATUSES = ("queued", "held", "sent", "failed", "skipped")
NOTIFICATION_CHANNELS = ("telegram", "email")


class Notification(Base):
    """Outbox and delivery log. `text` is cleared once the message is sent or given up – the log keeps no
    content."""
    __tablename__ = "notifications"
    __table_args__ = (Index("idx_notifications_due", "status", "next_attempt_at"),
                      Index("idx_notifications_created", "created_at"),
                      Index("idx_notifications_message", "actor_id", "telegram_message_id"),
                      Index("idx_notifications_channel_sent", "channel", "sent_at"), TABLE_OPTS)

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
    # One row per recipient and channel. Email: Message-ID of the mail the row went out in (bundles share one).
    channel: Mapped[str] = mapped_column(Enum(*NOTIFICATION_CHANNELS, name="notification_channel"),
                                         server_default="telegram")
    email_message_id: Mapped[str | None] = mapped_column(String(200))
    # Inbox notices feed: when the human read this notification in the browser (None = unread).
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class EmailVerifyCode(Base):
    """One-time link that confirms `actors.email` (24 h). Only the SHA-256 is stored; `email` is the address the
    link was sent to, so a link for an old address does not verify a new one."""
    __tablename__ = "email_verify_codes"
    __table_args__ = (TABLE_OPTS,)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_email_verify_codes_actor", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(200))
    code_hash: Mapped[str] = mapped_column(Char(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


MAIL_RECIPIENT_MODES = ("freigabe", "ohne_freigabe")  # With approval (default) / without approval
OUTGOING_MAIL_STATUSES = ("pending_approval", "queued", "sent", "failed", "rejected")


class ProjectMailRecipient(Base):
    """Address an agent may send work results to (`app/services/result_mail.py`); other addresses are refused."""
    __tablename__ = "project_mail_recipients"
    __table_args__ = (UniqueConstraint("project_id", "address", name="uq_mail_recipients_address"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("projects.id", name="fk_mail_recipients_project", ondelete="CASCADE"))
    address: Mapped[str] = mapped_column(String(200))
    label: Mapped[str | None] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(Enum(*MAIL_RECIPIENT_MODES, name="mail_recipient_mode"),
                                      server_default="freigabe")
    created_by: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_mail_recipients_actor", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


class OutgoingMail(Base):
    """Send request of a work result (`app/services/result_mail.py`). `body` and the attachment references are
    cleared after 30 days (`cleared_at`); recipients, subject, hashes and status stay."""
    __tablename__ = "outgoing_mails"
    __table_args__ = (Index("idx_outgoing_mails_due", "status", "next_attempt_at"),
                      Index("idx_outgoing_mails_project", "project_id", "created_at"),
                      Index("idx_outgoing_mails_sent", "sent_at"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_outgoing_mails_task",
                                                          ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_outgoing_mails_project",
                                                             ondelete="CASCADE"))
    requested_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_outgoing_mails_requester"))
    data_class: Mapped[str | None] = mapped_column(String(16))  # Policy at request time; never loosened later
    to_json: Mapped[str] = mapped_column(Text)  # JSON list of addresses
    cc_json: Mapped[str | None] = mapped_column(Text)
    reply_to: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(MediumText)  # Markdown; NULL once cleared
    # JSON list of {ref, name, size, sha256, content_type}; `ref` is dropped when cleared.
    attachments_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Enum(*OUTGOING_MAIL_STATUSES, name="outgoing_mail_status"),
                                        server_default="pending_approval")
    on_approve: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))  # Send when the task is approved
    approved_by: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_outgoing_mails_approver", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_note: Mapped[str | None] = mapped_column(String(500))  # Rejection reason
    message_id: Mapped[str | None] = mapped_column(String(200))
    smtp_error: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(UTinyInt, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime)


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


DATASET_CLASSES = ("local_only", "sealed")
DATASET_STATUSES = ("receiving", "quarantine", "profiling", "ready", "archived", "deleted")


class Dataset(Base):
    """Encrypted file in the data store (`app/services/datasets.py`, docs/data-store.md). Never in Git; tasks
    refer to it by id only."""
    __tablename__ = "datasets"
    __table_args__ = (Index("idx_datasets_project", "project_id", "status"),
                      Index("idx_datasets_retention", "status", "retention_until"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    dataset_key: Mapped[str] = mapped_column(String(16), unique=True)  # DS-0003
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_datasets_project"))
    name: Mapped[str] = mapped_column(String(200))  # Internal only, never given to cloud agents
    data_class: Mapped[str] = mapped_column(Enum(*DATASET_CLASSES, name="dataset_class"), server_default="sealed")
    status: Mapped[str] = mapped_column(Enum(*DATASET_STATUSES, name="dataset_status"), server_default="receiving")
    original_filename: Mapped[str | None] = mapped_column(String(255))
    media_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(UBigInt, server_default="0")
    sha256: Mapped[str | None] = mapped_column(Char(64))  # Over the plaintext
    storage_path: Mapped[str | None] = mapped_column(String(500))  # Relative to TBR_DATA_DIR
    dek_wrapped: Mapped[bytes | None] = mapped_column(VARBINARY(128))  # NULL after deletion (crypto-shredding)
    supersedes_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("datasets.id", name="fk_datasets_supersedes", ondelete="SET NULL"))
    upload_channel: Mapped[str] = mapped_column(Enum("web", "sftp", name="upload_channel"), server_default="web")
    uploaded_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_datasets_uploaded_by"))
    retention_until: Mapped[date | None] = mapped_column(Date)
    retention_warned_at: Mapped[datetime | None] = mapped_column(DateTime)  # Reminder sent (once)
    min_group_size: Mapped[int | None] = mapped_column(USmallInt)
    gate_policy_json: Mapped[str | None] = mapped_column(Text)
    released_count: Mapped[int] = mapped_column(UInt, server_default="0")
    epsilon_spent: Mapped[Decimal] = mapped_column(Numeric(30, 6), server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_by: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_datasets_deleted_by", ondelete="SET NULL"))


class DatasetProfile(Base):
    __tablename__ = "dataset_profiles"
    __table_args__ = (Index("idx_dataset_profiles_dataset", "dataset_id", "id"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(UInt, ForeignKey("datasets.id", name="fk_profiles_dataset"))
    task_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_profiles_task", ondelete="SET NULL"))
    code_task_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("tasks.id", name="fk_profiles_code_task", ondelete="SET NULL"))
    profiler_version: Mapped[str] = mapped_column(String(20))
    schema_fingerprint: Mapped[str] = mapped_column(Char(64))
    schema_json: Mapped[str] = mapped_column(MediumText)
    sample_csv: Mapped[str] = mapped_column(MediumText)
    aliases_json: Mapped[str | None] = mapped_column(MediumText)
    status: Mapped[str] = mapped_column(Enum("generated", "released", "rejected", name="profile_status"),
                                       server_default="generated")
    released_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("actors.id", name="fk_profiles_releaser"))
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


AUDIT_OBJECT_TYPES = ("dataset", "profile", "result", "task", "actor", "project")
AUDIT_VIA = ("web", "api", "sftp", "runner", "scheduler")


class DataAudit(Base):
    """Append-only data audit with a SHA-256 hash chain (`app/services/data_audit.py`). No foreign keys on purpose:
    a cascading delete or SET NULL would change hashed rows. Triggers refuse UPDATE/DELETE (migration 0024)."""
    __tablename__ = "data_audit"
    __table_args__ = (Index("idx_data_audit_object", "object_type", "object_id"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=False)
    at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    actor_id: Mapped[int | None] = mapped_column(UInt)  # NULL = system/scheduler
    action: Mapped[str] = mapped_column(String(40))
    object_type: Mapped[str] = mapped_column(Enum(*AUDIT_OBJECT_TYPES, name="audit_object_type"))
    object_id: Mapped[int] = mapped_column(UInt)
    task_id: Mapped[int | None] = mapped_column(UInt)
    detail_json: Mapped[str | None] = mapped_column(Text)  # Never data values
    via: Mapped[str] = mapped_column(Enum(*AUDIT_VIA, name="audit_via"))
    prev_hash: Mapped[str] = mapped_column(Char(64))
    hash: Mapped[str] = mapped_column(Char(64))


class DataAuditHead(Base):
    """Single row (id 1) with the end of the `data_audit` chain; appends lock it with `FOR UPDATE`."""
    __tablename__ = "data_audit_head"

    id: Mapped[int] = mapped_column(UTinyInt, primary_key=True, autoincrement=False)
    last_id: Mapped[int] = mapped_column(UBigInt, server_default="0")
    last_hash: Mapped[str] = mapped_column(Char(64))


class DataSftpAccount(Base):
    """Native SFTP inbox (concept §4/§9, migration `0029`): one system user per deliverer, chrooted to
    `/srv/taskbrigade-sftp/<username>` and mapped to exactly one project. The user has no shell; deliveries run
    through OpenSSH `internal-sftp` and the drop-in `60-taskbrigade-sftp.conf`. Created with
    `tbr-admin data sftp-user add …` (`app/services/data_sftp.py`)."""
    __tablename__ = "data_sftp_accounts"
    __table_args__ = (Index("idx_data_sftp_accounts_project", "project_id"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)  # System user, no shell, chrooted
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_data_sftp_accounts_project"))
    name: Mapped[str | None] = mapped_column(String(200))  # Human label, e.g. "Accounting export"
    owner_id: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_data_sftp_accounts_owner"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    last_import_at: Mapped[datetime | None] = mapped_column(DateTime)


class DataSftpKey(Base):
    """Public key allowed to log in as a `data_sftp_accounts` user. Only the base64 key body is stored (no
    comment); the fingerprint is the `authorized_keys` comment and the unique key per account."""
    __tablename__ = "data_sftp_keys"
    __table_args__ = (UniqueConstraint("account_id", "fingerprint", name="uq_data_sftp_keys_fingerprint"),
                      TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("data_sftp_accounts.id", name="fk_data_sftp_keys_account", ondelete="CASCADE"))
    key_type: Mapped[str] = mapped_column(String(32))
    key_blob: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Char(64))
    comment: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)


FIRE_OUTCOMES = ("started", "queued", "skipped_overlap", "skipped_misfire", "failed")


class WorkflowCalendar(Base):
    __tablename__ = "workflow_calendars"
    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(2), server_default="DE")
    subdivision: Mapped[str | None] = mapped_column(String(10))
    workdays: Mapped[str] = mapped_column(String(30), server_default="mon,tue,wed,thu,fri")
    days: Mapped[list["WorkflowCalendarDay"]] = relationship(passive_deletes="all")


class WorkflowCalendarDay(Base):
    __tablename__ = "workflow_calendar_days"
    calendar_id: Mapped[int] = mapped_column(UInt, ForeignKey("workflow_calendars.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(Enum("closed", "open", name="workflow_calendar_day_kind"))
    note: Mapped[str | None] = mapped_column(String(100))


class WorkflowSchedule(Base):
    __tablename__ = "workflow_schedules"
    __table_args__ = (Index("idx_workflow_schedules_due", "enabled", "next_fire_at"), TABLE_OPTS)
    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(UInt, ForeignKey("workflow_templates.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(Enum("cron", "workday", name="workflow_schedule_kind"))
    cron_expr: Mapped[str | None] = mapped_column(String(100))
    workday_rule_json: Mapped[str | None] = mapped_column(String(300))
    timezone: Mapped[str] = mapped_column(String(40), server_default="Europe/Berlin")
    calendar_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("workflow_calendars.id"))
    holiday_shift: Mapped[str] = mapped_column(Enum("skip", "next_workday", "prev_workday", name="workflow_holiday_shift"), server_default="skip")
    params_json: Mapped[str | None] = mapped_column(Text)
    overlap_policy: Mapped[str] = mapped_column(Enum("skip", "queue", name="workflow_overlap_policy"), server_default="skip")
    misfire_policy: Mapped[str] = mapped_column(Enum("run_late", "skip", name="workflow_misfire_policy"), server_default="run_late")
    misfire_grace_min: Mapped[int] = mapped_column(UInt, server_default="360")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_outcome: Mapped[str | None] = mapped_column(Enum(*FIRE_OUTCOMES, name="workflow_schedule_outcome"))
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW_ON_UPDATE)
    template: Mapped[WorkflowTemplate] = relationship()
    calendar: Mapped[WorkflowCalendar | None] = relationship()


class WorkflowScheduleFire(Base):
    __tablename__ = "workflow_schedule_fires"
    __table_args__ = (UniqueConstraint("schedule_id", "scheduled_for", name="uq_workflow_fires_slot"), TABLE_OPTS)
    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(UInt, ForeignKey("workflow_schedules.id", ondelete="CASCADE"))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime)
    fired_at: Mapped[datetime] = mapped_column(DateTime)
    outcome: Mapped[str] = mapped_column(Enum(*FIRE_OUTCOMES, name="workflow_fire_outcome"))
    run_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("workflow_runs.id"))
    note: Mapped[str | None] = mapped_column(String(200))


TRIGGER_KINDS = ("dataset_ready", "workflow_succeeded", "workflow_failed")


class WorkflowTrigger(Base):
    """Event trigger on a template (research/konzept-workflows.md §1.5). Paused templates do not fire."""
    __tablename__ = "workflow_triggers"
    __table_args__ = (Index("idx_workflow_triggers_template", "template_id", "enabled"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("workflow_templates.id", name="fk_wftr_template", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Enum(*TRIGGER_KINDS, name="workflow_trigger_event_kind"))
    filter_json: Mapped[str] = mapped_column(Text)
    params_json: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("actors.id", name="fk_wftr_creator"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)

    template: Mapped[WorkflowTemplate] = relationship()


class WorkflowEvent(Base):
    """Outbox row. The scheduler tick delivers it; producers never start a run themselves."""
    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_workflow_events_dedupe"),
        Index("idx_workflow_events_processed", "processed_at"), TABLE_OPTS,
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40))
    project_id: Mapped[int] = mapped_column(UInt, ForeignKey("projects.id", name="fk_wfe_project"))
    payload_json: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)


class WorkflowTriggerFire(Base):
    """One delivery of one event to one trigger. The unique pair is the exactly-once barrier."""
    __tablename__ = "workflow_trigger_fires"
    __table_args__ = (UniqueConstraint("trigger_id", "event_id", name="uq_workflow_trigger_fires_once"), TABLE_OPTS)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    trigger_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("workflow_triggers.id", name="fk_wftf_trigger", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workflow_events.id", name="fk_wftf_event", ondelete="CASCADE"))
    fired_at: Mapped[datetime] = mapped_column(DateTime)
    outcome: Mapped[str] = mapped_column(Enum(*FIRE_OUTCOMES, name="workflow_trigger_fire_outcome"))
    run_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("workflow_runs.id", name="fk_wftf_run"))
    note: Mapped[str | None] = mapped_column(String(200))


RUN_RESULT_STATUSES = ("quarantine", "checks_failed", "pending_approval", "released", "rejected", "deleted",
                       "kept_sealed")


class RunResult(Base):
    """Outcome of a runner job (research/konzept-datenzonen.md §6). Metadata only: outputs and logs are encrypted below
    `TBR_DATA_DIR/quarantine` (`files_json` lists them, one DEK per result wrapped with the context
    `run-result/<job_id>`). The output gate (`app/services/output_gate.py`) moves `status` on from `quarantine`;
    `released_json` is the only plaintext and exists for `released` results only."""
    __tablename__ = "run_results"
    __table_args__ = (Index("idx_run_results_task", "task_id"), Index("idx_run_results_status", "status"),
                      TABLE_OPTS)

    id: Mapped[int] = mapped_column(UInt, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(UInt, ForeignKey("tasks.id", name="fk_run_results_task",
                                                          ondelete="CASCADE"))
    dataset_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("datasets.id", name="fk_run_results_dataset", ondelete="SET NULL"))
    runner_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("actors.id", name="fk_run_results_runner", ondelete="SET NULL"))
    job_id: Mapped[str] = mapped_column(String(64), unique=True)
    task_kind: Mapped[str] = mapped_column(Enum("profile", "run", name="run_result_kind"), server_default="run")
    script_commit_sha: Mapped[str | None] = mapped_column(Char(40))
    script_path: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    exit_code: Mapped[int | None] = mapped_column(SmallInteger)
    service_result: Mapped[str | None] = mapped_column(String(40))  # systemd: success, exit-code, timeout, …
    peak_memory_mb: Mapped[int | None] = mapped_column(UInt)
    status: Mapped[str] = mapped_column(Enum(*RUN_RESULT_STATUSES, name="run_result_status"),
                                        server_default="quarantine")
    output_path: Mapped[str | None] = mapped_column(String(500))  # result.json, relative to TBR_DATA_DIR
    output_sha256: Mapped[str | None] = mapped_column(Char(64))  # Over the plaintext
    files_json: Mapped[str | None] = mapped_column(Text)
    dek_wrapped: Mapped[bytes | None] = mapped_column(VARBINARY(128))
    checks_json: Mapped[str | None] = mapped_column(Text)
    released_json: Mapped[str | None] = mapped_column(MediumText)
    release_policy_json: Mapped[str | None] = mapped_column(Text)
    approvals_json: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=NOW)
