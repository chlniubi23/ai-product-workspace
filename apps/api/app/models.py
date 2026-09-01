from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from .db import Base

# V1.1 storage contract.  Compatibility ORM classes below remain importable
# during the rolling migration, but new code must use these durable tables.
V11_CORE_TABLE_NAMES = (
    "users",
    "projects",
    "metric_definitions",
    "datasets",
    "dataset_versions",
    "data_columns",
    "data_quality_reports",
    "cleaning_operations",
    "analysis_runs",
    "analysis_artifacts",
    "insights",
    "product_problems",
    "solution_options",
    "decision_proposals",
    "copilot_sessions",
    "copilot_messages",
    "documents",
    "document_versions",
    "ai_runs",
    "feedback_notes",
)
V11_LEGACY_TABLE_NAMES = (
    "workspaces",
    "workspace_members",
    "tasks",
    "task_links",
    "feedback_items",
    "feedback_clusters",
    "feedback_cluster_items",
    "approval_requests",
    "jobs",
    "audit_logs",
)


def new_id() -> str:
    return str(uuid4())


def now() -> datetime:
    # Naive-UTC, byte-for-byte equivalent to the deprecated datetime.utcnow():
    # every DATETIME column stores naive UTC and common.serialize() re-attaches
    # the UTC marker on the way out, so the tzinfo must stay stripped here.
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True, default=new_id)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    memberships = relationship("WorkspaceMember", back_populates="user", cascade="all, delete-orphan")


class Workspace(Base):
    __tablename__ = "workspaces"
    id = Column(String(36), primary_key=True, default=new_id)
    name = Column(String(255), nullable=False)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    settings_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    owner = relationship("User")
    members = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")
    projects = relationship("Project", back_populates="workspace", cascade="all, delete-orphan")
    metric_definitions = relationship("MetricDefinition", back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),)
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), default="viewer", nullable=False)
    joined_at = Column(DateTime, default=now, nullable=False)
    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", back_populates="memberships")


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    definition = Column(Text, nullable=False)
    category = Column(String(40), default="other", nullable=False)
    numerator = Column(Text, default="", nullable=False)
    denominator = Column(Text, default="", nullable=False)
    unit = Column(String(80), default="", nullable=False)
    aggregation_period = Column(String(20), default="day", nullable=False)
    field_mapping_json = Column(JSON, default=dict, nullable=False)
    display_format = Column(String(30), default="number", nullable=False)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    updated_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)
    deleted_at = Column(DateTime, nullable=True, index=True)
    workspace = relationship("Workspace", back_populates="metric_definitions")
    creator = relationship("User", foreign_keys=[created_by])
    updater = relationship("User", foreign_keys=[updated_by])


class Project(Base):
    __tablename__ = "projects"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    status = Column(String(30), default="active", nullable=False)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    goal_statement = Column(Text, default="")
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)
    workspace = relationship("Workspace", back_populates="projects")
    owner = relationship("User")
    tasks = relationship("Task", back_populates="project", cascade="all, delete-orphan")
    datasets = relationship("Dataset", back_populates="project", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="")
    priority = Column(String(4), default="P2", nullable=False)
    status = Column(String(30), default="todo", nullable=False)
    assignee_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    due_at = Column(DateTime, nullable=True)
    ai_summary = Column(Text, default="")
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)
    project = relationship("Project", back_populates="tasks")
    assignee = relationship("User")
    links = relationship("TaskLink", back_populates="task", cascade="all, delete-orphan")


