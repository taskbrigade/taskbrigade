"""Pydantic schemas for the REST API."""
import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ActorType = Literal["human", "agent"]
AgentKind = Literal["hermes", "openclaw", "codex", "claude_code", "opencode", "other", "runner"]
Role = Literal["admin", "orchestrator", "worker", "member", "reviewer"]
Runtime = Literal["local", "cloud"]
ModelLocation = Literal["cloud", "local", "none"]  # Where the agent's model runs (`app/services/zones.py`)


class ActorPublic(BaseModel):
    """Actor directory for assignments, without contact details or credentials."""
    model_config = ConfigDict(from_attributes=True)

    name: str
    display_name: str
    actor_type: ActorType
    agent_kind: AgentKind | None
    runtime: Runtime
    model_location: ModelLocation = "cloud"
    role: Role
    owner: str | None = Field(default=None, validation_alias="owner_name")  # Agents: owning human
    is_public: bool = False  # Agents: shown to everyone in assignment choices


class ActorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    display_name: str
    actor_type: ActorType
    agent_kind: AgentKind | None
    runtime: Runtime
    model_location: ModelLocation = "cloud"
    role: Role
    is_active: bool
    api_key_prefix: str | None
    api_key_created_at: datetime | None
    email: str | None
    tailscale_login: str | None
    gitea_username: str | None
    gitea_token_last4: str | None
    gitea_token_checked_at: datetime | None
    gitea_email: str | None
    sftp_username: str | None = None
    sftp_password_last4: str | None = None
    sftp_checked_at: datetime | None = None
    owner: str | None = Field(default=None, validation_alias="owner_name")
    is_public: bool = False
    lease_minutes: int
    max_parallel_tasks: int
    notes: str | None
    last_seen_at: datetime | None
    created_at: datetime
    # Model choice (`app/services/model_choice.py`); stored as JSON text in the actors table.
    model_default: str | None = None
    model_tiers: dict[str, str] = {}
    models_allowed: list[str] = []

    @field_validator("model_tiers", "models_allowed", mode="before")
    @classmethod
    def _json_text(cls, value, info):
        empty = {} if info.field_name == "model_tiers" else []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return empty
        return value if isinstance(value, type(empty)) else empty


class ActorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)
    actor_type: ActorType
    agent_kind: AgentKind | None = None
    role: Role = "worker"
    runtime: Runtime = "local"
    model_location: ModelLocation = "cloud"
    lease_minutes: int = 120
    max_parallel_tasks: int = 2
    notes: str | None = None
    email: str | None = None
    password: str | None = Field(default=None, repr=False)
    tailscale_login: str | None = None
    owner: str | None = None  # Agents: owning human; missing = the creating admin (if human), "none" = no owner
    is_public: bool = False


class ActorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    role: Role | None = None
    runtime: Runtime | None = None
    model_location: ModelLocation | None = None
    agent_kind: AgentKind | None = None
    lease_minutes: int | None = None
    max_parallel_tasks: int | None = None
    notes: str | None = None
    email: str | None = None
    tailscale_login: str | None = None
    owner: str | None = None  # Agents: human name; null/"none" removes the owner
    is_public: bool | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, repr=False)
    model_default: str | None = None
    model_tiers: dict[str, str | None] | None = None  # tier → model; missing tiers are cleared
    models_allowed: list[str] | None = None


class ActorWithToken(BaseModel):
    actor: ActorOut
    token: str


class TokenOut(BaseModel):
    token: str
    prefix: str


class GiteaCredentialsIn(BaseModel):
    gitea_username: str
    gitea_token: str = Field(repr=False)
    gitea_email: str | None = None


class SftpCredentialsIn(BaseModel):
    sftp_username: str = Field(min_length=1, max_length=100)
    sftp_password: str = Field(min_length=1, max_length=500, repr=False)


class SftpFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SftpUploadIn(BaseModel):
    files: list[SftpFileIn] = Field(min_length=1, max_length=10000)


class JoinIn(BaseModel):
    code: str = Field(min_length=1, max_length=100)


class JoinOut(BaseModel):
    token: str
    actor: ActorOut
    gitea_url: str | None


class JoinCodeOut(BaseModel):
    code: str
    expires_at: datetime


class GitCredentialsOut(BaseModel):
    username: str
    token: str
    email: str | None
    gitea_url: str | None


