from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    workspace_name: str = Field(default="My Workspace", min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str


class UserOut(APIModel):
    id: str
    email: str
    name: str
    is_active: bool
    created_at: datetime


class WorkspaceFeatureFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    copilot_enabled: bool = True
    document_generation_enabled: bool = True
    insight_suggestions_enabled: bool = True
    auto_report_enabled: bool = True
    anomaly_alert: bool = True
    quality_alert: bool = True
    review_alert: bool = True
    email_digest: bool = False


class WorkspaceFeatureFlagsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    copilot_enabled: bool | None = None
    document_generation_enabled: bool | None = None
    insight_suggestions_enabled: bool | None = None
    auto_report_enabled: bool | None = None
    anomaly_alert: bool | None = None
    quality_alert: bool | None = None
    review_alert: bool | None = None
    email_digest: bool | None = None


class WorkspaceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=50)
    data_retention_days: int | None = Field(default=90, ge=1, le=3650)
    analysis_threshold: float = Field(default=3.0, gt=0, le=100)
    ai_model_id: str = Field(default="deepseek-chat", min_length=1, max_length=100)
    ai_max_output_tokens: int = Field(default=4096, ge=1, le=131072)
    ai_per_request_token_budget: int = Field(default=16000, ge=1, le=1_000_000)
    ai_daily_token_budget: int = Field(default=500000, ge=1, le=100_000_000)
    feature_flags: WorkspaceFeatureFlags = Field(default_factory=WorkspaceFeatureFlags)


class WorkspaceSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = Field(default=None, min_length=1, max_length=50)
    data_retention_days: int | None = Field(default=None, ge=1, le=3650)
    analysis_threshold: float | None = Field(default=None, gt=0, le=100)
    ai_model_id: str | None = Field(default=None, min_length=1, max_length=100)
    ai_max_output_tokens: int | None = Field(default=None, ge=1, le=131072)
    ai_per_request_token_budget: int | None = Field(default=None, ge=1, le=1_000_000)
    ai_daily_token_budget: int | None = Field(default=None, ge=1, le=100_000_000)
    feature_flags: WorkspaceFeatureFlagsPatch | None = None


class WorkspacePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: WorkspaceSettingsPatch | None = None


MetricCategory = Literal["active", "retention", "conversion", "quality", "cost", "feedback", "other"]
MetricAggregationPeriod = Literal["day", "week", "month"]
MetricDisplayFormat = Literal["number", "percentage", "duration", "currency"]


class MetricDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    definition: str = Field(min_length=1, max_length=4000)
    category: MetricCategory = "other"
    numerator: str = Field(default="", max_length=4000)
    denominator: str = Field(default="", max_length=4000)
    unit: str = Field(default="", max_length=80)
    aggregation_period: MetricAggregationPeriod = "day"
    field_mapping: dict[str, str] = Field(default_factory=dict)
    display_format: MetricDisplayFormat = "number"


class MetricDefinitionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    definition: str | None = Field(default=None, min_length=1, max_length=4000)
    category: MetricCategory | None = None
    numerator: str | None = Field(default=None, max_length=4000)
    denominator: str | None = Field(default=None, max_length=4000)
    unit: str | None = Field(default=None, max_length=80)
    aggregation_period: MetricAggregationPeriod | None = None
    field_mapping: dict[str, str] | None = None
    display_format: MetricDisplayFormat | None = None


class MemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    role: Literal["owner", "editor", "viewer"] = "viewer"


class MemberPatch(BaseModel):
    role: Literal["owner", "editor", "viewer"]


class ProjectCreate(BaseModel):
    workspace_id: str | None = None
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    status: str = "active"
    goal_statement: str = ""


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: str | None = None
    goal_statement: str | None = None


TaskStatus = Literal["todo", "in_progress", "in_review", "blocked", "done", "archived"]


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    status: TaskStatus = "todo"
    assignee_id: str | None = None
    due_at: datetime | None = None


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    status: TaskStatus | None = None
    assignee_id: str | None = None
    due_at: datetime | None = None
    ai_summary: str | None = None


class LinkCreate(BaseModel):
    link_type: str = Field(min_length=1, max_length=40)
    target_id: str
    title: str = ""


class DatasetDeleteRequest(BaseModel):
    """Explicit confirmation required before soft-deleting a dataset.

    The API accepts either the dataset id (the strongest confirmation and the
    value used by the web client) or a boolean ``true`` for API clients that
    cannot conveniently echo a path parameter in a request body.
    """

    confirm: bool | str


class SchemaPatch(BaseModel):
    columns: list[dict[str, Any]]


class AnalysisCreate(BaseModel):
    project_id: str
    dataset_version_id: str
    analysis_type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    field_mapping: dict[str, str] = Field(default_factory=dict)


class FeedbackCreate(BaseModel):
    project_id: str
    content: str = Field(min_length=1)
    external_ref: str | None = None
    user_ref: str | None = None
    channel: str | None = None
    rating: float | None = None
    feedback_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    status: str = "unreviewed"


class FeedbackPatch(BaseModel):
    labels: list[str] | None = None
    status: str | None = None
    project_id: str | None = None


class FeedbackNoteCreate(BaseModel):
    """Normalized V1.1 feedback record.

    Notes intentionally contain only the durable context needed by the
    pipeline. Raw source metadata stays out of the AI context allowlist.
    """

    project_id: str | None = None
    dataset_version_id: str | None = None
    content: str = Field(min_length=1, max_length=12000)
    source: str = Field(default="manual", min_length=1, max_length=80)
    label: str = Field(default="", max_length=120)
    sentiment: str = Field(default="unknown", max_length=30)
    cluster_name: str | None = Field(default=None, max_length=255)


class FeedbackNotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=120)
    sentiment: str | None = Field(default=None, max_length=30)
    cluster_name: str | None = Field(default=None, max_length=255)
    project_id: str | None = None


class FeedbackClusterPatch(BaseModel):
    name: str | None = None
    summary: str | None = None
    status: str | None = None


class InsightCreate(BaseModel):
    project_id: str
    task_id: str | None = None
    title: str
    insight_type: Literal["fact", "hypothesis", "recommendation"]
    content: str
    confidence: Literal["high", "medium", "low"] = "medium"
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["draft", "confirmed", "rejected"] = "draft"


class InsightPatch(BaseModel):
    title: str | None = None
    content: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    evidence: list[dict[str, Any]] | None = None
    status: Literal["draft", "confirmed", "rejected"] | None = None


class ProblemCreate(BaseModel):
    project_id: str
    title: str
    statement: str
    impact_scope: str = ""
    source_insight_ids: list[str] = Field(default_factory=list)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    status: Literal["open", "framing", "confirmed", "dropped"] = "open"


class ProblemPatch(BaseModel):
    title: str | None = None
    statement: str | None = None
    impact_scope: str | None = None
    source_insight_ids: list[str] | None = None
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    status: Literal["open", "framing", "confirmed", "dropped"] | None = None


class SolutionCreate(BaseModel):
    title: str
    approach: str
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    effort: Literal["S", "M", "L"] = "M"


class SolutionPatch(BaseModel):
    title: str | None = None
    approach: str | None = None
    pros: list[str] | None = None
    cons: list[str] | None = None
    effort: Literal["S", "M", "L"] | None = None
    status: Literal["proposed", "discussing", "selected", "rejected"] | None = None
    reject_reason: str | None = None


class SolutionSelect(BaseModel):
    """Selecting one option rejects its siblings, so the caller must supply the
    losing rationale up front, keyed by option id."""

    reject_reasons: dict[str, str] = Field(default_factory=dict)


class AIFrameProblemRequest(BaseModel):
    project_id: str
    insight_ids: list[str] = Field(default_factory=list)
    question: str = ""


class AIProposeSolutionsRequest(BaseModel):
    problem_id: str
    option_count: int = Field(default=3, ge=2, le=5)


class AIDraftDecisionRequest(BaseModel):
    """Batch 19: body for the AI-drafted decision proposal (draft only)."""

    problem_id: str


class DecisionCreate(BaseModel):
    project_id: str
    task_id: str | None = None
    title: str
    problem_statement: str
    proposed_action: str
    expected_impact: str = ""
    risk_summary: str = ""
    validation_plan: str
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class DecisionPatch(BaseModel):
    title: str | None = None
    problem_statement: str | None = None
    proposed_action: str | None = None
    expected_impact: str | None = None
    risk_summary: str | None = None
    validation_plan: str | None = None
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    evidence: list[dict[str, Any]] | None = None
    version: int | None = Field(default=None, ge=1)


class ApprovalDecision(BaseModel):
    version: int = Field(ge=1)
    decision_note: str = ""


class DocumentCreate(BaseModel):
    project_id: str
    document_type: Literal["weekly_report", "retrospective", "prd"] = "prd"
    title: str
    content_markdown: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class DocumentGenerate(BaseModel):
    project_id: str
    document_type: Literal["weekly_report", "retrospective", "prd"] = "prd"
    title: str
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    template_options: dict[str, Any] = Field(default_factory=dict)


class AIInterpretRequest(BaseModel):
    """Request contract for the stateless V1.1 interpretation endpoint.

    ``context`` is intentionally untyped at the HTTP boundary for backwards
    compatibility.  The route immediately projects it through
    :func:`build_ai_context`, so unknown fields and raw data never reach the
    provider or persistence layer.
    """

    model_config = ConfigDict(extra="ignore")

    project_id: str | None = None
    dataset_version_id: str | None = None
    # ``dataset_id`` is accepted as a compatibility alias for clients that
    # still use that name for a version identifier.
    dataset_id: str | None = None
    question: str | None = Field(default=None, max_length=4000)
    prompt: str | None = Field(default=None, max_length=4000)
    user_question: str | None = Field(default=None, max_length=4000)
    content: str | None = Field(default=None, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)


class AIClusterFeedbackRequest(BaseModel):
    """Request contract for draft feedback clustering.

    Clusters are always persisted as ``draft``; human confirmation remains a
    separate action and no AI-generated cluster is treated as a conclusion.
    """

    model_config = ConfigDict(extra="ignore")

    project_id: str
    dataset_version_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class DocumentVersionCreate(BaseModel):
    content_markdown: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    version: int | None = Field(default=None, ge=1)


class AIDistillInterviewRequest(BaseModel):
    """Distill answered interview questions + analysis artifacts (stage 7)."""

    project_id: str


class InterviewQuestionCreate(BaseModel):
    """Manual supplement for the stage-6 interview: a point title plus the
    information the user wants on record (empty info leaves it pending)."""

    project_id: str
    topic: str = ""
    question_text: str = Field(min_length=1)
    answer_text: str = ""


class InterviewQuestionPatch(BaseModel):
    answer_text: str | None = None
    status: Literal["answered", "skipped"] | None = None


class CopilotSessionCreate(BaseModel):
    workspace_id: str
    project_id: str | None = None
    page_context: dict[str, Any] = Field(default_factory=dict)


class CopilotMessageCreate(BaseModel):
    content: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