class TaskLink(Base):
    __tablename__ = "task_links"
    id = Column(String(36), primary_key=True, default=new_id)
    task_id = Column(String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    link_type = Column(String(40), nullable=False)
    target_id = Column(String(36), nullable=False)
    title = Column(String(255), default="")
    created_at = Column(DateTime, default=now, nullable=False)
    task = relationship("Task", back_populates="links")


class Dataset(Base):
    __tablename__ = "datasets"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    source_type = Column(String(30), default="upload", nullable=False)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    project = relationship("Project", back_populates="datasets")
    versions = relationship("DatasetVersion", back_populates="dataset", cascade="all, delete-orphan", order_by="DatasetVersion.version_number")


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version_number", name="uq_dataset_version"),)
    id = Column(String(36), primary_key=True, default=new_id)
    dataset_id = Column(String(36), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    parent_version_id = Column(String(36), nullable=True)
    storage_path = Column(String(1024), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_size_bytes = Column(Integer, default=0, nullable=False)
    row_count = Column(Integer, default=0, nullable=False)
    column_count = Column(Integer, default=0, nullable=False)
    schema_json = Column(JSON, default=dict, nullable=False)
    status = Column(String(30), default="ready", nullable=False)
    fingerprint = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)
    # Set when a user has looked at the inferred field roles.  Reviewing is the
    # stage-2 gate; confirming individual roles is optional because cleaned
    # business tables often have no event columns to map.
    schema_reviewed_at = Column(DateTime, nullable=True)
    # Set when the parse job accepted the inferred roles on the user's behalf.
    # Satisfies the same stage-2 gate, but is deliberately a separate column:
    # an auto-accepted schema is a guess nobody has looked at yet, and the UI
    # must disclose that rather than present it as reviewed.
    schema_auto_accepted_at = Column(DateTime, nullable=True)
    dataset = relationship("Dataset", back_populates="versions")
    columns = relationship("DataColumn", back_populates="dataset_version", cascade="all, delete-orphan", order_by="DataColumn.ordinal")
    quality_report = relationship("DataQualityReport", back_populates="dataset_version", uselist=False, cascade="all, delete-orphan")
    cleaning_operations_from = relationship(
        "CleaningOperation",
        foreign_keys="CleaningOperation.source_version_id",
        back_populates="source_version",
        passive_deletes=True,
        order_by="CleaningOperation.created_at",
    )
    cleaning_operations_to = relationship(
        "CleaningOperation",
        foreign_keys="CleaningOperation.result_version_id",
        back_populates="result_version",
        passive_deletes=True,
        order_by="CleaningOperation.created_at",
    )


class CleaningOperation(Base):
    """An approved, replayable transformation applied to a dataset version.

    Each operation in a cleaning request gets its own row.  The row is created
    when the derived version is queued and its ``preview_json`` is enriched by
    the worker with the actual before/after summary, making failures and
    retries traceable without changing either dataset version.
    """

    __tablename__ = "cleaning_operations"
    id = Column(String(36), primary_key=True, default=new_id)
    source_version_id = Column(String(36), ForeignKey("dataset_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    result_version_id = Column(String(36), ForeignKey("dataset_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    operation_type = Column(String(50), nullable=False)
    parameters_json = Column(JSON, default=dict, nullable=False)
    preview_json = Column(JSON, default=dict, nullable=False)
    approved_by = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=now, nullable=False)
    source_version = relationship(
        "DatasetVersion",
        foreign_keys=[source_version_id],
        back_populates="cleaning_operations_from",
    )
    result_version = relationship(
        "DatasetVersion",
        foreign_keys=[result_version_id],
        back_populates="cleaning_operations_to",
    )
    approver = relationship("User", foreign_keys=[approved_by])


class DataColumn(Base):
    __tablename__ = "data_columns"
    id = Column(String(36), primary_key=True, default=new_id)
    dataset_version_id = Column(String(36), ForeignKey("dataset_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    inferred_type = Column(String(40), nullable=False)
    confirmed_type = Column(String(40), nullable=True)
    nullable = Column(Boolean, default=True, nullable=False)
    unique_ratio = Column(Float, default=0, nullable=False)
    mapping_role = Column(String(50), nullable=True)
    ordinal = Column(Integer, default=0, nullable=False)
    dataset_version = relationship("DatasetVersion", back_populates="columns")


class DataQualityReport(Base):
    __tablename__ = "data_quality_reports"
    id = Column(String(36), primary_key=True, default=new_id)
    dataset_version_id = Column(String(36), ForeignKey("dataset_versions.id", ondelete="CASCADE"), unique=True, nullable=False)
    overall_score = Column(Float, default=0, nullable=False)
    status = Column(String(30), default="needs_review", nullable=False)
    summary_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    dataset_version = relationship("DatasetVersion", back_populates="quality_report")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_version_id = Column(String(36), ForeignKey("dataset_versions.id"), nullable=False, index=True)
    analysis_type = Column(String(64), nullable=False)
    config_json = Column(JSON, default=dict, nullable=False)
    status = Column(String(30), default="succeeded", nullable=False)
    requested_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_code = Column(String(80), nullable=True)
    result_summary = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    artifacts = relationship("AnalysisArtifact", back_populates="analysis_run", cascade="all, delete-orphan")


class AnalysisArtifact(Base):
    __tablename__ = "analysis_artifacts"
    id = Column(String(36), primary_key=True, default=new_id)
    analysis_run_id = Column(String(36), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type = Column(String(50), nullable=False)
    title = Column(String(255), nullable=False)
    payload_json = Column(JSON, default=dict, nullable=False)
    fingerprint = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)
    analysis_run = relationship("AnalysisRun", back_populates="artifacts")


class FeedbackItem(Base):
    __tablename__ = "feedback_items"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_version_id = Column(String(36), nullable=True)
    external_ref = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    user_ref = Column(String(255), nullable=True)
    channel = Column(String(80), nullable=True)
    rating = Column(Float, nullable=True)
    feedback_at = Column(DateTime, nullable=True)
    labels_json = Column(JSON, default=list, nullable=False)
    status = Column(String(30), default="unreviewed", nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class FeedbackCluster(Base):
    __tablename__ = "feedback_clusters"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    summary = Column(Text, default="")
    sentiment = Column(String(30), default="unknown")
    sample_count = Column(Integer, default=0, nullable=False)
    evidence_json = Column(JSON, default=list, nullable=False)
    status = Column(String(30), default="draft", nullable=False)
    ai_run_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)


class FeedbackClusterItem(Base):
    __tablename__ = "feedback_cluster_items"
    cluster_id = Column(String(36), ForeignKey("feedback_clusters.id", ondelete="CASCADE"), primary_key=True)
    feedback_item_id = Column(String(36), ForeignKey("feedback_items.id", ondelete="CASCADE"), primary_key=True)
    score = Column(Float, default=0, nullable=False)


class FeedbackNote(Base):
    """Single-row feedback representation used by the V1.1 pipeline.

    The legacy feedback item/cluster tables remain available for backwards
    compatibility during migration; new code can write one normalized note and
    update its cluster label in place.
    """

    __tablename__ = "feedback_notes"
    id = Column(String(36), primary_key=True, default=new_id)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    dataset_version_id = Column(String(36), ForeignKey("dataset_versions.id", ondelete="SET NULL"), nullable=True, index=True)
    content = Column(Text, nullable=False)
    source = Column(String(80), default="manual", nullable=False)
    label = Column(String(120), default="", nullable=False)
    sentiment = Column(String(30), default="unknown", nullable=False)
    cluster_name = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=now, nullable=False)


class Insight(Base):
    __tablename__ = "insights"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(String(36), ForeignKey("tasks.id"), nullable=True)
    title = Column(String(255), nullable=False)
    insight_type = Column(String(30), nullable=False)
    content = Column(Text, nullable=False)
    confidence = Column(String(20), default="medium", nullable=False)
    evidence_json = Column(JSON, default=list, nullable=False)
    status = Column(String(30), default="draft", nullable=False)
    ai_run_id = Column(String(36), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)


class ProductProblem(Base):
    """Pipeline stage 9.  Converts scattered insights into a framed problem.

    ``source_insight_ids`` is the traceability link back to data.  A problem may
    be drafted freely, but it cannot reach ``confirmed`` without at least one
    source insight -- otherwise the pipeline would allow a problem asserted from
    intuition to flow into a decision.
    """

    __tablename__ = "product_problems"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    statement = Column(Text, nullable=False)
    impact_scope = Column(Text, default="")
    source_insight_ids = Column(JSON, default=list, nullable=False)
    status = Column(String(20), default="open", nullable=False)
    priority = Column(String(4), default="P2", nullable=False)
    ai_run_id = Column(String(36), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)
    solutions = relationship("SolutionOption", back_populates="problem", cascade="all, delete-orphan", order_by="SolutionOption.created_at")


class SolutionOption(Base):
    """Pipeline stage 10.  Candidate approaches compared before a decision.

    ``reject_reason`` is required on non-selected options once a selection is
    made.  Recording why an option lost is the durable value of this stage; the
    winning option alone does not explain the decision.
    """

    __tablename__ = "solution_options"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    problem_id = Column(String(36), ForeignKey("product_problems.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    approach = Column(Text, nullable=False)
    pros = Column(JSON, default=list, nullable=False)
    cons = Column(JSON, default=list, nullable=False)
    effort = Column(String(10), default="M", nullable=False)
    status = Column(String(20), default="proposed", nullable=False)
    reject_reason = Column(Text, default="")
    ai_run_id = Column(String(36), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)
    problem = relationship("ProductProblem", back_populates="solutions")


class DecisionProposal(Base):
    __tablename__ = "decision_proposals"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(String(36), ForeignKey("tasks.id"), nullable=True)
    title = Column(String(255), nullable=False)
    problem_statement = Column(Text, nullable=False)
    proposed_action = Column(Text, nullable=False)
    expected_impact = Column(Text, default="")
    risk_summary = Column(Text, default="")
    validation_plan = Column(Text, nullable=False)
    priority = Column(String(4), default="P2", nullable=False)
    status = Column(String(30), default="draft", nullable=False)
    evidence_json = Column(JSON, default=list, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type = Column(String(50), nullable=False)
    target_id = Column(String(36), nullable=False, index=True)
    action_type = Column(String(50), nullable=False)
    status = Column(String(20), default="pending", nullable=False)
    requested_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    decided_by = Column(String(36), ForeignKey("users.id"), nullable=True)
    decision_note = Column(Text, default="")
    version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    decided_at = Column(DateTime, nullable=True)


class Document(Base):
    __tablename__ = "documents"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    document_type = Column(String(30), nullable=False)
    title = Column(String(255), nullable=False)
    status = Column(String(30), default="draft", nullable=False)
    current_version_id = Column(String(36), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)
    versions = relationship("DocumentVersion", back_populates="document", cascade="all, delete-orphan", order_by="DocumentVersion.version_number")


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id = Column(String(36), primary_key=True, default=new_id)
    document_id = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content_markdown = Column(Text, nullable=False)
    evidence_json = Column(JSON, default=list, nullable=False)
    ai_run_id = Column(String(36), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    document = relationship("Document", back_populates="versions")


class Job(Base):
    __tablename__ = "jobs"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type = Column(String(60), nullable=False)
    status = Column(String(20), default="queued", nullable=False)
    progress = Column(Integer, default=0, nullable=False)
    current_step = Column(String(255), default="queued")
    input_json = Column(JSON, default=dict, nullable=False)
    result_type = Column(String(60), nullable=True)
    result_id = Column(String(36), nullable=True)
    error_code = Column(String(80), nullable=True)
    error_message = Column(Text, nullable=True)
    attempt_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class CopilotSession(Base):
    __tablename__ = "copilot_sessions"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    page_context_json = Column(JSON, default=dict, nullable=False)
    summary = Column(Text, default="")
    created_at = Column(DateTime, default=now, nullable=False)
    messages = relationship("CopilotMessage", back_populates="session", cascade="all, delete-orphan", order_by="CopilotMessage.created_at")


class CopilotMessage(Base):
    __tablename__ = "copilot_messages"
    id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), ForeignKey("copilot_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    content_json = Column(JSON, default=dict, nullable=False)
    ai_run_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)
    session = relationship("CopilotSession", back_populates="messages")


class AIRun(Base):
    __tablename__ = "ai_runs"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    feature_name = Column(String(80), nullable=False)
    provider = Column(String(40), default="deepseek", nullable=False)
    model = Column(String(80), nullable=False)
    request_fingerprint = Column(String(128), nullable=True)
    status = Column(String(20), default="running", nullable=False)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    input_summary_json = Column(JSON, default=dict, nullable=False)
    output_reference = Column(String(255), nullable=True)
    error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)


class AutoAnalysisReport(Base):
    """Project-level analysis report covering the latest version of each dataset.

    The deterministic aggregates (EDA, distributions, trend) are computed by
    pandas first; the AI call only narrates them.  ``status`` records who wrote
    the prose: ``succeeded`` means a provider call was validated and stored,
    ``not_configured`` means the sections carry the deterministic fallback only.
    A report is never auto-confirmed -- ``confirm`` is a separate user action,
    so downstream stages can tell a reviewed report from a fresh draft.
    """

    __tablename__ = "auto_analysis_reports"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    # draft | succeeded | not_configured | failed | confirmed
    status = Column(String(32), default="draft", nullable=False)
    summary = Column(Text, default="", nullable=False)
    content_markdown = Column(Text, default="", nullable=False)
    sections_json = Column(JSON, default=list, nullable=False)
    key_findings = Column(JSON, default=list, nullable=False)
    recommendations = Column(JSON, default=list, nullable=False)
    limitations = Column(JSON, default=list, nullable=False)
    dataset_version_ids = Column(JSON, default=list, nullable=False)
    deterministic_json = Column(JSON, default=dict, nullable=False)
    ai_run_id = Column(String(36), nullable=True)
    error_code = Column(String(80), nullable=True)
    generated_by = Column(String(36), nullable=True)
    confirmed_by = Column(String(36), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)


class AnalysisReportNarration(Base):
    """The AI-written reading of a finished set of analysis runs.

    Separate from ``AnalysisRun`` on purpose: the runs are deterministic pandas
    output and must stay complete and usable when narration is absent, disabled,
    or over budget.  ``status`` starts at ``draft`` and is never auto-confirmed,
    so a narration cannot reach the stage 6+ decision chain on its own -- the
    hard AI boundary after stage 5 is preserved.
    """

    __tablename__ = "analysis_report_narrations"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    dataset_version_id = Column(String(36), ForeignKey("dataset_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    ai_run_id = Column(String(36), nullable=True)
    # draft | not_configured | budget_exceeded | failed | confirmed
    status = Column(String(32), default="draft", nullable=False)
    summary = Column(Text, nullable=True)
    payload_json = Column(JSON, default=dict, nullable=False)
    analysis_run_ids = Column(JSON, default=list, nullable=False)
    generated_by = Column(String(36), nullable=True)
    confirmed_by = Column(String(36), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)
    updated_at = Column(DateTime, default=now, onupdate=now, nullable=False)


class InterviewQuestion(Base):
    """One AI-interview question (or a manual supplement) for a project.

    Stage 6 asks the model for a round of 3-5 grounded questions; the user
    answers or skips each one and can add points manually at any time.
    ``round_number`` 0 marks manual rows; AI rounds count up from 1.  A row
    never blocks the pipeline by itself -- stage 7 distills answered rows into
    insight drafts, and the usual draft/adjudication boundary applies there.
    """

    __tablename__ = "interview_questions"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    round_number = Column(Integer, default=1, nullable=False)   # 0 = 手动补充
    topic = Column(String(120), default="", nullable=False)
    question_text = Column(Text, nullable=False)                # 手动补充时为要点标题
    rationale = Column(Text, default="", nullable=False)        # AI 说明"为什么问这个"
    # pending | answered | skipped
    status = Column(String(20), default="pending", nullable=False)
    answer_text = Column(Text, default="", nullable=False)
    # ai | manual
    source = Column(String(20), default="ai", nullable=False)
    ai_run_id = Column(String(36), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
    answered_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String(36), primary_key=True, default=new_id)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_type = Column(String(30), nullable=False)
    actor_id = Column(String(36), nullable=True)
    action = Column(String(80), nullable=False)
    target_type = Column(String(80), nullable=True)
    target_id = Column(String(36), nullable=True)
    detail_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