class DatabaseIn(BaseModel):
    host: str
    port: int
    database: str
    user: str
    password: str | None = Field(default=None, repr=False)
    change_in_db: bool = False


# --- Projects -------------------------------------------------------------------------------

ProjectStatus = Literal["idea", "active", "paused", "archived"]
Privacy = Literal["local_only", "cloud_ok"]
DataClassIn = Literal["cloud_ok", "local_only", "sealed", "inherit"]  # Task data class (`app/services/zones.py`)


class ProjectCreate(BaseModel):
    key: str
    name: str
    description: str | None = None
    status: ProjectStatus = "idea"
    privacy: Privacy = "local_only"
    gitea_mode: Literal["create", "link", "none"] = "none"
    gitea_owner: str | None = None
    gitea_repo: str | None = None
    add_missing_files: bool = True
    protect_branch: bool | None = None
    owner: str | None = None  # Human; defaults to the creator.


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str | None = None
    co_owners: list[str] | None = None  # Further owners with the same rights; replaces the list, [] removes all.
    serialize_reviews: bool | None = None
    data_four_eyes: bool | None = None  # Output gate; send null explicitly to return to the admin default
    default_reviewer: str | None = None  # Human or reviewer agent for new tasks; "" or "none" removes it.
    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    privacy: Privacy | None = None


class ProjectGitea(BaseModel):
    mode: Literal["create", "link"]
    gitea_owner: str | None = None
    gitea_repo: str | None = None
    add_missing_files: bool = True
    protect_branch: bool | None = None
    activate: bool = False


class MemberIn(BaseModel):
    gitea_permission: Literal["read", "write"] = "write"


# --- Tasks ----------------------------------------------------------------------------------

class TaskCreate(BaseModel):
    project: str
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    acceptance_criteria: str | None = None
    prio: int = Field(default=3, ge=1, le=5)
    assigned_to: str | None = None
    parent_task_nr: str | None = None
    depends_on: list[str] = []
    detail_task_git_url: str | None = None
    due_date: date | None = None
    create_spec_file: bool = False
    status: Literal["draft", "defined"] = "defined"
    sprint: int | None = None  # Sprint id; in a planned sprint the task starts as a draft.
    follow_up_of: str | None = None  # Origin task (same project) this task follows up on.
    model_tier: Literal["simple", "standard", "complex"] | None = None
    model: str | None = Field(default=None, max_length=100)  # Must be on the assignee's allow list.
    # Human or reviewer agent; missing/null = the project's default reviewer, "none" = nobody (owner/admins review).
    reviewer: str | None = Field(default=None, max_length=50)
    data_class: DataClassIn | None = None  # Own data class; missing/null/"inherit" = the project's privacy.
    # Runner tasks (data zones): kind `run` needs `run` = {commit_sha, script_path, dataset}, `profile` {dataset}.
    task_kind: Literal["work", "run", "profile"] = "work"
    run: "RunFieldsIn | None" = None


class RunFieldsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: str | None = Field(default=None, max_length=40)  # Merge commit of the reviewed script
    script_path: str | None = Field(default=None, max_length=300)  # Path in the repository, e.g. analysis/x.py
    dataset: str | None = Field(default=None, max_length=16)  # Input dataset key, e.g. DS-0003


class RunResultFileIn(BaseModel):
    name: str = Field(max_length=100)
    kind: Literal["output", "log"]
    path: str = Field(max_length=500)  # Relative to TBR_DATA_DIR, below quarantine/<job_id>/
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)  # Over the plaintext


class RunResultIn(BaseModel):
    """Report of a finished sandbox job (runner → server); no output contents."""
    job_id: str = Field(max_length=64)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    service_result: str | None = Field(default=None, max_length=40)
    peak_memory_mb: int | None = Field(default=None, ge=0)
    dek_wrapped: str | None = Field(default=None, max_length=200)
    files: list[RunResultFileIn] = Field(default_factory=list, max_length=20)
    refused: int = Field(default=0, ge=0)  # Entries in out/ the runner did not take (output gate check 1)
    probe: dict | None = None  # Report of the gate probe job (checks 4 and 5): counts and locations only


class RunFailIn(BaseModel):
    code: str = Field(max_length=40)
    detail: str | None = Field(default=None, max_length=2000)


TaskCreate.model_rebuild()


class TaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    prio: int | None = Field(default=None, ge=1, le=5)
    assigned_to: str | None = None
    due_date: date | None = None
    detail_task_git_url: str | None = None
    sprint: int | None = None  # Sprint id; null detaches.
    parent_task_nr: str | None = None  # null removes the parent task
    depends_on: list[str] | None = None  # replaces the dependencies; [] or null removes them
    model_tier: Literal["simple", "standard", "complex"] | None = None  # null removes the tier
    model: str | None = Field(default=None, max_length=100)  # null removes the concrete model
    reviewer: str | None = Field(default=None, max_length=50)  # null removes the reviewer (owner/admins review)
    data_class: DataClassIn | None = None  # null/"inherit" = inherit the project's privacy
    run: RunFieldsIn | None = None  # Runner fields; only before work started


class DuplicateIn(BaseModel):
    assign: str | None = None  # Actor for the new draft; default: nobody.
    title: str | None = Field(default=None, max_length=200)  # Default: "(duplicate) " + the original title.


class CommentIn(BaseModel):
    type: Literal["comment"] = "comment"
    body: str = Field(min_length=1)
    # Runner note "agent window without activity" (`tbr session`): also notifies the project owner.
    signal: Literal["stalled"] | None = None


class UsageIn(BaseModel):
    """One usage report (research/konzept-workflows.md §7.4): tokens/cost consumed by the agent for the task."""
    input_tokens: int | None = Field(default=None, ge=0, le=4294967295)
    output_tokens: int | None = Field(default=None, ge=0, le=4294967295)
    cost_cents: int | None = Field(default=None, ge=0, le=4294967295)
    model: str | None = Field(default=None, max_length=100)


class HeartbeatIn(BaseModel):
    """Optional body of the heartbeat: conscious waiting for a long background run, and usage."""
    waiting: str | None = Field(default=None, max_length=300)
    for_minutes: int | None = Field(default=None, ge=1)  # Default 120, capped at 1440 (24 h) by the server.
    watch_file: str | None = Field(default=None, max_length=500)
    done_waiting: bool = False
    usage: UsageIn | None = None


class ModelUsedIn(BaseModel):
    """Runner start note: the model passed to the agent CLI (None = the CLI's own default)."""
    model: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=500)


class BodyIn(BaseModel):
    body: str = Field(min_length=1)


class ReleaseIn(BaseModel):
    reason: str = Field(min_length=1)
    # Runner gives the task back because the agent is blocked (login, limit, failed start): notifies the owner.
    signal: Literal["blocked"] | None = None


class SubmitIn(BaseModel):
    branch_name: str
    commit_sha: str
    summary: str = Field(min_length=1)
    open_points: list[str] = []  # Points left for a follow-up task, stored at the task.
    outputs: dict[str, Any] | None = None  # Declared workflow outputs (§3.3). Ignored when the task has none.
    usage: UsageIn | None = None  # Token/cost usage reported on completion (§7.4).


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject", "reject_conflict"]
    comment: str | None = None


class TransitionIn(BaseModel):
    to: str
    comment: str | None = None
    override: bool = False


class ClaimIn(BaseModel):
    task_nr: str | None = None
    # The caller handles review jobs (runner/MCP that know REVIEW.md): a reviewer agent may get one instead of work.
    reviews: bool = False


# --- Sprints --------------------------------------------------------------------------------

class SprintCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class SprintPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    goal: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class SprintClose(BaseModel):
    """Open tasks either move to another sprint of the project or are detached."""
    move_to: int | None = None
    detach: bool = False


class SprintReopen(BaseModel):
    """Reopen a closed sprint; `restore_tasks` moves back the tasks that were moved out when it was closed."""
    restore_tasks: bool = True


class SprintTaskEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    acceptance_criteria: str | None = None
    assigned_to: str | None = None
    prio: int = Field(default=3, ge=1, le=5)
    reviewer: str | None = Field(default=None, max_length=50)  # null = project default, "none" = nobody


class SprintBatch(BaseModel):
    tasks: list[SprintTaskEntry] = Field(min_length=1, max_length=200)


class NotifyChange(BaseModel):
    """`until`: duration (2h, 1d) or clock time (18:00, until 6:30); empty = until switched back."""
    action: Literal["pause", "resume", "reviews_off", "reviews_on"]
    until: str | None = Field(None, max_length=40)
    user: str | None = Field(None, max_length=50)
