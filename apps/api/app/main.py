from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from . import db as database
from .ai_context import (
    AI_OUTPUT_SCHEMA,
    AIOutputValidationError,
    assert_safe_ai_context,
    build_ai_context,
    empty_ai_output,
    empty_report_output,
    validate_ai_output,
    validate_report_output,
    REPORT_OUTPUT_SCHEMA,
)
from .analytics.engine import AnalysisEngine
from .analytics.quality import apply_cleaning as apply_quality_cleaning
from .analytics.quality import assess_quality, infer_column_type
from .auth import create_access_token, get_current_user, hash_password, password_needs_rehash, verify_password
from .config import settings
from .db import SessionLocal, get_db, init_db
from .infrastructure.jobs import JobContext, JobExecutionError, JobExecutor, JobResult
from .infrastructure.llm.deepseek import (
    AiRequestMetadata,
    AnalysisPlanError,
    ChatMessage,
    CopilotOrchestrator,
    DeepSeekAdapter,
    DeepSeekConfigurationError,
    DeepSeekError,
    DeepSeekProviderError,
    DeepSeekSettings,
    ToolPermissionError,
    build_default_tool_registry,
)
from .models import (
    AIRun,
    AnalysisArtifact,
    AnalysisRun,
    ApprovalRequest,
    AuditLog,
    AutoAnalysisReport,
    CleaningOperation,
    CopilotMessage,
    CopilotSession,
    DataColumn,
    DataQualityReport,
    Dataset,
    DatasetVersion,
    DecisionProposal,
    Document,
    DocumentVersion,
    FeedbackCluster,
    FeedbackClusterItem,
    FeedbackItem,
    FeedbackNote,
    Insight,
    Job,
    MetricDefinition,
    ProductProblem,
    Project,
    SolutionOption,
    Task,
    TaskLink,
    User,
    Workspace,
    WorkspaceMember,
    now,
)
from .schemas import (
    AIClusterFeedbackRequest,
    AIFrameProblemRequest,
    AIInterpretRequest,
    AIProposeSolutionsRequest,
    AnalysisCreate,
    ApprovalDecision,
    CleaningRequest,
    CopilotMessageCreate,
    CopilotSessionCreate,
    DatasetDeleteRequest,
    DecisionCreate,
    DecisionPatch,
    DocumentCreate,
    DocumentGenerate,
    DocumentVersionCreate,
    FeedbackClusterPatch,
    FeedbackCreate,
    FeedbackNoteCreate,
    FeedbackNotePatch,
    FeedbackPatch,
    InsightCreate,
    InsightPatch,
    LinkCreate,
    LoginRequest,
    MemberCreate,
    MemberPatch,
    MetricDefinitionCreate,
    MetricDefinitionPatch,
    ProblemCreate,
    ProblemPatch,
    ProjectCreate,
    ProjectPatch,
    RegisterRequest,
    SchemaPatch,
    SolutionCreate,
    SolutionPatch,
    SolutionSelect,
    TaskCreate,
    TaskPatch,
    WorkspacePatch,
    WorkspaceSettings,
    WorkspaceSettingsPatch,
)

# pandas is imported lazily by _require_pandas() to keep API startup fast; this
# sentinel holds the cached module once that first import succeeds.
pd = None  # type: ignore[assignment]

job_executor = JobExecutor(SessionLocal)
app = FastAPI(title="AI Product Workspace API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_V11_LEGACY_API_PREFIXES = (
    "/api/v1/workspaces",
    "/api/v1/tasks",
    "/api/v1/approval-requests",
    "/api/v1/decision-proposals",
    "/api/v1/jobs",
    "/api/v1/copilot/sessions",
    "/api/v1/feedback-items",
    "/api/v1/feedback-clusters",
)


@app.middleware("http")
async def mark_legacy_api_surfaces(request: Request, call_next):
    response = await call_next(request)
    if any(request.url.path.startswith(prefix) for prefix in _V11_LEGACY_API_PREFIXES):
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = "2027-01-01"
        response.headers["Link"] = '</api/v1/feedback-notes>; rel="successor-version"'
    return response


def _redact_validation_details(value: Any) -> Any:
    sensitive_names = {"apikey", "deepseekapikey", "password", "secret", "accesstoken", "refreshtoken", "authorization"}
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in sensitive_names:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_validation_details(item)
        error_location = tuple(redacted.get("loc") or ())
        if error_location:
            last_field = re.sub(r"[^a-z0-9]", "", str(error_location[-1]).lower())
            if last_field in sensitive_names:
                if "input" in redacted:
                    redacted["input"] = "[REDACTED]"
                if "msg" in redacted:
                    for name in sensitive_names:
                        if name in redacted["msg"].lower():
                            redacted["msg"] = re.sub(name, "[REDACTED]", redacted["msg"], flags=re.IGNORECASE)
        return redacted
    if isinstance(value, list):
        return [_redact_validation_details(item) for item in value]
    return value


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    # Keep the canonical ``error`` envelope while exposing the legacy
    # ``detail`` alias for readiness probes and older API clients.
    return JSONResponse(status_code=exc.status_code, content={"error": detail, "detail": detail, "meta": {"request_id": _request_id()}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed", "details": _redact_validation_details(exc.errors())}, "meta": {"request_id": _request_id()}})


@app.on_event("startup")
async def startup() -> None:
    # Reading the property creates DATA_ROOT and its uploads/processed/exports
    # subdirectories, so a fresh clone can accept an upload before any request.
    _ = settings.data_path
    init_db()
    # A failed configured database must remain diagnosable through the health
    # endpoint.  Do not let job recovery mask the real connection error.
    if not database.database_ready():
        return
    await job_executor.recover_pending()


def _request_id() -> str:
    return f"req_{uuid4().hex}"


def _require_pandas():
    global pd
    if pd is None:
        try:
            import importlib

            pd = importlib.import_module("pandas")
        except Exception:
            pd = None
    if pd is None:
        raise error("DEPENDENCY_ERROR", "Pandas is unavailable; install API dependencies before using data endpoints", 503)
    return pd


def ok(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": _request_id(), **meta}}


def error(code: str, message: str, status_code: int = 400, details: Any = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        detail["details"] = details
    return HTTPException(status_code=status_code, detail=detail)


# Credential material must never leave the API, regardless of which endpoint
# serializes the model (BUG-013).
SENSITIVE_MODEL_FIELDS = {"password_hash"}


def serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items() if str(k) not in SENSITIVE_MODEL_FIELDS}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "__table__"):
        return {k: serialize(v) for k, v in vars(value).items() if not k.startswith("_") and k not in SENSITIVE_MODEL_FIELDS}
    return value


def model_dict(obj: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if hasattr(obj, "__table__"):
        result = {column.name: serialize(getattr(obj, column.name)) for column in obj.__table__.columns if column.name not in SENSITIVE_MODEL_FIELDS}
    else:
        result = {k: serialize(v) for k, v in vars(obj).items() if not k.startswith("_") and k not in SENSITIVE_MODEL_FIELDS}
    if extra:
        result.update(serialize(extra))
    return result


def page_params(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> tuple[int, int]:
    return page, page_size


def paged(items: list[Any], page: int, page_size: int, total: int | None = None) -> dict[str, Any]:
    total = len(items) if total is None else total
    start = (page - 1) * page_size
    return ok(items[start : start + page_size], page=page, page_size=page_size, total=total)


def audit(
    db: Session,
    workspace_id: str,
    actor_id: str | None,
    action: str,
    target_type: str = "",
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    actor_type: str = "user",
) -> None:
    db.add(AuditLog(workspace_id=workspace_id, actor_type=actor_type, actor_id=actor_id, action=action, target_type=target_type, target_id=target_id, detail_json=detail or {}))


def audit_user_workspaces(
    db: Session,
    user_id: str,
    action: str,
    target_type: str = "user",
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    actor_type: str = "user",
) -> None:
    """Write an authentication event to every workspace the user belongs to.

    ``audit_logs`` is intentionally workspace-scoped, so a known user's auth
    event is copied to each of their workspaces. An unknown email has no safe
    workspace to associate with and is therefore not persisted; the caller
    still returns the same generic authentication error in either case.
    """

    workspace_ids = db.scalars(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id)).all()
    for workspace_id in workspace_ids:
        actor_id = user_id if actor_type == "user" else None
        audit(db, workspace_id, actor_id, action, target_type, target_id or user_id, detail, actor_type)


def membership(db: Session, user: User, workspace_id: str, minimum: str = "viewer") -> WorkspaceMember:
    member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id))
    if member is None:
        raise error("FORBIDDEN", "Workspace access denied", 403)
    ranks = {"viewer": 1, "editor": 2, "owner": 3}
    if ranks.get(member.role, 0) < ranks.get(minimum, 1):
        raise error("FORBIDDEN", f"{minimum} role required", 403)
    return member


def _workspace_settings(workspace: Workspace) -> dict[str, Any]:
    """Return the allowlisted settings contract without exposing legacy secrets."""

    defaults = WorkspaceSettings().model_dump()
    # Keep the workspace view aligned with the provider health endpoint when a
    # workspace has no explicit model override.  Otherwise the UI would show
    # the schema fallback (``deepseek-chat``) while the configured server model
    # could be different.
    defaults["ai_model_id"] = DeepSeekSettings.from_app_settings(settings).model
    source = workspace.settings_json if isinstance(workspace.settings_json, dict) else {}
    candidate = dict(defaults)
    for key in (
        "timezone",
        "data_retention_days",
        "analysis_threshold",
        "ai_model_id",
        "ai_max_output_tokens",
        "ai_per_request_token_budget",
        "ai_daily_token_budget",
    ):
        if key in source:
            candidate[key] = source[key]

    flags = dict(defaults["feature_flags"])
    stored_flags = source.get("feature_flags")
    if isinstance(stored_flags, dict):
        for key in flags:
            if key in stored_flags:
                flags[key] = stored_flags[key]
    for key in ("anomaly_alert", "quality_alert", "review_alert", "email_digest"):
        if key in source:
            flags[key] = source[key]
    candidate["feature_flags"] = flags

    try:
        return WorkspaceSettings.model_validate(candidate).model_dump()
    except Exception:
        # Invalid legacy values are never reflected. The next valid PATCH will
        # rewrite the stored JSON to the canonical, allowlisted representation.
        return defaults


def _merge_workspace_settings(workspace: Workspace, patch: WorkspaceSettingsPatch) -> tuple[dict[str, Any], list[str]]:
    merged = _workspace_settings(workspace)
    updates = patch.model_dump(exclude_unset=True)
    feature_updates = updates.pop("feature_flags", None)
    changed_fields = list(updates)
    merged.update(updates)
    if isinstance(feature_updates, dict):
        merged["feature_flags"] = {**merged["feature_flags"], **feature_updates}
        changed_fields.extend(f"feature_flags.{key}" for key in feature_updates)

    validated = WorkspaceSettings.model_validate(merged)
    if validated.ai_max_output_tokens > validated.ai_per_request_token_budget:
        raise error("VALIDATION_ERROR", "AI max output cannot exceed the per-request token budget", 400)
    if validated.ai_per_request_token_budget > validated.ai_daily_token_budget:
        raise error("VALIDATION_ERROR", "Per-request token budget cannot exceed the daily token budget", 400)
    workspace.settings_json = validated.model_dump()
    return workspace.settings_json, changed_fields


def _token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _workspace_day_start(workspace: Workspace) -> datetime:
    settings_json = _workspace_settings(workspace)
    try:
        local_zone = ZoneInfo(str(settings_json.get("timezone") or "UTC"))
    except Exception:
        local_zone = UTC
    local_now = datetime.now(local_zone)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(UTC).replace(tzinfo=None)


def _workspace_ai_budget(workspace: Workspace) -> dict[str, Any]:
    workspace_settings = _workspace_settings(workspace)
    provider_settings = DeepSeekSettings.from_app_settings(settings)
    per_request = max(1, _token_count(workspace_settings.get("ai_per_request_token_budget")))
    return {
        "per_request": per_request,
        "daily": max(1, _token_count(workspace_settings.get("ai_daily_token_budget") or provider_settings.daily_token_budget)),
        "max_output": min(per_request, max(1, _token_count(workspace_settings.get("ai_max_output_tokens")))),
        "model": str(workspace_settings.get("ai_model_id") or provider_settings.model),
    }


def _workspace_token_usage(db: Session, workspace: Workspace, per_request_budget: int, exclude_run_id: str | None = None) -> int:
    rows = db.scalars(
        select(AIRun).where(
            AIRun.workspace_id == workspace.id,
            AIRun.status.in_(["running", "succeeded"]),
            AIRun.created_at >= _workspace_day_start(workspace),
        )
    ).all()
    total = 0
    for run in rows:
        if exclude_run_id and run.id == exclude_run_id:
            continue
        observed = _token_count(run.prompt_tokens) + _token_count(run.completion_tokens)
        if run.status == "running":
            # A running provider call has no final usage yet. Reserve the whole
            # per-request budget so concurrent requests cannot oversubscribe.
            total += max(per_request_budget, observed)
        elif observed:
            total += observed
        else:
            # Legacy succeeded runs may predate provider usage persistence.
            total += per_request_budget
    return total


def _reject_ai_budget(
    db: Session,
    workspace: Workspace,
    user: User,
    ai_run: AIRun,
    budget: dict[str, Any],
    *,
    daily_used: int,
    observed_tokens: int = 0,
    reason: str = "Workspace AI token budget exceeded",
) -> None:
    details = {
        "daily_used_tokens": daily_used,
        "daily_token_budget": budget["daily"],
        "per_request_token_budget": budget["per_request"],
        "observed_tokens": observed_tokens,
    }
    ai_run.status = "failed"
    ai_run.error_code = "AI_BUDGET_EXCEEDED"
    ai_run.input_summary_json = {**(ai_run.input_summary_json or {}), "budget": details, "events": [{"type": "run.failed", "data": {"code": "AI_BUDGET_EXCEEDED", "retryable": False}}]}
    audit(db, workspace.id, user.id, "copilot.budget_exceeded", "ai_run", ai_run.id, details)
    db.commit()
    raise error("AI_BUDGET_EXCEEDED", reason, 429, details)


def _workspace_payload(workspace: Workspace, role: str | None = None) -> dict[str, Any]:
    payload = model_dict(workspace)
    payload["settings_json"] = _workspace_settings(workspace)
    if role is not None:
        payload["role"] = role
    return payload


def _metric_payload(metric: MetricDefinition) -> dict[str, Any]:
    return {
        "id": metric.id,
        "workspace_id": metric.workspace_id,
        "name": metric.name,
        "definition": metric.definition,
        "category": metric.category,
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "unit": metric.unit,
        "aggregation_period": metric.aggregation_period,
        "field_mapping": metric.field_mapping_json or {},
        "display_format": metric.display_format,
        "maintainer_id": metric.updated_by,
        "created_at": serialize(metric.created_at),
        "updated_at": serialize(metric.updated_at),
    }


def _migrate_legacy_metric_dictionary(db: Session, workspace: Workspace) -> None:
    active_count = db.scalar(
        select(func.count()).select_from(MetricDefinition).where(
            MetricDefinition.workspace_id == workspace.id,
            MetricDefinition.deleted_at.is_(None),
        )
    ) or 0
    source = workspace.settings_json if isinstance(workspace.settings_json, dict) else {}
    legacy_metrics = source.get("metric_dictionary")
    if active_count or not isinstance(legacy_metrics, list):
        return

    migrated = 0
    period_map = {"日": "day", "周": "week", "月": "month", "daily": "day", "weekly": "week", "monthly": "month"}
    for item in legacy_metrics:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        definition = str(item.get("definition") or "").strip()
        if not name or not definition:
            continue
        unit = str(item.get("unit") or "").strip()
        display_format = str(item.get("display_format") or ("percentage" if "百分比" in unit or unit == "%" else "number"))
        if display_format not in {"number", "percentage", "duration", "currency"}:
            display_format = "number"
        db.add(
            MetricDefinition(
                workspace_id=workspace.id,
                name=name[:255],
                definition=definition[:4000],
                category=str(item.get("category") or "other") if item.get("category") in {"active", "retention", "conversion", "quality", "cost", "feedback", "other"} else "other",
                numerator=str(item.get("numerator") or "")[:4000],
                denominator=str(item.get("denominator") or "")[:4000],
                unit=unit[:80],
                aggregation_period=period_map.get(str(item.get("aggregation_period") or item.get("period") or ""), "day"),
                field_mapping_json=item.get("field_mapping") if isinstance(item.get("field_mapping"), dict) else {},
                display_format=display_format,
                created_by=workspace.owner_id,
                updated_by=workspace.owner_id,
            )
        )
        migrated += 1
    if not migrated:
        return
    workspace.settings_json = _workspace_settings(workspace)
    audit(db, workspace.id, None, "metric_dictionary.migrated", "workspace", workspace.id, {"count": migrated}, "system")
    db.commit()


def project_for(db: Session, user: User, project_id: str, minimum: str = "viewer") -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise error("NOT_FOUND", "Project not found", 404)
    membership(db, user, project.workspace_id, minimum)
    return project


def _task_for_project(db: Session, project: Project, task_id: str | None) -> Task | None:
    if not task_id:
        return None
    task = db.get(Task, task_id)
    if task is None or task.workspace_id != project.workspace_id or task.project_id != project.id:
        raise error("FORBIDDEN", "Task is outside the selected project", 403)
    return task


def _check_assignee(db: Session, workspace_id: str, assignee_id: str | None) -> None:
    if assignee_id and db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == assignee_id)) is None:
        raise error("VALIDATION_ERROR", "Assignee is not a member of this workspace", 400)


def _check_evidence_scope(db: Session, workspace_id: str, evidence: list[dict[str, Any]], project_id: str | None = None) -> None:
    """Validate evidence references against existence and resource boundaries.

    A reference whose type is recognised must point at an existing object
    (fabricated IDs are rejected, BUG-004), and any existing object is always
    checked against the current workspace/project boundary.
    """
    recognised_types = {
        "insight", "decision", "decision_proposal", "feedback_cluster", "feedback_theme",
        "document", "analysis_artifact", "artifact", "analysis", "analysis_run",
        "dataset", "data", "dataset_version", "data_version", "feedback", "feedback_item",
        "document_version", "task", "feedback_note",
    }
    for reference in evidence:
        raw_type, ref_id = reference.get("type"), reference.get("id")
        ref_type = str(raw_type).strip().lower().replace("-", "_") if raw_type else ""
        if not ref_type or not ref_id:
            continue
        item: Any = None
        owner_workspace: str | None = None
        owner_project: str | None = None
        if ref_type == "insight":
            item = db.get(Insight, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"decision", "decision_proposal"}:
            item = db.get(DecisionProposal, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"feedback_cluster", "feedback_theme"}:
            item = db.get(FeedbackCluster, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type == "document":
            item = db.get(Document, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"analysis_artifact", "artifact"}:
            item = db.get(AnalysisArtifact, ref_id)
            run = db.get(AnalysisRun, item.analysis_run_id) if item else None
            owner_workspace = run.workspace_id if run else None
            owner_project = run.project_id if run else None
        elif ref_type in {"analysis", "analysis_run"}:
            item = db.get(AnalysisRun, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"dataset", "data"}:
            item = db.get(Dataset, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"dataset_version", "data_version"}:
            item = db.get(DatasetVersion, ref_id)
            dataset = db.get(Dataset, item.dataset_id) if item else None
            owner_workspace = dataset.workspace_id if dataset else None
            owner_project = dataset.project_id if dataset else None
        elif ref_type in {"feedback", "feedback_item"}:
            item = db.get(FeedbackItem, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type == "feedback_note":
            item = db.get(FeedbackNote, ref_id)
            project = db.get(Project, item.project_id) if item and item.project_id else None
            owner_workspace = project.workspace_id if project else None
            owner_project = item.project_id if item else None
        elif ref_type == "document_version":
            item = db.get(DocumentVersion, ref_id)
            document = db.get(Document, item.document_id) if item else None
            owner_workspace = document.workspace_id if document else None
            owner_project = document.project_id if document else None
        elif ref_type == "task":
            item = db.get(Task, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        if ref_type in recognised_types and item is None:
            raise error("VALIDATION_ERROR", f"Evidence {ref_type} '{ref_id}' does not exist", 400)
        if owner_workspace is not None and owner_workspace != workspace_id:
            raise error("FORBIDDEN", "Evidence is outside the current workspace", 403)
        if project_id and owner_project is not None and owner_project != project_id:
            raise error("FORBIDDEN", "Evidence is outside the current project", 403)


def _require_confirmed_insight_refs(
    db: Session,
    workspace_id: str,
    evidence: list[dict[str, Any]],
    project_id: str | None = None,
) -> None:
    """Allow documents to cite only confirmed insights.

    Evidence scope and existence are checked first so a caller cannot use the
    status check to probe an insight in another workspace or project.  Other
    evidence types keep their existing lifecycle semantics.
    """

    _check_evidence_scope(db, workspace_id, evidence, project_id)
    for reference in evidence:
        if not isinstance(reference, dict):
            continue
        ref_type = str(reference.get("type") or "").strip().lower().replace("-", "_")
        if ref_type != "insight":
            continue
        ref_id = str(reference.get("id") or "")
        insight = db.get(Insight, ref_id)
        # _check_evidence_scope above guarantees that a recognised reference
        # exists and belongs to the requested scope.
        if insight is not None and insight.status != "confirmed":
            raise error(
                "INSIGHT_NOT_CONFIRMED",
                "Only confirmed insights can be referenced by a document",
                409,
                {"insight_id": ref_id, "status": insight.status},
            )


def _require_nonempty_evidence(evidence: Any, *, subject: str = "Insight") -> list[dict[str, Any]]:
    """Require at least one structured reference before a claim can be stored.

    AI claims and human-entered claims share the same persistence contract.  A
    reference must carry both a type and an id so the scope validator can prove
    that it points to a real workspace resource.
    """

    if not isinstance(evidence, list) or not evidence:
        raise error("VALIDATION_ERROR", f"{subject} requires at least one evidence reference", 400)
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict) or not str(item.get("type") or "").strip() or not str(item.get("id") or "").strip():
            raise error("VALIDATION_ERROR", f"{subject} evidence references require type and id", 400)
        normalized.append({"type": str(item["type"])[:80], "id": str(item["id"])[:255]})
    return normalized


def _linked_resource_scope(db: Session, link_type: str, target_id: str) -> tuple[str, str | None]:
    """Resolve a task link and return its workspace/project ownership."""
    normalized = link_type.strip().lower()
    target: Any = None
    project_id: str | None = None

    if normalized in {"dataset", "data"}:
        target = db.get(Dataset, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"dataset_version", "dataset-version", "data_version", "data-version"}:
        version = db.get(DatasetVersion, target_id)
        dataset = db.get(Dataset, version.dataset_id) if version is not None else None
        target = dataset
        if target is not None:
            project_id = target.project_id
    elif normalized in {"analysis", "analysis_run", "analysis-run"}:
        target = db.get(AnalysisRun, target_id)
        if target is None and normalized == "analysis":
            artifact = db.get(AnalysisArtifact, target_id)
            target = db.get(AnalysisRun, artifact.analysis_run_id) if artifact is not None else None
        if target is not None:
            project_id = target.project_id
    elif normalized in {"analysis_artifact", "analysis-artifact", "artifact"}:
        artifact = db.get(AnalysisArtifact, target_id)
        run = db.get(AnalysisRun, artifact.analysis_run_id) if artifact is not None else None
        target = run
        if target is not None:
            project_id = target.project_id
    elif normalized in {"feedback", "feedback_item", "feedback-item"}:
        target = db.get(FeedbackItem, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"feedback_cluster", "feedback-cluster", "feedback_theme", "feedback-theme"}:
        target = db.get(FeedbackCluster, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized == "insight":
        target = db.get(Insight, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"decision", "decision_proposal", "decision-proposal"}:
        target = db.get(DecisionProposal, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"document", "doc"}:
        target = db.get(Document, target_id)
        if target is not None:
            project_id = target.project_id
    else:
        raise error("VALIDATION_ERROR", f"Unsupported task link type: {link_type}", 422)

    if target is None:
        raise error("NOT_FOUND", "Linked object not found", 404)
    if isinstance(target, Dataset) and target.deleted_at is not None:
        raise error("NOT_FOUND", "Linked dataset not found", 404)
    workspace_id = getattr(target, "workspace_id", None)
    if not workspace_id:
        raise error("VALIDATION_ERROR", "Linked object has no workspace scope", 422)
    return workspace_id, project_id


def workspace_for_user(db: Session, user: User, workspace_id: str | None = None) -> Workspace:
    if workspace_id:
        membership(db, user, workspace_id)
        workspace = db.get(Workspace, workspace_id)
    else:
        workspace = db.scalar(select(Workspace).join(WorkspaceMember).where(WorkspaceMember.user_id == user.id).order_by(Workspace.created_at).limit(1))
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    return workspace


def _job(db: Session, workspace_id: str, job_type: str, input_json: dict[str, Any] | None = None, status_value: str = "queued", result_type: str | None = None, result_id: str | None = None) -> Job:
    payload = {"_retryable": True, **(input_json or {})}
    job = Job(workspace_id=workspace_id, job_type=job_type, status=status_value, progress=0, current_step="queued", input_json=payload, result_type=result_type, result_id=result_id, attempt_count=0)
    db.add(job)
    return job


def _job_payload(job: Job) -> dict[str, Any]:
    source = job.input_json if isinstance(job.input_json, dict) else {}
    public_input = {key: value for key, value in source.items() if not str(key).startswith("_")}
    can_retry = bool(source.get("_retryable", True)) and job.status in {"failed", "cancelled"} and job_executor.has_handler(job.job_type)
    return model_dict(job, {"input_json": public_input, "retryable": can_retry})


def _cleaning_operation_rows(version: DatasetVersion) -> list[CleaningOperation]:
    """Return operation history touching a version, ordered oldest first."""

    rows = {item.id: item for item in (*version.cleaning_operations_from, *version.cleaning_operations_to)}
    return sorted(rows.values(), key=lambda item: item.created_at)


def _version_payload(version: DatasetVersion, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize a dataset version without exposing the internal storage path."""

    payload = model_dict(version, extra)
    payload.pop("storage_path", None)
    return payload


def _safe_name(name: str) -> str:
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base)[:255] or "upload.csv"


# ``.xls`` is deliberately absent: reading it needs ``xlrd``, which is neither
# declared nor installed, so accepting it only moves the failure from a clear 400
# into the parse job where the user sees a dead version instead of an error.
UPLOAD_SUFFIXES = frozenset({".csv", ".xlsx"})
UPLOAD_SUFFIX_MESSAGE = "Only CSV and XLSX files are supported"


def _reject_unsupported_upload(filename: str) -> None:
    if Path(filename).suffix.lower() not in UPLOAD_SUFFIXES:
        raise error("VALIDATION_ERROR", UPLOAD_SUFFIX_MESSAGE, 400)


def _read_dataframe(path: str | Path, file_name: str, worksheet_name: str | None = None) -> pd.DataFrame:
    _require_pandas()
    suffix = Path(file_name).suffix.lower()
    if suffix == ".csv":
        # Uploaded CSVs commonly arrive from spreadsheet tools with a BOM or a
        # Chinese locale encoding. Try the documented encodings in a deterministic
        # order and only fail after each decoder has been attempted.
        last_error: Exception | None = None
        for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
            try:
                return pd.read_csv(path, encoding=encoding)
            except UnicodeDecodeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return pd.read_csv(path)
    if suffix == ".xlsx":
        return pd.read_excel(path, sheet_name=worksheet_name or 0)
    raise error("VALIDATION_ERROR", UPLOAD_SUFFIX_MESSAGE, 400)


def _type_name(series: pd.Series) -> str:
    """Map a pandas dtype to the product's documented display vocabulary.

    The data dictionary exposes string/integer/float/boolean/datetime/category
    (BUG-014); internal analytics names such as "numeric" must not leak out.
    """
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    inferred = infer_column_type(series)
    if inferred == "datetime":
        return "datetime"
    if inferred == "numeric":
        return "float"
    if inferred == "boolean":
        return "boolean"
    return "string"


_IDENTIFIER_NAME = re.compile(r"(?:^|[_-])(id|uuid|guid)$", re.IGNORECASE)


def _column_schema(df: pd.DataFrame) -> list[dict[str, Any]]:
    _require_pandas()
    result: list[dict[str, Any]] = []
    role_names = {
        "user_id": "user_id",
        "event_time": "event_time",
        "event_name": "event_name",
        "date": "date",
        "channel": "segment",
        "version": "version",
        "feedback_text": "feedback_text",
        "rating": "rating",
    }
    for ordinal, name in enumerate(df.columns):
        col = str(name)
        series = df[name]
        nullable = bool(series.isna().any())
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            nullable = nullable or bool(series.astype("string").str.strip().eq("").any())
        unique_ratio = float(series.nunique(dropna=True) / max(1, len(series)))
        inferred = _type_name(series)
        # Identifier columns are labels, not measures: an "id"-named column that
        # happens to parse as numbers must still be treated as a string key.
        if inferred in {"integer", "float"} and _IDENTIFIER_NAME.search(col):
            inferred = "string"
        result.append({"name": col, "display_name": col, "inferred_type": inferred, "confirmed_type": None, "nullable": nullable, "unique_ratio": unique_ratio, "mapping_role": role_names.get(col.lower()), "ordinal": ordinal})
    return result


def _quality_summary(df: pd.DataFrame) -> tuple[float, str, dict[str, Any]]:
    _require_pandas()
    report = assess_quality(df).to_dict()
    missing = {
        str(item["name"]): int(item["missing_count"])
        for item in report.get("columns", [])
        if int(item.get("missing_count", 0)) > 0
    }
    type_errors = {
        str(item["column"]): int(item["invalid_count"])
        for item in report.get("type_errors", [])
    }
    anomalies = {
        str(column): int(details.get("count", 0))
        for column, details in (report.get("outliers") or {}).items()
        if int(details.get("count", 0)) > 0
    }
    # Keep the compact keys used by the V1 API while retaining the richer quality
    # report produced by the analytics package for the UI and Copilot.
    report.update({
        "missing_values": missing,
        "duplicate_rows": int(report.get("duplicate_rows", 0)),
        "type_errors": type_errors,
        "anomalies": anomalies,
        "sample": report.get("sample") or _json_records(df.head(5)),
    })
    return float(report.get("overall_score", 0)), str(report.get("status", "needs_review")), report


def _json_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    _require_pandas()
    clean = df.astype(object).where(pd.notna(df), None)
    return [serialize(row) for row in clean.to_dict(orient="records")]


def _cleaning_operation_parameters(operation: dict[str, Any]) -> dict[str, Any]:
    raw = operation.get("parameters") or {}
    params = dict(raw) if isinstance(raw, dict) else {}
    for key in ("columns", "subset", "column", "value", "target_type"):
        if key in operation and key not in params:
            params[key] = operation[key]
    return params


def _normalise_cleaning_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the public cleaning request into replayable operation payloads."""

    normalised: list[dict[str, Any]] = []
    for operation in operations:
        typ = str(operation.get("operation") or operation.get("type") or "").lower()
        params = _cleaning_operation_parameters(operation)
        if typ in {"dropna", "drop_missing"}:
            typ = "drop_missing"
        elif typ in {"fillna", "fill_missing"}:
            typ = "fill_missing"
        elif typ in {"coerce_numeric", "coerce_type"}:
            typ = "coerce_type" if typ == "coerce_type" else "coerce_numeric"
            if typ == "coerce_numeric":
                params.setdefault("target_type", "numeric")
        # The API accepts the concise top-level form used by the web client.
        # Copy those values into parameters so the persisted row is sufficient
        # to replay the operation without relying on the original request.
        normalised.append({**operation, "operation": typ, "parameters": params})
    return normalised


def _apply_cleaning(df: pd.DataFrame, operations: list[dict[str, Any]]) -> tuple[pd.DataFrame, dict[str, Any]]:
    _require_pandas()
    normalised = _normalise_cleaning_operations(operations)
    affected: list[str] = []
    for operation in normalised:
        params = dict(operation.get("parameters") or {})
        affected.extend([str(item) for item in (operation.get("columns") or params.get("columns") or params.get("subset") or ([operation.get("column")] if operation.get("column") else []))])
    try:
        result = apply_quality_cleaning(df, normalised)
    except (KeyError, TypeError, ValueError) as exc:
        raise error("VALIDATION_ERROR", f"Invalid cleaning operation: {exc}", 400) from exc
    removed = max(0, len(df) - len(result))
    risks: list[str] = []
    if not len(result):
        risks.append("Cleaning operations would remove every row")
    if removed:
        risks.append(f"{removed} rows will be removed from the derived version")
    # ``estimated_dropped_rows`` is the documented field name (BUG-016);
    # ``estimated_deleted_rows`` is kept as a compatibility alias.
    return result, {"estimated_dropped_rows": int(removed), "estimated_deleted_rows": int(removed), "affected_fields": sorted(set(item for item in affected if item)), "sample_before": _json_records(df.head(5)), "sample_after": _json_records(result.head(5)), "risks": risks}


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ai-product-workspace-api", "version": app.version}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)) -> dict[str, Any]:
    checks: dict[str, Any] = {"database": False, "data_root": False, "database_backend": database.engine.url.get_backend_name(), "database_fallback": database.USING_FALLBACK_SQLITE}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    try:
        path = settings.data_path
        checks["data_root"] = path.is_dir() and (path / "uploads").is_dir()
    except Exception:
        checks["data_root"] = False
    # A fallback is intentionally *not* ready.  Returning 200 here previously
    # made an empty SQLite store look like the configured MySQL database.
    ready = bool(checks["database"]) and bool(checks["data_root"]) and not bool(checks["database_fallback"])
    payload = {"status": "ready" if ready else "not_ready", "checks": checks}
    if not ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get("/health/ai")
def health_ai() -> dict[str, Any]:
    return {"status": "configured" if settings.deepseek_api_key else "not_configured", "provider": "deepseek", "model": settings.deepseek_model, "base_url": settings.deepseek_base_url}


@app.post("/api/v1/auth/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    email = str(body.email).lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise error("VALIDATION_ERROR", "Email is already registered", 400)
    user = User(email=email, name=body.name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    workspace = Workspace(name=body.workspace_name, owner_id=user.id)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    audit(db, workspace.id, user.id, "auth.registered", "user", user.id)
    db.commit()
    token = create_access_token(user.id)
    return ok({"access_token": token, "token_type": "bearer", "user": model_dict(user), "workspace": _workspace_payload(workspace, "owner")})


@app.post("/api/v1/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.email == str(body.email).lower()))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        if user is not None:
            audit_user_workspaces(db, user.id, "auth.login_failed", "user", user.id, actor_type="anonymous")
            db.commit()
        raise error("UNAUTHENTICATED", "Incorrect email or password", 401)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    audit_user_workspaces(db, user.id, "auth.login_succeeded", "user", user.id)
    db.commit()
    return ok({"access_token": create_access_token(user.id), "token_type": "bearer", "user": model_dict(user)})


@app.post("/api/v1/auth/refresh")
def refresh(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return ok({"access_token": create_access_token(user.id), "token_type": "bearer"})


@app.get("/api/v1/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    workspaces = [db.get(Workspace, row.workspace_id) for row in rows]
    return ok(
        {
            "user": model_dict(user),
            "workspaces": [
                _workspace_payload(ws, row.role)
                for ws, row in zip(workspaces, rows, strict=True)
                if ws is not None
            ],
        }
    )


@app.get("/api/v1/workspaces")
def list_workspaces(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    members = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    rows = []
    for member in members:
        workspace = db.get(Workspace, member.workspace_id)
        if workspace:
            rows.append(_workspace_payload(workspace, member.role))
    return ok(rows, page=1, page_size=len(rows), total=len(rows))


@app.get("/api/v1/audit-logs")
def list_audit_logs(
    workspace_id: str = Query(..., min_length=1, max_length=36),
    target_type: str | None = Query(default=None, min_length=1, max_length=80),
    target_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pagination: tuple[int, int] = Depends(page_params),
) -> dict[str, Any]:
    """List immutable audit events within an authorized workspace boundary."""

    membership(db, user, workspace_id)
    filters = [AuditLog.workspace_id == workspace_id]
    if target_type is not None:
        filters.append(AuditLog.target_type == target_type)
    if target_id is not None:
        filters.append(AuditLog.target_id == target_id)

    page, page_size = pagination
    total = db.scalar(select(func.count()).select_from(AuditLog).where(*filters)) or 0
    rows = db.scalars(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ok([model_dict(row) for row in rows], page=page, page_size=page_size, total=total)


@app.patch("/api/v1/workspaces/{workspace_id}")
def patch_workspace(workspace_id: str, body: WorkspacePatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    member = membership(db, user, workspace_id, "owner")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    changed_fields: list[str] = []
    if body.name is not None:
        workspace_name = body.name.strip()
        if not workspace_name:
            raise error("VALIDATION_ERROR", "Workspace name cannot be blank", 400)
        workspace.name = workspace_name
        changed_fields.append("name")
    if body.settings is not None:
        _, setting_fields = _merge_workspace_settings(workspace, body.settings)
        changed_fields.extend(f"settings.{field}" for field in setting_fields)
    audit(db, workspace_id, user.id, "workspace.updated", "workspace", workspace_id, {"changed_fields": changed_fields})
    db.commit()
    return ok(_workspace_payload(workspace, member.role))


@app.get("/api/v1/workspaces/{workspace_id}/settings")
def get_workspace_settings(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    member = membership(db, user, workspace_id)
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    current = _workspace_settings(workspace)
    # Settings values are exposed both nested and flattened (BUG-022): API
    # clients read the documented top-level fields, the web client reads
    # ``settings``.
    return ok(
        {
            **current,
            "workspace_id": workspace.id,
            "name": workspace.name,
            "role": member.role,
            "can_edit": member.role == "owner",
            "settings": current,
        }
    )


@app.patch("/api/v1/workspaces/{workspace_id}/settings")
def patch_workspace_settings(workspace_id: str, body: WorkspaceSettingsPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    member = membership(db, user, workspace_id, "owner")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    merged, changed_fields = _merge_workspace_settings(workspace, body)
    audit(db, workspace_id, user.id, "workspace.settings_updated", "workspace", workspace_id, {"changed_fields": changed_fields})
    db.commit()
    return ok(
        {
            **merged,
            "workspace_id": workspace.id,
            "name": workspace.name,
            "role": member.role,
            "can_edit": True,
            "settings": merged,
        }
    )


@app.get("/api/v1/workspaces/{workspace_id}/metrics")
def list_metric_definitions(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id)
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    _migrate_legacy_metric_dictionary(db, workspace)
    rows = db.scalars(
        select(MetricDefinition)
        .where(MetricDefinition.workspace_id == workspace_id, MetricDefinition.deleted_at.is_(None))
        .order_by(MetricDefinition.name, MetricDefinition.created_at)
    ).all()
    return ok([_metric_payload(row) for row in rows], page=1, page_size=len(rows), total=len(rows))


@app.post("/api/v1/workspaces/{workspace_id}/metrics")
def create_metric_definition(workspace_id: str, body: MetricDefinitionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    name = body.name.strip()
    definition = body.definition.strip()
    if not name or not definition:
        raise error("VALIDATION_ERROR", "Metric name and definition cannot be blank", 400)
    duplicate = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.workspace_id == workspace_id,
            MetricDefinition.deleted_at.is_(None),
            func.lower(MetricDefinition.name) == name.lower(),
        )
    )
    if duplicate is not None:
        raise error("VALIDATION_ERROR", "A metric with this name already exists", 400)
    metric = MetricDefinition(
        workspace_id=workspace_id,
        name=name,
        definition=definition,
        category=body.category,
        numerator=body.numerator.strip(),
        denominator=body.denominator.strip(),
        unit=body.unit.strip(),
        aggregation_period=body.aggregation_period,
        field_mapping_json=body.field_mapping,
        display_format=body.display_format,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(metric)
    db.flush()
    audit(db, workspace_id, user.id, "metric_definition.created", "metric_definition", metric.id, {"name": metric.name, "category": metric.category})
    db.commit()
    return ok(_metric_payload(metric))


@app.patch("/api/v1/workspaces/{workspace_id}/metrics/{metric_id}")
def patch_metric_definition(workspace_id: str, metric_id: str, body: MetricDefinitionPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            MetricDefinition.workspace_id == workspace_id,
            MetricDefinition.deleted_at.is_(None),
        )
    )
    if metric is None:
        raise error("NOT_FOUND", "Metric definition not found", 404)
    updates = body.model_dump(exclude_unset=True)
    if "name" in updates:
        name = str(updates["name"]).strip()
        if not name:
            raise error("VALIDATION_ERROR", "Metric name cannot be blank", 400)
        duplicate = db.scalar(
            select(MetricDefinition).where(
                MetricDefinition.workspace_id == workspace_id,
                MetricDefinition.id != metric.id,
                MetricDefinition.deleted_at.is_(None),
                func.lower(MetricDefinition.name) == name.lower(),
            )
        )
        if duplicate is not None:
            raise error("VALIDATION_ERROR", "A metric with this name already exists", 400)
        updates["name"] = name
    for text_field in ("definition", "numerator", "denominator", "unit"):
        if text_field in updates:
            updates[text_field] = str(updates[text_field]).strip()
    if "definition" in updates and not updates["definition"]:
        raise error("VALIDATION_ERROR", "Metric definition cannot be blank", 400)
    if "field_mapping" in updates:
        updates["field_mapping_json"] = updates.pop("field_mapping")
    for field, value in updates.items():
        setattr(metric, field, value)
    metric.updated_by = user.id
    metric.updated_at = now()
    audit(db, workspace_id, user.id, "metric_definition.updated", "metric_definition", metric.id, {"changed_fields": sorted(updates)})
    db.commit()
    return ok(_metric_payload(metric))


@app.delete("/api/v1/workspaces/{workspace_id}/metrics/{metric_id}")
def delete_metric_definition(workspace_id: str, metric_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    membership(db, user, workspace_id, "owner")
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            MetricDefinition.workspace_id == workspace_id,
            MetricDefinition.deleted_at.is_(None),
        )
    )
    if metric is None:
        raise error("NOT_FOUND", "Metric definition not found", 404)
    metric.deleted_at = now()
    metric.updated_at = now()
    metric.updated_by = user.id
    audit(db, workspace_id, user.id, "metric_definition.deleted", "metric_definition", metric.id, {"name": metric.name})
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# V1.1 exposes context resources at the top level.  Keep the workspace-scoped
# routes above as compatibility endpoints, while these aliases infer the
# caller's first workspace when ``workspace_id`` is omitted.  An explicit
# query parameter remains available for users who belong to multiple
# workspaces.
@app.get("/api/v1/settings")
def get_settings_alias(
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return get_workspace_settings(workspace.id, user, db)


@app.patch("/api/v1/settings")
def patch_settings_alias(
    body: WorkspaceSettingsPatch,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return patch_workspace_settings(workspace.id, body, user, db)


@app.get("/api/v1/metrics")
def list_metrics_alias(
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pagination: tuple[int, int] = Depends(page_params),
) -> dict[str, Any]:
    # ``list_metric_definitions`` currently returns all rows for the selected
    # workspace.  Preserve that response shape for the new top-level route;
    # pagination is accepted for contract compatibility with other list APIs.
    workspace = workspace_for_user(db, user, workspace_id)
    return list_metric_definitions(workspace.id, user, db)


@app.post("/api/v1/metrics")
def create_metric_alias(
    body: MetricDefinitionCreate,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return create_metric_definition(workspace.id, body, user, db)


@app.patch("/api/v1/metrics/{metric_id}")
def patch_metric_alias(
    metric_id: str,
    body: MetricDefinitionPatch,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return patch_metric_definition(workspace.id, metric_id, body, user, db)


@app.delete("/api/v1/metrics/{metric_id}")
def delete_metric_alias(
    metric_id: str,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    workspace = workspace_for_user(db, user, workspace_id)
    return delete_metric_definition(workspace.id, metric_id, user, db)


@app.get("/api/v1/workspaces/{workspace_id}/members")
def list_members(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id)
    members = db.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)).all()
    rows = []
    for member in members:
        member_user = db.get(User, member.user_id)
        rows.append({**model_dict(member), "user": model_dict(member_user) if member_user else None})
    return ok(rows, page=1, page_size=len(rows), total=len(rows))


@app.post("/api/v1/workspaces/{workspace_id}/members")
def add_member(workspace_id: str, body: MemberCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    target = db.scalar(select(User).where(User.email == str(body.email).lower()))
    if target is None:
        raise error("NOT_FOUND", "User with this email is not registered", 404)
    if db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == target.id)):
        raise error("VALIDATION_ERROR", "User is already a member", 400)
    member = WorkspaceMember(workspace_id=workspace_id, user_id=target.id, role=body.role)
    db.add(member)
    audit(db, workspace_id, user.id, "workspace.member_added", "user", target.id)
    db.commit()
    return ok({**model_dict(member), "user": model_dict(target)})


@app.patch("/api/v1/workspaces/{workspace_id}/members/{member_id}")
def patch_member(workspace_id: str, member_id: str, body: MemberPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.id == member_id, WorkspaceMember.workspace_id == workspace_id))
    if member is None:
        raise error("NOT_FOUND", "Member not found", 404)
    member.role = body.role
    audit(db, workspace_id, user.id, "workspace.member_role_changed", "user", member.user_id, {"role": body.role})
    db.commit()
    return ok(model_dict(member))


# Deleting a project is a soft delete so an accidental delete stays recoverable.
# Defined once here because the list filter, the delete handler, and the restore
# handler must agree on the sentinel.
ARCHIVED_PROJECT_STATUS = "archived"


@app.get("/api/v1/projects")
def list_projects(workspace_id: str | None = Query(default=None), include_archived: bool = Query(default=False), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    page, page_size = pagination
    # DELETE is a hard delete, so nothing lands here by being deleted.  A project
    # can still be archived by hand via PATCH status="archived" to hide finished
    # work from the list; ``include_archived`` brings those back into view.
    conditions = [] if include_archived else [Project.status != ARCHIVED_PROJECT_STATUS]
    if workspace_id:
        membership(db, user, workspace_id)
        rows = db.scalars(select(Project).where(Project.workspace_id == workspace_id, *conditions).order_by(Project.created_at.desc())).all()
    else:
        ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Project).where(Project.workspace_id.in_(ids), *conditions).order_by(Project.created_at.desc())).all() if ids else []
    return paged([model_dict(row) for row in rows], page, page_size, len(rows))


@app.post("/api/v1/projects")
def create_project(body: ProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, body.workspace_id)
    membership(db, user, workspace.id, "editor")
    project = Project(workspace_id=workspace.id, owner_id=user.id, name=body.name, description=body.description, status=body.status, goal_statement=body.goal_statement)
    db.add(project)
    db.flush()
    audit(db, workspace.id, user.id, "project.created", "project", project.id)
    db.commit()
    return ok(model_dict(project))


@app.get("/api/v1/projects/{project_id}")
def get_project(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return ok(model_dict(project_for(db, user, project_id)))


@app.patch("/api/v1/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    for field in ("name", "description", "status", "goal_statement"):
        value = getattr(body, field)
        if value is not None:
            setattr(project, field, value)
    audit(db, project.workspace_id, user.id, "project.updated", "project", project.id)
    db.commit()
    return ok(model_dict(project))


def _safe_data_file(relative_path: str) -> Path | None:
    """Resolve a stored file inside DATA_ROOT, or None if it escapes the root.

    Deletion walks ``storage_path`` values straight from the database, so this
    refuses anything resolving outside the data root instead of unlinking it.
    """

    if not relative_path:
        return None
    root = settings.data_path.resolve()
    candidate = (root / str(relative_path)).resolve()
    if root != candidate and root not in candidate.parents:
        return None
    return candidate


def _purge_project(db: Session, project: Project) -> dict[str, int]:
    """Delete a project's rows and uploaded files, returning what was removed.

    SQLite runs with ``PRAGMA foreign_keys=ON`` (db.py:21), so order matters.
    Most children declare ``ondelete="CASCADE"``, but ``AnalysisRun`` points at
    ``dataset_versions.id`` with no ``ondelete`` (models.py:278), so analysis
    rows must go before the versions they reference.  Everything is issued in
    one transaction; the caller commits.
    """

    dataset_ids = list(db.scalars(select(Dataset.id).where(Dataset.project_id == project.id)).all())
    version_rows = (
        db.execute(
            select(DatasetVersion.id, DatasetVersion.storage_path).where(DatasetVersion.dataset_id.in_(dataset_ids))
        ).all()
        if dataset_ids
        else []
    )
    version_ids = [row[0] for row in version_rows]
    counts = {"datasets": len(dataset_ids), "versions": len(version_ids)}

    if version_ids:
        counts["quality_reports"] = int(
            db.scalar(
                select(func.count())
                .select_from(DataQualityReport)
                .where(DataQualityReport.dataset_version_id.in_(version_ids))
            )
            or 0
        )
        # Analysis runs reference versions without ON DELETE, so clear them first.
        run_ids = list(db.scalars(select(AnalysisRun.id).where(AnalysisRun.dataset_version_id.in_(version_ids))).all())
        if run_ids:
            db.execute(delete(AnalysisArtifact).where(AnalysisArtifact.analysis_run_id.in_(run_ids)))
            db.execute(delete(AnalysisRun).where(AnalysisRun.id.in_(run_ids)))
        db.execute(delete(CleaningOperation).where(CleaningOperation.source_version_id.in_(version_ids)))
        db.execute(delete(CleaningOperation).where(CleaningOperation.result_version_id.in_(version_ids)))
        db.execute(delete(DataQualityReport).where(DataQualityReport.dataset_version_id.in_(version_ids)))
        db.execute(delete(DataColumn).where(DataColumn.dataset_version_id.in_(version_ids)))
        db.execute(delete(DatasetVersion).where(DatasetVersion.id.in_(version_ids)))
    else:
        counts["quality_reports"] = 0

    if dataset_ids:
        db.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))

    # Insight.task_id (models.py:367) and DecisionProposal.task_id
    # (models.py:437) reference tasks.id with no ondelete, while Project.tasks
    # carries an ORM delete-orphan cascade (models.py:128).  db.delete(project)
    # therefore deletes the Task rows while these still point at them, so they
    # must be cleared first or SQLite raises FOREIGN KEY constraint failed.
    db.execute(delete(Insight).where(Insight.project_id == project.id))
    db.execute(delete(DecisionProposal).where(DecisionProposal.project_id == project.id))

    # Approvals are addressed by (target_type, target_id) rather than a foreign
    # key, so no cascade reaches them.
    orphan_targets = {"project": [project.id], "dataset": dataset_ids, "dataset_version": version_ids}
    for target_type, ids in orphan_targets.items():
        if ids:
            db.execute(
                delete(ApprovalRequest).where(
                    ApprovalRequest.target_type == target_type, ApprovalRequest.target_id.in_(ids)
                )
            )

    db.delete(project)
    db.flush()

    removed_files = 0
    for _, storage_path in version_rows:
        if not storage_path:
            continue
        path = settings.data_path / storage_path
        try:
            path.unlink()
            removed_files += 1
        except FileNotFoundError:
            continue
        except OSError:
            # A locked or already-gone file must not abort the delete; the row is
            # gone either way and a stray file is recoverable disk noise.
            continue
    counts["files"] = removed_files
    return counts


@app.delete("/api/v1/projects/{project_id}")
def delete_project(
    project_id: str,
    body: DatasetDeleteRequest | None = Body(default=None),
    confirm: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Permanently delete a project with its datasets, reports, and versions.

    This is a hard delete: uploaded files are unlinked and no restore exists.
    It mirrors the confirmation contract already used by DELETE /datasets/{id}
    (main.py:1939) so an accidental call cannot destroy data.
    """

    project = project_for(db, user, project_id, "owner")
    workspace_id = project.workspace_id
    name = project.name

    confirmation = body.confirm if body is not None else confirm
    confirmed = confirmation is True
    if isinstance(confirmation, str):
        normalized = confirmation.strip()
        confirmed = normalized == project.id or normalized.lower() in {"true", "1", "yes"}
    if not confirmed:
        raise error(
            "CONFIRMATION_REQUIRED",
            "Project deletion is permanent and requires confirm=true or confirm=<project_id>",
            409,
            {"project_id": project_id},
        )

    counts = _purge_project(db, project)
    audit(db, workspace_id, user.id, "project.purged", "project", project_id, {"name": name, "removed": counts})
    db.commit()
    return ok({"id": project_id, "deleted": True, "removed": counts})


@app.get("/api/v1/projects/{project_id}/overview")
def project_overview(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id)
    tasks = db.scalars(select(Task).where(Task.project_id == project.id)).all()
    datasets = db.scalars(select(Dataset).where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None))).all()
    workflow = _project_workflow_status(db, project)
    return ok({"project": model_dict(project), "workflow_status": "analysis_ready" if datasets else "draft", "workflow": workflow, "task_counts": {state: sum(1 for task in tasks if task.status == state) for state in {task.status for task in tasks}}, "tasks": [model_dict(task) for task in tasks[:10]], "datasets": [model_dict(dataset, {"versions": len(dataset.versions)}) for dataset in datasets]})


# V1.1 keeps the old project overview contract but adds a real, metadata-only
# progress calculation for the five-step pipeline.  Keeping this in one helper
# also gives the dedicated workflow-status endpoint and legacy overview exactly
# the same semantics.
# Advisory only.  These roles unlock richer analysis types (trend/funnel/
# retention) when present, but a cleaned business table legitimately has none of
# them, so their absence must never block the pipeline.  This is the single
# source of truth: the frontend reads it from the workflow-status payload rather
# than keeping its own copy.
SUGGESTED_FIELD_ROLES = ("user_id", "event_time", "event_name")


def _project_workflow_status(db: Session, project: Project) -> dict[str, Any]:
    datasets = db.scalars(
        select(Dataset)
        .where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None))
        .order_by(Dataset.created_at.desc())
    ).all()
    versions: list[DatasetVersion] = []
    for dataset in datasets:
        # The latest non-failed version is the version a user sees in the
        # pipeline.  A processing/failed upload must not mark step 1 complete.
        candidates = sorted(dataset.versions, key=lambda item: item.version_number, reverse=True)
        version = next((item for item in candidates if item.status not in {"failed", "error"}), None)
        if version is not None:
            versions.append(version)

    selected_version = versions[0] if versions else None
    confirmed_roles = sorted(
        {
            str(column.mapping_role).strip()
            for column in (selected_version.columns if selected_version else [])
            if column.mapping_role
        }
    )
    # Reported for UI hinting only.  Deliberately not part of `data_complete`.
    missing_roles = [role for role in SUGGESTED_FIELD_ROLES if role not in confirmed_roles]
    # An auto-accepted schema counts as reviewed.  The parse job stamps
    # ``schema_auto_accepted_at`` (see _run_auto_analyses) so a plain upload
    # advances on its own; an explicit human review still stamps
    # ``schema_reviewed_at`` and is reported separately below.
    schema_reviewed = bool(
        selected_version
        and (selected_version.schema_reviewed_at is not None or selected_version.schema_auto_accepted_at is not None)
    )
    schema_reviewed_by_human = bool(selected_version and selected_version.schema_reviewed_at is not None)
    # `status` still gates: a failed parse never advances, auto-accepted or not.
    data_complete = bool(selected_version and selected_version.status in {"ready", "confirmed", "succeeded"} and schema_reviewed)

    quality_complete = bool(selected_version and selected_version.quality_report is not None)
    analysis_complete = db.scalar(
        select(func.count(AnalysisRun.id)).where(
            AnalysisRun.project_id == project.id,
            AnalysisRun.status == "succeeded",
        )
    ) > 0
    insight_complete = db.scalar(
        select(func.count(Insight.id)).where(
            Insight.project_id == project.id,
            Insight.status == "confirmed",
        )
    ) > 0
    delivery_complete = db.scalar(
        select(func.count(DocumentVersion.id))
        .select_from(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.project_id == project.id)
    ) > 0

    step_values = [data_complete, quality_complete, analysis_complete, insight_complete, delivery_complete]
    step_names = ("data", "quality", "analysis", "insights", "delivery")
    steps = [
        {
            "key": key,
            "complete": complete,
            "progress": 100 if complete else 0,
        }
        for key, complete in zip(step_names, step_values, strict=True)
    ]
    completed_steps = sum(step_values)
    next_step = next((key for key, complete in zip(step_names, step_values, strict=True) if not complete), None)
    return {
        "steps": steps,
        "completed_steps": completed_steps,
        "total_steps": len(steps),
        "progress_percent": int(round(completed_steps / len(steps) * 100)),
        "next_step": next_step,
        "selected_dataset_version_id": selected_version.id if selected_version else None,
        "confirmed_roles": confirmed_roles,
        "missing_roles": missing_roles,
        "suggested_roles": list(SUGGESTED_FIELD_ROLES),
        "schema_reviewed": schema_reviewed,
        "schema_reviewed_by_human": schema_reviewed_by_human,
        "schema_auto_accepted": bool(selected_version and selected_version.schema_auto_accepted_at is not None),
    }


@app.get("/api/v1/projects/{project_id}/workflow-status")
def project_workflow_status(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return truthful V1.1 pipeline progress for a project.

    This endpoint is intentionally additive.  It does not expose task rows or
    dataset contents and therefore can be used by the new five-step frontend
    without removing V1.0 routes.
    """

    project = project_for(db, user, project_id)
    return ok({"project_id": project.id, **_project_workflow_status(db, project)})


@app.get("/api/v1/projects/{project_id}/tasks")
def list_tasks(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    project = project_for(db, user, project_id)
    rows = db.scalars(select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/projects/{project_id}/tasks")
def create_task(project_id: str, body: TaskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    _check_assignee(db, project.workspace_id, body.assignee_id)
    task = Task(workspace_id=project.workspace_id, project_id=project.id, title=body.title, description=body.description, priority=body.priority, status=body.status, assignee_id=body.assignee_id, due_at=body.due_at)
    db.add(task)
    db.flush()
    audit(db, project.workspace_id, user.id, "task.created", "task", task.id)
    db.commit()
    return ok(model_dict(task))


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id)
    return ok(model_dict(task, {"links": [model_dict(link) for link in task.links]}))


@app.patch("/api/v1/tasks/{task_id}")
def patch_task(task_id: str, body: TaskPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    _check_assignee(db, task.workspace_id, body.assignee_id)
    for field in ("title", "description", "priority", "status", "assignee_id", "due_at", "ai_summary"):
        value = getattr(body, field)
        if value is not None:
            setattr(task, field, value)
    audit(db, task.workspace_id, user.id, "task.updated", "task", task.id)
    db.commit()
    return ok(model_dict(task))


@app.delete("/api/v1/tasks/{task_id}")
def delete_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    task.status = "archived"
    audit(db, task.workspace_id, user.id, "task.deleted", "task", task.id)
    db.commit()
    return ok({"id": task.id, "status": task.status})


@app.post("/api/v1/tasks/{task_id}/links")
def link_task(task_id: str, body: LinkCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    target_workspace, target_project = _linked_resource_scope(db, body.link_type, body.target_id)
    if target_workspace != task.workspace_id or (target_project is not None and target_project != task.project_id):
        raise error("FORBIDDEN", "Linked object is outside the task project", 403)
    link_type = body.link_type.strip().lower()
    existing = db.scalar(select(TaskLink).where(TaskLink.task_id == task.id, TaskLink.link_type == link_type, TaskLink.target_id == body.target_id))
    if existing is not None:
        raise error("CONFLICT", "This object is already linked to the task", 409)
    link = TaskLink(task_id=task.id, link_type=link_type, target_id=body.target_id, title=body.title)
    db.add(link)
    audit(db, task.workspace_id, user.id, "task.link_created", "task", task.id, {"link_type": link_type, "target_id": body.target_id})
    db.commit()
    return ok(model_dict(link))


@app.get("/api/v1/tasks/{task_id}/links")
def list_task_links(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id)
    rows = db.scalars(select(TaskLink).where(TaskLink.task_id == task.id).order_by(TaskLink.created_at)).all()
    return ok([model_dict(row) for row in rows])


@app.delete("/api/v1/tasks/{task_id}/links/{link_id}")
def unlink_task(task_id: str, link_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    link = db.scalar(select(TaskLink).where(TaskLink.id == link_id, TaskLink.task_id == task_id))
    if link is None:
        raise error("NOT_FOUND", "Task link not found", 404)
    db.delete(link)
    audit(db, task.workspace_id, user.id, "task.link_deleted", "task", task.id, {"link_id": link_id})
    db.commit()
    return ok({"id": link_id, "deleted": True})


@app.get("/api/v1/datasets")
def list_datasets(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(Dataset).where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None)).order_by(Dataset.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Dataset).where(Dataset.workspace_id.in_(workspace_ids), Dataset.deleted_at.is_(None)).order_by(Dataset.created_at.desc())).all() if workspace_ids else []
    payload = [model_dict(row, {"versions": [_version_payload(version, {"columns": None, "cleaning_operations": [model_dict(item) for item in _cleaning_operation_rows(version)]}) for version in row.versions]}) for row in rows]
    return paged(payload, *pagination, len(payload))


@app.post("/api/v1/datasets/upload")
async def upload_dataset(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    dataset_name: str | None = Form(default=None),
    worksheet_name: str | None = Form(default=None),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    filename = _safe_name(file.filename or "upload.csv")
    _reject_unsupported_upload(filename)
    content = await file.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise error("FILE_TOO_LARGE", f"File exceeds {settings.max_upload_size_mb} MB", 413)
    upload_path = settings.data_path / "uploads" / f"{uuid4().hex}_{filename}"
    upload_path.write_bytes(content)
    # Re-uploading under an existing dataset name appends an immutable new
    # version instead of forking a parallel dataset (BUG-015).
    name = dataset_name or Path(filename).stem
    dataset = db.scalar(select(Dataset).where(Dataset.project_id == project.id, Dataset.name == name, Dataset.deleted_at.is_(None)))
    if dataset is None:
        dataset = Dataset(workspace_id=project.workspace_id, project_id=project.id, name=name, source_type="upload", created_by=user.id)
        db.add(dataset)
        db.flush()
        next_version_number = 1
    else:
        membership(db, user, dataset.workspace_id, "editor")
        existing = [v.version_number or 0 for v in dataset.versions]
        next_version_number = (max(existing) if existing else 0) + 1
    relative_path = str(upload_path.relative_to(settings.data_path))
    version = DatasetVersion(dataset_id=dataset.id, version_number=next_version_number, storage_path=relative_path, file_name=filename, file_size_bytes=len(content), row_count=0, column_count=0, schema_json={"columns": []}, status="processing", fingerprint=hashlib.sha256(content).hexdigest())
    db.add(version)
    db.flush()
    job = _job(db, project.workspace_id, "dataset_parse", {"dataset_id": dataset.id, "dataset_version_id": version.id, "file_name": filename, "worksheet_name": worksheet_name, "_storage_path": relative_path, "_actor_id": user.id}, result_type="dataset_version", result_id=version.id)
    audit(db, project.workspace_id, user.id, "dataset.parse_queued", "dataset", dataset.id, {"version_id": version.id, "job_id": job.id})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"dataset": model_dict(dataset), "version": _version_payload(version, {"columns": [], "quality_report": None}), "job": _job_payload(job)})


BATCH_UPLOAD_LIMIT = 10


@app.post("/api/v1/datasets/upload-batch")
async def upload_dataset_batch(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Upload several files at once; each becomes (or versions) its own dataset.

    Semantics match the single-file endpoint exactly: the file stem is the
    dataset name, re-uploading under an existing name appends an immutable
    version, and every file gets its own parse job.  A failure on one file does
    not roll the others back -- the response reports per-file outcomes so the
    client can show what landed and what did not.
    """

    project = project_for(db, user, project_id, "editor")
    if not files:
        raise error("VALIDATION_ERROR", "At least one file is required", 400)
    if len(files) > BATCH_UPLOAD_LIMIT:
        raise error("VALIDATION_ERROR", f"At most {BATCH_UPLOAD_LIMIT} files per batch", 400)

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    accepted: list[tuple[UploadFile, bytes, str, Path]] = []
    failures: list[dict[str, Any]] = []
    for item in files:
        filename = _safe_name(item.filename or "upload.csv")
        try:
            _reject_unsupported_upload(filename)
            content = await item.read()
            if len(content) > max_bytes:
                raise error("FILE_TOO_LARGE", f"{filename} exceeds {settings.max_upload_size_mb} MB", 413)
            if not content:
                raise error("VALIDATION_ERROR", f"{filename} is empty", 400)
            upload_path = settings.data_path / "uploads" / f"{uuid4().hex}_{filename}"
            accepted.append((item, content, filename, upload_path))
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
            failures.append({"file_name": filename, "code": detail.get("code"), "message": detail.get("message")})

    results: list[dict[str, Any]] = []
    for _item, content, filename, upload_path in accepted:
        upload_path.write_bytes(content)
        name = Path(filename).stem
        dataset = db.scalar(select(Dataset).where(Dataset.project_id == project.id, Dataset.name == name, Dataset.deleted_at.is_(None)))
        if dataset is None:
            dataset = Dataset(workspace_id=project.workspace_id, project_id=project.id, name=name, source_type="upload", created_by=user.id)
            db.add(dataset)
            db.flush()
        # Compute the next version number from the database rather than the
        # relationship collection: two same-named files in one batch would
        # otherwise both read the stale collection and collide on
        # (dataset_id, version_number) at commit -- a 500 with the whole batch
        # rolled back.
        next_version_number = (
            db.scalar(select(func.max(DatasetVersion.version_number)).where(DatasetVersion.dataset_id == dataset.id)) or 0
        ) + 1
        relative_path = str(upload_path.relative_to(settings.data_path))
        version = DatasetVersion(dataset_id=dataset.id, version_number=next_version_number, storage_path=relative_path, file_name=filename, file_size_bytes=len(content), row_count=0, column_count=0, schema_json={"columns": []}, status="processing", fingerprint=hashlib.sha256(content).hexdigest())
        db.add(version)
        db.flush()
        job = _job(db, project.workspace_id, "dataset_parse", {"dataset_id": dataset.id, "dataset_version_id": version.id, "file_name": filename, "_storage_path": relative_path, "_actor_id": user.id}, result_type="dataset_version", result_id=version.id)
        # Flush so job.id is assigned before the payload below serializes it;
        # the single-file endpoint reads it after commit instead.
        db.flush()
        results.append({"file_name": filename, "dataset": model_dict(dataset), "version": _version_payload(version, {"columns": [], "quality_report": None}), "job": _job_payload(job)})

    audit(db, project.workspace_id, user.id, "dataset.batch_upload_queued", "project", project.id, {"accepted": len(results), "rejected": len(failures), "file_names": [row["file_name"] for row in results]})
    db.commit()
    for row in results:
        job_executor.schedule(background_tasks, row["job"]["id"])
    return ok({"project_id": project.id, "uploads": results, "failures": failures})


def _dataset_version_for(db: Session, user: User, version_id: str, minimum: str = "viewer") -> tuple[DatasetVersion, Dataset, Project]:
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise error("NOT_FOUND", "Dataset version not found", 404)
    dataset = db.get(Dataset, version.dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    project = project_for(db, user, dataset.project_id, minimum)
    return version, dataset, project


@app.get("/api/v1/datasets/{dataset_id}")
def get_dataset(dataset_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    project_for(db, user, dataset.project_id)
    return ok(model_dict(dataset, {"versions": [_version_payload(version, {"quality_report": model_dict(version.quality_report) if version.quality_report else None, "columns": [model_dict(column) for column in version.columns], "cleaning_operations": [model_dict(item) for item in _cleaning_operation_rows(version)]}) for version in dataset.versions]}))


@app.get("/api/v1/datasets/{dataset_id}/versions")
def list_dataset_versions(dataset_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    project_for(db, user, dataset.project_id)
    versions = sorted(dataset.versions, key=lambda item: item.version_number or 0)
    payload = [_version_payload(version, {"columns": None}) for version in versions]
    return ok(payload, page=1, page_size=len(payload), total=len(payload))


@app.get("/api/v1/dataset-versions/{version_id}")
def get_dataset_version(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, _ = _dataset_version_for(db, user, version_id)
    return ok(_version_payload(version, {"columns": [model_dict(column) for column in version.columns], "quality_report": model_dict(version.quality_report) if version.quality_report else None, "cleaning_operations": [model_dict(item) for item in _cleaning_operation_rows(version)]}))


def _dataset_schema_payload(version: DatasetVersion, dataset: Dataset) -> dict[str, Any]:
    columns = [model_dict(column) for column in version.columns]
    return {
        "dataset_id": dataset.id,
        "dataset_version_id": version.id,
        "status": version.status,
        "columns": columns,
        "available_columns": [str(column["name"]) for column in columns],
    }


@app.get("/api/v1/dataset-versions/{version_id}/schema")
def get_dataset_version_schema(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, dataset, _ = _dataset_version_for(db, user, version_id)
    return ok(_dataset_schema_payload(version, dataset))


@app.get("/api/v1/datasets/{dataset_id}/versions/{version_id}/schema")
def get_dataset_schema(dataset_id: str, version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, dataset, _ = _dataset_version_for(db, user, version_id)
    if dataset.id != dataset_id:
        raise error("NOT_FOUND", "Dataset version does not belong to this dataset", 404)
    return ok(_dataset_schema_payload(version, dataset))


@app.get("/api/v1/dataset-versions/{version_id}/cleaning-operations")
def list_cleaning_operations(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, _ = _dataset_version_for(db, user, version_id)
    rows = [model_dict(item) for item in _cleaning_operation_rows(version)]
    return ok(rows, page=1, page_size=len(rows), total=len(rows))


@app.get("/api/v1/dataset-versions/{version_id}/preview")
def preview_dataset(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    version, _, _ = _dataset_version_for(db, user, version_id)
    path = settings.data_path / version.storage_path
    try:
        frame = _read_dataframe(path, version.file_name)
    except Exception as exc:
        raise error("VALIDATION_ERROR", f"Could not load dataset: {exc}", 400) from exc
    start = (page - 1) * page_size
    return ok({"columns": [model_dict(column) for column in version.columns], "rows": _json_records(frame.iloc[start : start + page_size]), "total": len(frame), "page": page, "page_size": page_size, "dataset_version_id": version.id})


CONFIRMABLE_COLUMN_TYPES = {"string", "integer", "float", "boolean", "datetime", "category"}


@app.patch("/api/v1/dataset-versions/{version_id}/schema")
def patch_schema(version_id: str, body: SchemaPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, project = _dataset_version_for(db, user, version_id, "editor")
    columns_by_name = {column.name: column for column in version.columns}
    for item in body.columns:
        name = item.get("name")
        column_id = item.get("id")
        if column_id:
            column = db.get(DataColumn, column_id)
            if column is not None and column.dataset_version_id != version.id:
                raise error("FORBIDDEN", "Column is outside the selected dataset version", 403)
        else:
            column = columns_by_name.get(name)
        if column is None:
            raise error("VALIDATION_ERROR", f"Column '{name or column_id}' does not exist in this dataset version", 400)
        confirmed = item.get("confirmed_type")
        if confirmed is not None and str(confirmed) not in CONFIRMABLE_COLUMN_TYPES:
            raise error("VALIDATION_ERROR", f"confirmed_type must be one of {sorted(CONFIRMABLE_COLUMN_TYPES)}", 400)
        for field in ("display_name", "confirmed_type", "mapping_role", "nullable"):
            if field in item:
                setattr(column, field, item[field])
    version.schema_json = {"columns": [model_dict(column) for column in version.columns]}
    # Editing the schema implies the user looked at the inferred roles, which is
    # what stage 2 actually gates on.
    if version.schema_reviewed_at is None:
        version.schema_reviewed_at = datetime.now(UTC).replace(tzinfo=None)
    audit(db, project.workspace_id, user.id, "dataset.schema_confirmed", "dataset_version", version.id)
    db.commit()
    return ok(_version_payload(version, {"columns": [model_dict(column) for column in version.columns]}))


@app.post("/api/v1/dataset-versions/{version_id}/schema-review")
def mark_schema_reviewed(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Record that a user reviewed the inferred field roles without changing them.

    This is the "I looked, nothing to map" path.  A cleaned business table often
    has no event columns, so requiring a role edit to advance would deadlock the
    pipeline.  Idempotent: reviewing twice keeps the first timestamp.
    """

    version, _, project = _dataset_version_for(db, user, version_id, "editor")
    if version.schema_reviewed_at is None:
        version.schema_reviewed_at = datetime.now(UTC).replace(tzinfo=None)
        audit(db, project.workspace_id, user.id, "dataset.schema_reviewed", "dataset_version", version.id)
    db.commit()
    return ok(_version_payload(version, {"columns": [model_dict(column) for column in version.columns]}))


@app.get("/api/v1/dataset-versions/{version_id}/quality-report")
def quality_report(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, _ = _dataset_version_for(db, user, version_id)
    if version.quality_report is None:
        raise error("NOT_FOUND", "Quality report not found", 404)
    return ok(model_dict(version.quality_report))


@app.post("/api/v1/dataset-versions/{version_id}/cleaning-preview")
def cleaning_preview(version_id: str, body: CleaningRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, _ = _dataset_version_for(db, user, version_id, "editor")
    path = settings.data_path / version.storage_path
    try:
        dataframe = _read_dataframe(path, version.file_name)
    except Exception as exc:
        raise error("VALIDATION_ERROR", f"Could not load dataset: {exc}", 400) from exc
    _, summary = _apply_cleaning(dataframe, body.operations)
    return ok({"dataset_version_id": version.id, **summary})


@app.post("/api/v1/dataset-versions/{version_id}/cleaning-operations")
def cleaning_operations(version_id: str, body: CleaningRequest, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, dataset, project = _dataset_version_for(db, user, version_id, "editor")
    if version.status not in {"ready", "confirmed"}:
        raise error("DATASET_NOT_READY", "Source dataset version is not ready for cleaning", 422)
    normalised_operations = _normalise_cleaning_operations(body.operations)
    if not normalised_operations:
        raise error("VALIDATION_ERROR", "At least one cleaning operation is required", 400)
    new_version_number = max((item.version_number for item in dataset.versions), default=0) + 1
    cleaned_name = f"{Path(version.file_name).stem}_v{new_version_number}.csv"
    cleaned_path = settings.data_path / "processed" / f"{uuid4().hex}_{cleaned_name}"
    relative_path = str(cleaned_path.relative_to(settings.data_path))
    new_version = DatasetVersion(dataset_id=dataset.id, version_number=new_version_number, parent_version_id=version.id, storage_path=relative_path, file_name=cleaned_name, file_size_bytes=0, row_count=0, column_count=0, schema_json={"columns": []}, status="processing", fingerprint=None)
    db.add(new_version)
    db.flush()
    operation_rows: list[CleaningOperation] = []
    for operation in normalised_operations:
        operation_rows.append(CleaningOperation(
            source_version_id=version.id,
            result_version_id=new_version.id,
            operation_type=str(operation.get("operation") or "")[:50],
            parameters_json=_cleaning_operation_parameters(operation),
            preview_json={"status": "queued", "source_version_id": version.id, "result_version_id": new_version.id},
            approved_by=user.id,
        ))
    db.add_all(operation_rows)
    db.flush()
    job = _job(db, project.workspace_id, "dataset_cleaning", {"source_version_id": version.id, "target_version_id": new_version.id, "operations": normalised_operations, "_cleaning_operation_ids": [item.id for item in operation_rows], "_target_storage_path": relative_path, "_actor_id": user.id}, result_type="dataset_version", result_id=new_version.id)
    audit(db, project.workspace_id, user.id, "dataset.cleaning_queued", "dataset_version", new_version.id, {"parent_version_id": version.id, "operations": normalised_operations, "cleaning_operation_ids": [item.id for item in operation_rows], "job_id": job.id})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"dataset_version": _version_payload(new_version, {"columns": [], "quality_report": None, "cleaning_operations": [model_dict(item) for item in operation_rows]}), "preview": {"status": "queued"}, "job": _job_payload(job)})


@app.delete("/api/v1/datasets/{dataset_id}")
def delete_dataset(
    dataset_id: str,
    body: DatasetDeleteRequest | None = Body(default=None),
    confirm: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    project_for(db, user, dataset.project_id, "owner")

    confirmation = body.confirm if body is not None else confirm
    confirmed = confirmation is True
    if isinstance(confirmation, str):
        normalized = confirmation.strip()
        confirmed = normalized == dataset.id or normalized.lower() in {"true", "1", "yes"}
    if not confirmed:
        raise error(
            "CONFIRMATION_REQUIRED",
            "Dataset deletion requires confirm=true or confirm=<dataset_id>",
            409,
            {"dataset_id": dataset_id},
        )
    dataset.deleted_at = now()
    audit(db, dataset.workspace_id, user.id, "dataset.deleted", "dataset", dataset.id, {"confirmed": True})
    db.commit()
    return ok({"id": dataset.id, "deleted": True})


def _analysis_artifacts(frame: pd.DataFrame, version: DatasetVersion, analysis_type: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    _require_pandas()
    artifacts: list[dict[str, Any]] = []
    numeric_columns = [str(c) for c in frame.select_dtypes(include="number").columns]
    kind = str(analysis_type or "").strip().lower()
    engine = AnalysisEngine(dataset_version_id=version.id)
    field_mapping = dict(config.get("field_mapping") or {}) if isinstance(config.get("field_mapping"), dict) else {}

    # The user-facing API and Copilot tool registry share the same deterministic
    # implementation so an identical version/config produces the same evidence.
    engine_artifact = None
    if kind in {"trend", "time_series"}:
        engine_artifact = engine.run_trend_analysis(
            frame,
            time_column=str(config.get("time_column") or config.get("event_time_column") or config.get("date_column") or "event_time"),
            metric_column=str(config.get("metric_column")),
            group_column=str(config["group_column"]) if config.get("group_column") else None,
            frequency=str(config.get("frequency") or "D"),
            aggregation=str(config.get("aggregation") or "mean"),
            field_mapping=field_mapping,
        )
    elif kind in {"funnel", "conversion"}:
        engine_artifact = engine.run_funnel_analysis(
            frame,
            user_id_column=str(config.get("user_id_column") or "user_id"),
            event_time_column=str(config.get("event_time_column") or "event_time"),
            event_name_column=str(config.get("event_name_column") or "event_name"),
            steps=[str(step) for step in config.get("steps") or []],
            window_hours=float(config.get("window_hours") or config.get("time_window_hours")) if config.get("window_hours") is not None or config.get("time_window_hours") is not None else None,
            field_mapping=field_mapping,
        )
    elif kind in {"retention", "retention_analysis"}:
        engine_artifact = engine.run_retention_analysis(
            frame,
            user_id_column=str(config.get("user_id_column") or "user_id"),
            event_time_column=str(config.get("event_time_column") or "event_time"),
            periods=[int(period) for period in config.get("periods") or [1, 7, 30]],
            cohort_granularity=str(config.get("cohort_granularity") or "day"),
            return_event_filter=dict(config.get("return_event_filter") or {}),
            field_mapping=field_mapping,
        )
    elif kind in {"anomaly", "anomalies"}:
        method = str(config.get("method") or "iqr").lower().replace("z_score", "zscore")
        engine_artifact = engine.run_anomaly_detection(
            frame,
            metric_column=str(config.get("metric_column")),
            time_column=str(config["time_column"]) if config.get("time_column") else None,
            method=method,
            threshold=float(config.get("threshold") or config.get("z_threshold") or 3),
            window=int(config.get("window") or 7),
            group_column=str(config["group_column"]) if config.get("group_column") else None,
        )
    if engine_artifact is not None:
        result = engine_artifact.to_dict()
        payload = dict(result["payload_json"])
        option: dict[str, Any] | None = None
        chart_type: str | None = None
        if kind in {"trend", "time_series"}:
            chart = dict(payload.get("chart") or {})
            option = {
                "tooltip": {"trigger": "axis"},
                "legend": {"type": "scroll", "bottom": 0},
                "grid": {"left": 48, "right": 20, "top": 24, "bottom": 52},
                "xAxis": chart.get("xAxis") or {"type": "time"},
                "yAxis": chart.get("yAxis") or {"type": "value"},
                "series": chart.get("series") or [],
            }
            chart_type = "line"
        elif kind in {"funnel", "conversion"}:
            chart = dict(payload.get("chart") or {})
            option = {
                "tooltip": {"trigger": "item", "formatter": "{b}: {c}"},
                "series": [{"type": "funnel", "left": "10%", "width": "80%", "label": {"show": True, "position": "inside"}, "data": chart.get("data") or []}],
            }
            chart_type = "funnel"
        elif kind in {"retention", "retention_analysis"}:
            rows = list(payload.get("cohort_results") or [])
            cohorts = sorted({str(row.get("cohort")) for row in rows})
            periods = sorted({int(row.get("period") or 0) for row in rows})
            values = [[periods.index(int(row.get("period") or 0)), cohorts.index(str(row.get("cohort"))), row.get("retention_rate")] for row in rows]
            option = {
                "tooltip": {"position": "top"},
                "grid": {"left": 92, "right": 28, "top": 20, "bottom": 52},
                "xAxis": {"type": "category", "data": [f"D{period}" for period in periods], "splitArea": {"show": True}},
                "yAxis": {"type": "category", "data": cohorts, "splitArea": {"show": True}},
                "visualMap": {"min": 0, "max": 1, "calculable": True, "orient": "horizontal", "left": "center", "bottom": 0},
                "series": [{"name": "Retention", "type": "heatmap", "data": values, "label": {"show": True, "formatter": "{@[2]}"}}],
            }
            chart_type = "heatmap"
        elif kind in {"anomaly", "anomalies"}:
            rows = list(payload.get("rows") or [])
            x_values = [row.get("timestamp") or row.get("index") for row in rows]
            option = {
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 48, "right": 20, "top": 24, "bottom": 44},
                "xAxis": {"type": "category", "data": x_values},
                "yAxis": {"type": "value"},
                "series": [
                    {"name": str(payload.get("metric_column") or "value"), "type": "line", "data": [row.get("value") for row in rows], "showSymbol": False},
                    {"name": "anomaly", "type": "scatter", "symbolSize": 10, "data": [[index, row.get("value")] for index, row in enumerate(rows) if row.get("is_anomaly")]},
                ],
            }
            chart_type = "line"
        payload.update({"datasetVersionId": version.id, "configSnapshot": result["config_snapshot"], "chartType": chart_type, "title": result["title"], "option": option})
        return [{"artifact_type": result["artifact_type"], "title": result["title"], "payload_json": payload}]

    if analysis_type in {"eda", "descriptive", "overview"}:
        describe = frame.describe(include="all").replace({float("nan"): None})
        payload = {str(k): serialize(v) for k, v in describe.to_dict().items()}
        artifacts.append({"artifact_type": "table", "title": "Descriptive statistics", "payload_json": {"columns": list(frame.columns), "rows": _json_records(describe.reset_index())}})
        artifacts.append({"artifact_type": "metric", "title": "Dataset summary", "payload_json": {"row_count": len(frame), "column_count": len(frame.columns), "numeric_columns": numeric_columns, "missing_cells": int(frame.isna().sum().sum()), "stats": payload}})
    elif analysis_type in {"group", "grouped", "segmentation", "group_analysis"}:
        group_column = config.get("group_column") or config.get("segment_column")
        metric_column = config.get("metric_column")
        if not group_column or group_column not in frame.columns:
            raise error("VALIDATION_ERROR", "Grouped analysis requires a group_column", 400)
        if metric_column and metric_column not in frame.columns:
            raise error("VALIDATION_ERROR", f"Missing metric column: {metric_column}", 400)
        aggregation = str(config.get("aggregation") or "count").lower()
        working = frame[[group_column] + ([metric_column] if metric_column else [])].copy()
        if metric_column:
            working[metric_column] = pd.to_numeric(working[metric_column], errors="coerce")
            grouped = working.groupby(group_column, dropna=False)[metric_column].agg(aggregation if aggregation in {"sum", "mean", "median", "min", "max"} else "mean").reset_index(name="value")
        else:
            grouped = working.groupby(group_column, dropna=False).size().reset_index(name="value")
        grouped[group_column] = grouped[group_column].astype(str)
        grouped = grouped.sort_values("value", ascending=False).head(int(config.get("top_n") or 20))
        artifacts.append({"artifact_type": "chart", "title": f"Grouped analysis by {group_column}", "payload_json": {"chartType": "bar", "title": f"Grouped analysis by {group_column}", "datasetVersionId": version.id, "option": {"xAxis": {"type": "category", "data": grouped[group_column].tolist()}, "yAxis": {"type": "value"}, "series": [{"name": metric_column or "count", "type": "bar", "data": [serialize(value) for value in grouped["value"].tolist()]}]}}})
        artifacts.append({"artifact_type": "table", "title": "Grouped values", "payload_json": {"rows": _json_records(grouped)}})
    elif analysis_type in {"health", "health_score"}:
        metrics: list[dict[str, Any]] = []
        for column in numeric_columns:
            values = pd.to_numeric(frame[column], errors="coerce").dropna()
            metrics.append({"metric": column, "value": float(values.mean()) if not values.empty else None, "sample_count": int(values.size)})
        artifacts.append({"artifact_type": "metric", "title": "AI product health overview", "payload_json": {"chartType": "bar", "title": "AI product health overview", "datasetVersionId": version.id, "configSnapshot": config, "metrics": metrics, "definition": "Deterministic summary of numeric fields; metric semantics come from the workspace dictionary.", "option": {"tooltip": {"trigger": "axis"}, "grid": {"left": 48, "right": 20, "top": 24, "bottom": 64}, "xAxis": {"type": "category", "axisLabel": {"rotate": 28}, "data": [item["metric"] for item in metrics]}, "yAxis": {"type": "value"}, "series": [{"name": "mean", "type": "bar", "data": [item["value"] for item in metrics]}]}}})
    else:
        artifacts.append({"artifact_type": "table", "title": "Sample data", "payload_json": {"rows": _json_records(frame.head(100))}})
    return artifacts


def _analysis_result_summary(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"artifact_count": len(artifacts)}
    for artifact in artifacts:
        payload = artifact.get("payload_json") or {}
        retention_rows = payload.get("cohort_results") or []
        if retention_rows:
            d7_rows = [row for row in retention_rows if int(row.get("period") or -1) == 7]
            if d7_rows:
                summary["d7_retention"] = sum(float(row.get("retention_rate") or 0) for row in d7_rows) / len(d7_rows)
            summary["max_cohort_size"] = max((int(row.get("cohort_size") or 0) for row in retention_rows), default=0)
        funnel_rows = payload.get("step_results") or []
        if funnel_rows:
            summary["max_dropoff_users"] = max((int(row.get("dropoff_users") or 0) for row in funnel_rows), default=0)
        if "anomaly_count" in payload:
            summary["anomaly_count"] = int(payload.get("anomaly_count") or 0)
    return summary


def _job_storage_path(relative_path: str) -> Path:
    """Resolve an internal job path without allowing path traversal."""

    root = settings.data_path.resolve()
    candidate = (root / str(relative_path)).resolve()
    if root != candidate and root not in candidate.parents:
        raise JobExecutionError("INVALID_STORAGE_PATH", "Job storage path is outside the data root", retryable=False)
    return candidate


def _replace_version_columns(db: Session, version: DatasetVersion, schema: list[dict[str, Any]]) -> None:
    for column in list(version.columns):
        db.delete(column)
    db.flush()
    for item in schema:
        db.add(DataColumn(dataset_version_id=version.id, **item))


def _replace_quality_report(db: Session, version: DatasetVersion, score: float, quality_status: str, summary: dict[str, Any]) -> None:
    if version.quality_report is not None:
        db.delete(version.quality_report)
        db.flush()
    db.add(DataQualityReport(dataset_version_id=version.id, overall_score=score, status=quality_status, summary_json=summary))


def _update_cleaning_operation_rows(
    db: Session,
    payload: dict[str, Any],
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    source_row_count: int | None = None,
    result_row_count: int | None = None,
    result_fingerprint: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    operation_ids = [str(value) for value in (payload.get("_cleaning_operation_ids") or []) if value]
    if not operation_ids:
        return
    rows = db.scalars(select(CleaningOperation).where(CleaningOperation.id.in_(operation_ids))).all()
    by_id = {row.id: row for row in rows}
    for index, operation_id in enumerate(operation_ids):
        row = by_id.get(operation_id)
        if row is None:
            continue
        preview = dict(row.preview_json or {})
        preview.update({"status": status, "operation_index": index})
        if summary is not None:
            preview.update(summary)
        if source_row_count is not None:
            preview["source_row_count"] = source_row_count
        if result_row_count is not None:
            preview["result_row_count"] = result_row_count
        if result_fingerprint:
            preview["result_fingerprint"] = result_fingerprint
        if error_code:
            preview["error_code"] = error_code
        if error_message:
            preview["error_message"] = error_message[:4000]
        preview["updated_at"] = serialize(now())
        row.preview_json = preview


def _handle_dataset_parse(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    version = db.get(DatasetVersion, payload.get("dataset_version_id"))
    if version is None:
        raise JobExecutionError("NOT_FOUND", "Dataset version not found", retryable=False)
    file_name = str(payload.get("file_name") or version.file_name)
    storage_path = str(payload.get("_storage_path") or version.storage_path)
    context.progress(10, "读取上传文件")
    frame = _read_dataframe(_job_storage_path(storage_path), file_name, payload.get("worksheet_name"))
    if len(frame) > settings.max_rows_per_dataset or len(frame.columns) > settings.max_columns_per_dataset:
        raise JobExecutionError("VALIDATION_ERROR", f"Dataset exceeds {settings.max_rows_per_dataset} rows or {settings.max_columns_per_dataset} columns", retryable=False)
    if len(frame.columns) == 0 or len(frame) == 0:
        raise JobExecutionError("VALIDATION_ERROR", "Dataset must contain a header row and at least one data row", retryable=False)
    context.progress(45, "检查数据质量")
    score, quality_status, summary = _quality_summary(frame)
    schema = _column_schema(frame)
    context.progress(75, "写入字段字典")
    version.row_count = len(frame)
    version.column_count = len(frame.columns)
    version.schema_json = {"columns": schema}
    version.status = "ready"
    _replace_version_columns(db, version, schema)
    _replace_quality_report(db, version, score, quality_status, summary)
    audit(db, version.dataset.project.workspace_id, payload.get("_actor_id"), "dataset.parsed", "dataset_version", version.id, {"quality_status": quality_status, "row_count": len(frame)})

    context.progress(85, "自动分析")
    auto = _run_auto_analyses(db, version, schema, frame, str(payload.get("_actor_id") or ""))

    db.commit()
    return JobResult(
        result_type="dataset_version",
        result_id=version.id,
        input_updates={
            "rows": len(frame),
            "columns": len(frame.columns),
            "auto_analysis_run_ids": auto["run_ids"],
            "auto_analysis_plan": auto["plan"],
        },
    )


def _run_auto_analyses(
    db: Session,
    version: DatasetVersion,
    schema: list[dict[str, Any]],
    frame: pd.DataFrame,
    actor_id: str,
) -> dict[str, Any]:
    """Accept the inferred schema and run the auto-selected analyses inline.

    Called from the parse job with the frame already in memory, so nothing is
    re-read from disk.  Idempotent on ``schema_auto_accepted_at``: ``recover_pending``
    (app/infrastructure/jobs.py:134) re-runs the whole composite after a restart,
    and a second pass must not stack duplicate runs onto the same version.

    A failure here never fails the upload.  The parse result -- schema, columns,
    quality report -- is already committed-worthy at this point, and losing it
    because an optional convenience analysis raised would be a bad trade.
    """

    if version.schema_auto_accepted_at is not None:
        return {"run_ids": [], "plan": [], "skipped": [{"reason": "already_auto_accepted"}]}
    if not actor_id:
        # ``AnalysisRun.requested_by`` is NOT NULL and FK-bound to users.id
        # (app/models.py:287); without a real actor there is no run to create.
        return {"run_ids": [], "plan": [], "skipped": [{"reason": "no_actor"}]}

    project = version.dataset.project
    stamp = now()
    version.schema_auto_accepted_at = stamp

    plan = _auto_analysis_plan(schema)
    run_ids: list[str] = []
    skipped: list[dict[str, Any]] = []
    for entry in plan:
        kind = str(entry["analysis_type"])
        try:
            run, rejection = _prepare_analysis_run(
                db,
                project=project,
                version=version,
                analysis_type=kind,
                config=dict(entry["config"]),
                actor_id=actor_id,
            )
            if run is None:
                skipped.append({"analysis_type": kind, "reason": rejection.get("reason")})
                continue
            run.config_json = {
                **(run.config_json or {}),
                "auto_selected": True,
                "auto_reason": entry["reason"],
                "auto_columns": entry["columns"],
            }
            # Executed inline rather than via a queued ``analysis_run`` job: the
            # frame is already in memory here, and a separate job would re-read
            # the file and leave the run ``queued`` until it drained.
            run.status = "running"
            run.started_at = stamp
            artifacts = _analysis_artifacts(frame, version, kind, dict(run.config_json))
            for artifact in artifacts:
                db.add(
                    AnalysisArtifact(
                        analysis_run_id=run.id,
                        artifact_type=artifact["artifact_type"],
                        title=artifact["title"],
                        payload_json=artifact.get("payload_json") or {},
                        fingerprint=hashlib.sha256(
                            json.dumps(artifact.get("payload_json") or {}, ensure_ascii=False, sort_keys=True, default=str).encode()
                        ).hexdigest(),
                    )
                )
            run.status = "succeeded"
            run.completed_at = now()
            run.result_summary = _analysis_result_summary(artifacts)
            db.flush()
            run_ids.append(run.id)
        except Exception as exc:  # noqa: BLE001 - an optional analysis must not sink a good parse
            skipped.append({"analysis_type": kind, "reason": "error", "detail": type(exc).__name__})
            continue

    audit(
        db,
        project.workspace_id,
        actor_id or None,
        "dataset.schema_auto_accepted",
        "dataset_version",
        version.id,
        {
            "analysis_run_ids": run_ids,
            "plan": [{"type": e["analysis_type"], "reason": e["reason"]} for e in plan],
            "skipped": skipped,
        },
    )
    return {"run_ids": run_ids, "plan": plan, "skipped": skipped}


def _handle_dataset_cleaning(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    source = db.get(DatasetVersion, payload.get("source_version_id"))
    target = db.get(DatasetVersion, payload.get("target_version_id"))
    if source is None or target is None:
        raise JobExecutionError("NOT_FOUND", "Cleaning source or target version not found", retryable=False)
    context.progress(10, "读取原始版本")
    frame = _read_dataframe(_job_storage_path(source.storage_path), source.file_name)
    context.progress(35, "应用清洗规则")
    cleaned, summary = _apply_cleaning(frame, list(payload.get("operations") or []))
    if cleaned.empty:
        raise JobExecutionError("VALIDATION_ERROR", "Cleaning operations would remove every row; adjust the rules and retry", retryable=False)
    target_path = _job_storage_path(str(payload.get("_target_storage_path") or target.storage_path))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.to_csv(target_path, index=False)
    context.progress(70, "生成清洗后版本")
    schema = _column_schema(cleaned)
    target.row_count = len(cleaned)
    target.column_count = len(cleaned.columns)
    target.file_size_bytes = target_path.stat().st_size
    target.fingerprint = hashlib.sha256(target_path.read_bytes()).hexdigest()
    target.schema_json = {"columns": schema, "parent_version_id": source.id, "cleaning_operations": payload.get("operations") or []}
    target.status = "ready"
    _replace_version_columns(db, target, schema)
    score, quality_status, quality = _quality_summary(cleaned)
    _replace_quality_report(db, target, score, quality_status, quality)
    _update_cleaning_operation_rows(
        db,
        payload,
        status="succeeded",
        summary=summary,
        source_row_count=len(frame),
        result_row_count=len(cleaned),
        result_fingerprint=target.fingerprint,
    )
    audit(db, target.dataset.project.workspace_id, payload.get("_actor_id"), "dataset.cleaning_applied", "dataset_version", target.id, {"source_version_id": source.id, "operations": payload.get("operations") or [], "summary": summary})
    db.commit()
    return JobResult(result_type="dataset_version", result_id=target.id, input_updates={"rows": len(cleaned), "cleaning_summary": summary})


def _handle_analysis(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    run = db.get(AnalysisRun, payload.get("analysis_run_id"))
    version = db.get(DatasetVersion, payload.get("dataset_version_id"))
    if run is None or version is None:
        raise JobExecutionError("NOT_FOUND", "Analysis run or dataset version not found", retryable=False)
    run.status = "running"
    run.started_at = run.started_at or now()
    run.error_code = None
    db.commit()
    context.progress(12, "读取分析数据")
    frame = _read_dataframe(_job_storage_path(version.storage_path), version.file_name)
    context.progress(45, "执行确定性计算")
    analysis_config = dict(payload.get("config") or run.config_json or {})
    if isinstance(payload.get("field_mapping"), dict) and payload.get("field_mapping"):
        analysis_config["field_mapping"] = dict(payload["field_mapping"])
    artifacts = _analysis_artifacts(frame, version, str(payload.get("analysis_type") or run.analysis_type), analysis_config)
    for item in list(run.artifacts):
        db.delete(item)
    db.flush()
    for artifact in artifacts:
        db.add(AnalysisArtifact(analysis_run_id=run.id, artifact_type=artifact["artifact_type"], title=artifact["title"], payload_json=artifact.get("payload_json") or {}, fingerprint=hashlib.sha256(json.dumps(artifact.get("payload_json") or {}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()))
    run.status = "succeeded"
    run.completed_at = now()
    run.result_summary = _analysis_result_summary(artifacts)
    context.progress(85, "保存分析产物")
    audit(db, run.workspace_id, payload.get("_actor_id"), "analysis.completed", "analysis_run", run.id, {"artifact_count": len(artifacts)})
    db.commit()
    return JobResult(result_type="analysis_run", result_id=run.id, input_updates={"artifact_count": len(artifacts)})


def _feedback_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    names = {str(column).strip().lower(): str(column) for column in frame.columns}
    for candidate in candidates:
        if candidate in names:
            return names[candidate]
    return None


def _handle_feedback_import(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    project = db.get(Project, payload.get("project_id"))
    if project is None:
        raise JobExecutionError("NOT_FOUND", "Feedback project not found", retryable=False)
    frame = _read_dataframe(_job_storage_path(str(payload.get("_storage_path"))), str(payload.get("file_name") or "feedback.csv"))
    content_column = _feedback_column(frame, ("content", "feedback", "feedback_text", "text", "comment", "message", "review"))
    if content_column is None:
        content_column = next((str(column) for column in frame.columns if frame[column].dtype == "object"), None)
    if content_column is None:
        raise JobExecutionError("VALIDATION_ERROR", "Feedback file must contain a text column", retryable=False)
    user_column = _feedback_column(frame, ("user_id", "user_ref", "account_id", "customer_id"))
    channel_column = _feedback_column(frame, ("channel", "source", "platform"))
    rating_column = _feedback_column(frame, ("rating", "score", "stars"))
    date_column = _feedback_column(frame, ("feedback_at", "created_at", "date", "timestamp"))
    external_column = _feedback_column(frame, ("external_ref", "id", "ticket_id"))
    labels_column = _feedback_column(frame, ("labels", "label", "tags", "tag"))
    total = len(frame)
    created = 0
    for index, row in frame.iterrows():
        content = str(row.get(content_column) if row.get(content_column) is not None else "").strip()
        if not content or content.lower() == "nan":
            continue
        rating = None
        if rating_column:
            try:
                rating = float(row.get(rating_column))
            except (TypeError, ValueError):
                rating = None
        feedback_at = None
        if date_column:
            parsed = pd.to_datetime(row.get(date_column), errors="coerce")
            if pd.notna(parsed):
                feedback_at = parsed.to_pydatetime().replace(tzinfo=None)
        labels: list[str] = []
        if labels_column and row.get(labels_column) is not None:
            labels = [item.strip() for item in re.split(r"[,;|]", str(row.get(labels_column))) if item.strip() and item.lower() != "nan"]
        channel = str(row.get(channel_column))[:80] if channel_column and row.get(channel_column) is not None else "import"
        label = labels[0][:120] if labels else ""
        db.add(FeedbackItem(workspace_id=project.workspace_id, project_id=project.id, content=content, external_ref=str(row.get(external_column)) if external_column and row.get(external_column) is not None else None, user_ref=str(row.get(user_column)) if user_column and row.get(user_column) is not None else None, channel=channel, rating=rating, feedback_at=feedback_at, labels_json=labels, status="unreviewed"))
        # V1.1 keeps a single durable note shape.  The legacy row above is
        # retained temporarily so existing clients can finish their migration.
        db.add(FeedbackNote(project_id=project.id, content=content, source=channel, label=label, sentiment="unknown"))
        created += 1
        if created % 250 == 0:
            context.progress(min(90, 10 + int(index / max(1, total) * 80)), f"写入反馈 {created}/{total}")
    context.progress(95, "保存反馈导入结果")
    audit(db, project.workspace_id, payload.get("_actor_id"), "feedback.imported", "feedback_item", None, {"count": created, "job_id": context.job_id})
    db.commit()
    return JobResult(result_type="feedback_items", input_updates={"rows": created})


def _feedback_theme_key(content: str) -> str:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,8}|[a-zA-Z]{3,}", content.lower())
    return tokens[0] if tokens else "其他反馈"


def _handle_feedback_clusters(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    project = db.get(Project, payload.get("project_id"))
    if project is None:
        raise JobExecutionError("NOT_FOUND", "Feedback project not found", retryable=False)
    prior_ids = [str(value) for value in (payload.get("_created_cluster_ids") or []) if value]
    for cluster_id in prior_ids:
        cluster = db.get(FeedbackCluster, cluster_id)
        if cluster is not None and cluster.project_id == project.id and cluster.status == "draft":
            db.delete(cluster)
    db.flush()
    payload["_created_cluster_ids"] = []
    context.job.input_json = {**payload}
    db.commit()
    items = db.scalars(select(FeedbackItem).where(FeedbackItem.project_id == project.id).order_by(FeedbackItem.created_at)).all()
    groups: dict[str, list[FeedbackItem]] = {}
    for item in items:
        key = (item.labels_json or [None])[0] if item.labels_json else None
        key = str(key).strip() if key else _feedback_theme_key(item.content)
        groups.setdefault(key or "其他反馈", []).append(item)
    created_ids: list[str] = []
    for index, (key, grouped) in enumerate(sorted(groups.items(), key=lambda entry: (-len(entry[1]), entry[0]))[:12]):
        cluster = FeedbackCluster(workspace_id=project.workspace_id, project_id=project.id, name=key.title(), summary=f"共 {len(grouped)} 条反馈集中提及“{key}”，请结合样本进行人工确认。", sentiment="negative" if sum(1 for item in grouped if item.rating is not None and item.rating <= 2) > len(grouped) / 2 else "neutral", sample_count=len(grouped), evidence_json=[{"type": "feedback_item", "id": item.id} for item in grouped[:20]], status="draft")
        db.add(cluster)
        db.flush()
        created_ids.append(cluster.id)
        for item in grouped:
            db.add(FeedbackClusterItem(cluster_id=cluster.id, feedback_item_id=item.id, score=1.0))
            # Keep normalized notes aligned with the deterministic grouping.
            notes = db.scalars(select(FeedbackNote).where(FeedbackNote.project_id == project.id, FeedbackNote.content == item.content)).all()
            for note in notes:
                note.cluster_name = cluster.name[:255]
        context.progress(min(95, 20 + int((index + 1) / max(1, len(groups)) * 70)), f"归纳反馈主题 {index + 1}/{len(groups)}")
        payload["_created_cluster_ids"] = created_ids
        context.job.input_json = {**payload}
        db.commit()
    audit(db, project.workspace_id, payload.get("_actor_id"), "feedback.clusters_generated", "feedback_cluster", None, {"count": len(created_ids), "job_id": context.job_id})
    db.commit()
    return JobResult(result_type="feedback_clusters", input_updates={"cluster_ids": created_ids})


def _cleanup_feedback_cluster_attempt(db: Session, job: Job) -> None:
    payload = job.input_json if isinstance(job.input_json, dict) else {}
    project_id = payload.get("project_id")
    for cluster_id in payload.get("_created_cluster_ids") or []:
        cluster = db.get(FeedbackCluster, str(cluster_id))
        if cluster is not None and (not project_id or cluster.project_id == project_id) and cluster.status == "draft":
            db.delete(cluster)
    db.flush()


def _mark_feedback_clusters_failed(db: Session, job: Job, code: str, message: str) -> None:
    _cleanup_feedback_cluster_attempt(db, job)


def _mark_feedback_clusters_cancelled(db: Session, job: Job) -> None:
    _cleanup_feedback_cluster_attempt(db, job)


def _handle_document_generation(context: JobContext) -> JobResult:
    document = context.db.get(Document, context.input.get("document_id"))
    if document is None:
        raise JobExecutionError("NOT_FOUND", "Document not found", retryable=False)
    context.progress(60, "确认文档草稿")
    # The current endpoint creates a deterministic evidence-backed version before
    # queueing. Keeping the handler idempotent makes retries safe and preserves it.
    audit(context.db, document.workspace_id, context.input.get("_actor_id"), "document.generation_completed", "document", document.id, {"job_id": context.job_id})
    context.db.commit()
    return JobResult(result_type="document", result_id=document.id)


def _mark_dataset_parse_failed(db: Session, job: Job, code: str, message: str) -> None:
    version_id = (job.input_json or {}).get("dataset_version_id")
    version = db.get(DatasetVersion, version_id) if version_id else None
    if version is not None:
        version.status = "failed"


def _mark_cleaning_failed(db: Session, job: Job, code: str, message: str) -> None:
    payload = job.input_json if isinstance(job.input_json, dict) else {}
    target_id = payload.get("target_version_id")
    target = db.get(DatasetVersion, target_id) if target_id else None
    if target is not None:
        target.status = "failed"
    _update_cleaning_operation_rows(db, payload, status="failed", error_code=code, error_message=message)


def _mark_cleaning_cancelled(db: Session, job: Job) -> None:
    payload = job.input_json if isinstance(job.input_json, dict) else {}
    target_id = payload.get("target_version_id")
    target = db.get(DatasetVersion, target_id) if target_id else None
    if target is not None:
        target.status = "cancelled"
    _update_cleaning_operation_rows(db, payload, status="cancelled")


def _mark_analysis_failed(db: Session, job: Job, code: str, message: str) -> None:
    run_id = (job.input_json or {}).get("analysis_run_id")
    run = db.get(AnalysisRun, run_id) if run_id else None
    if run is not None:
        run.status = "failed"
        run.error_code = code
        run.completed_at = now()


def _mark_document_failed(db: Session, job: Job, code: str, message: str) -> None:
    document_id = (job.input_json or {}).get("document_id")
    document = db.get(Document, document_id) if document_id else None
    if document is not None:
        document.status = "generation_failed"


def _register_job_handlers() -> None:
    registrations = {
        "dataset_parse": (_handle_dataset_parse, _mark_dataset_parse_failed, None),
        "dataset_cleaning": (_handle_dataset_cleaning, _mark_cleaning_failed, _mark_cleaning_cancelled),
        "analysis_run": (_handle_analysis, _mark_analysis_failed, None),
        "feedback_import": (_handle_feedback_import, None, None),
        "feedback_cluster_generation": (_handle_feedback_clusters, _mark_feedback_clusters_failed, _mark_feedback_clusters_cancelled),
        "document_generation": (_handle_document_generation, _mark_document_failed, None),
    }
    for job_type, (handler, on_failure, on_cancel) in registrations.items():
        if not job_executor.has_handler(job_type):
            job_executor.register(job_type, handler, on_failure=on_failure, on_cancel=on_cancel)


SUPPORTED_ANALYSIS_TYPES = {
    "eda", "descriptive", "overview", "trend", "time_series", "group", "grouped",
    "segmentation", "group_analysis", "funnel", "conversion", "retention",
    "retention_analysis", "anomaly", "anomalies", "health", "health_score",
}


FIELD_MAPPING_ALIASES = {
    "user_id": "user_id",
    "user_id_column": "user_id",
    "user": "user_id",
    "event_time": "event_time",
    "event_time_column": "event_time",
    "time": "event_time",
    "time_column": "event_time",
    "event_name": "event_name",
    "event_name_column": "event_name",
    "event": "event_name",
    "session_id": "session_id",
    "session_id_column": "session_id",
}


def _analysis_request_config(body: AnalysisCreate) -> dict[str, Any]:
    """Merge the top-level mapping into the persisted job configuration."""

    config = dict(body.config or {})
    nested_mapping = config.get("field_mapping")
    mapping = dict(nested_mapping) if isinstance(nested_mapping, dict) else None
    if body.field_mapping:
        mapping = {**(mapping or {}), **body.field_mapping}
    if mapping is not None:
        config["field_mapping"] = mapping
    return config


def _normalise_analysis_mapping(raw_mapping: Any) -> tuple[dict[str, str], list[str], list[str]]:
    if raw_mapping is None:
        return {}, [], []
    if not isinstance(raw_mapping, dict):
        return {}, [], ["field_mapping must be an object"]
    mapping: dict[str, str] = {}
    unknown_keys: list[str] = []
    errors: list[str] = []
    for raw_key, raw_value in raw_mapping.items():
        key = str(raw_key).strip().lower()
        canonical = FIELD_MAPPING_ALIASES.get(key)
        if canonical is None:
            unknown_keys.append(str(raw_key))
            continue
        value = str(raw_value).strip() if raw_value is not None else ""
        if not value:
            errors.append(f"field_mapping.{canonical} cannot be empty")
            continue
        mapping[canonical] = value
    if unknown_keys:
        errors.append(f"Unsupported field mapping keys: {', '.join(sorted(unknown_keys))}")
    duplicate_sources = sorted({source for source in mapping.values() if list(mapping.values()).count(source) > 1})
    for source in duplicate_sources:
        errors.append(f"Column '{source}' cannot be mapped to multiple semantic fields")
    return mapping, unknown_keys, errors


def _analysis_config_validation(version: DatasetVersion, analysis_type: str, config: dict[str, Any]) -> dict[str, Any]:
    columns = {column.name for column in version.columns}
    kind = str(analysis_type or "").strip().lower()
    errors: list[str] = []
    missing_mappings: list[str] = []
    invalid_mappings: dict[str, str] = {}
    mapping, unknown_mapping_keys, mapping_errors = _normalise_analysis_mapping(config.get("field_mapping"))
    errors.extend(mapping_errors)
    if kind not in SUPPORTED_ANALYSIS_TYPES:
        return {
            "errors": [f"Unsupported analysis type: {analysis_type}"],
            "field_mapping": mapping,
            "missing_mappings": missing_mappings,
            "invalid_mappings": invalid_mappings,
            "unknown_mapping_keys": unknown_mapping_keys,
            "mapping_errors": mapping_errors,
        }

    for canonical, source in mapping.items():
        if source not in columns:
            invalid_mappings[canonical] = source
            errors.append(f"Missing column: {source}")

    def require_column(key: str, *aliases: str) -> str | None:
        value = next((config.get(candidate) for candidate in (key, *aliases) if config.get(candidate)), None)
        if not value:
            errors.append(f"{key} is required")
            return None
        if str(value) not in columns:
            errors.append(f"Missing column: {value}")
            return None
        return str(value)

    def require_semantic(canonical: str, *config_keys: str) -> str | None:
        source = mapping.get(canonical)
        if source:
            return source if source in columns else None
        configured = next((config.get(key) for key in config_keys if config.get(key)), None)
        if configured:
            source = str(configured)
            if source not in columns:
                errors.append(f"Missing column: {source}")
                invalid_mappings[canonical] = source
                return None
            return source
        if canonical in columns:
            return canonical
        missing_mappings.append(canonical)
        errors.append(f"field_mapping.{canonical} is required")
        return None

    if kind in {"trend", "time_series"}:
        if mapping.get("event_time"):
            require_semantic("event_time", "time_column", "event_time_column", "date_column")
        else:
            require_column("time_column", "event_time_column", "date_column")
        require_column("metric_column")
    elif kind in {"group", "grouped", "segmentation", "group_analysis"}:
        require_column("group_column", "segment_column")
        if config.get("metric_column") and str(config["metric_column"]) not in columns:
            errors.append(f"Missing column: {config['metric_column']}")
    elif kind in {"funnel", "conversion"}:
        require_semantic("user_id", "user_id_column")
        require_semantic("event_time", "event_time_column", "time_column")
        require_semantic("event_name", "event_name_column")
        steps = config.get("steps")
        if not isinstance(steps, list) or len(steps) < 2 or any(not str(step).strip() for step in steps):
            errors.append("steps must contain at least two event names")
    elif kind in {"retention", "retention_analysis"}:
        require_semantic("user_id", "user_id_column")
        require_semantic("event_time", "event_time_column", "time_column")
        periods = config.get("periods", [1, 7, 30])
        if not isinstance(periods, list) or not periods or any(not isinstance(period, int) or period < 0 for period in periods):
            errors.append("periods must contain non-negative integers")
    elif kind in {"anomaly", "anomalies"}:
        require_column("metric_column")
        if config.get("time_column") and str(config["time_column"]) not in columns:
            errors.append(f"Missing column: {config['time_column']}")
        method = str(config.get("method", "iqr")).lower()
        if method not in {"iqr", "zscore", "z_score", "rolling", "rolling_zscore"}:
            errors.append("method must be iqr, zscore or rolling")
    elif kind in {"health", "health_score"} and not columns:
        errors.append("health analysis requires a non-empty dataset")
    # Preserve stable ordering for deterministic API responses and tests.
    return {
        "errors": list(dict.fromkeys(errors)),
        "field_mapping": mapping,
        "missing_mappings": list(dict.fromkeys(missing_mappings)),
        "invalid_mappings": invalid_mappings,
        "unknown_mapping_keys": unknown_mapping_keys,
        "mapping_errors": mapping_errors,
    }


def _field_mapping_error_message(analysis_type: str) -> str:
    kind = str(analysis_type or "").strip().lower()
    if kind in {"funnel", "conversion"}:
        return "请为漏斗分析指定用户ID、事件时间和事件名称字段"
    if kind in {"retention", "retention_analysis"}:
        return "请为留存分析指定用户ID和事件时间字段"
    return "Please provide the required field mappings for this analysis"


@app.post("/api/v1/analysis-runs/validate-config")
def validate_analysis_config(body: AnalysisCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, project = _dataset_version_for(db, user, body.dataset_version_id)
    if project.id != body.project_id:
        raise error("FORBIDDEN", "Dataset and project do not match", 403)
    columns = {item.name for item in version.columns}
    config = _analysis_request_config(body)
    validation = _analysis_config_validation(version, body.analysis_type, config)
    errors = validation["errors"]
    missing = [error_message.split(": ", 1)[1] for error_message in errors if error_message.startswith("Missing column: ")]
    result: dict[str, Any] = {
        "valid": not errors,
        "analysis_type": body.analysis_type,
        "field_mapping": validation["field_mapping"],
        "available_columns": sorted(columns),
        "missing_columns": list(dict.fromkeys(missing)),
        "missing_mappings": validation["missing_mappings"],
        "invalid_mappings": validation["invalid_mappings"],
        "errors": errors,
    }
    if validation["missing_mappings"]:
        result["error"] = {
            "code": "FIELD_MAPPING_REQUIRED",
            "message": _field_mapping_error_message(body.analysis_type),
            "missing_mappings": validation["missing_mappings"],
            "available_columns": sorted(columns),
        }
    elif validation["invalid_mappings"] or validation["unknown_mapping_keys"] or validation["mapping_errors"]:
        result["error"] = {
            "code": "FIELD_MAPPING_INVALID",
            "message": "Field mapping contains columns or semantic fields that are not available",
            "invalid_mappings": validation["invalid_mappings"],
            "available_columns": sorted(columns),
        }
    return ok(result)


@app.get("/api/v1/analysis-runs")
def list_analysis_runs(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project.id).order_by(AnalysisRun.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(AnalysisRun).where(AnalysisRun.workspace_id.in_(workspace_ids)).order_by(AnalysisRun.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


_AUTO_ANALYSIS_LIMIT = 3


def _auto_analysis_plan(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick up to three analyses from the inferred schema, recording *why*.

    Deterministic and inspectable by design.  Each entry carries a ``reason`` and
    the columns it chose so the report can disclose that the selection was
    automatic and offer a re-pick -- nothing downstream can distinguish an
    analyst's choice from a column-order accident unless we say so here.

    ``funnel`` is never auto-selected: ``run_funnel_analysis`` needs an ordered
    ``steps`` list that cannot be inferred, and guessing produces a plausible but
    wrong funnel.  Stage 4 remains the way to run one.
    """

    by_role = {
        str(column.get("mapping_role")): str(column["name"])
        for column in schema
        if column.get("mapping_role")
    }
    datetimes = [c for c in schema if str(c.get("inferred_type")) in {"datetime", "date"}]
    numerics = [c for c in schema if str(c.get("inferred_type")) in {"integer", "float"}]
    # A grouping column must actually group: high-cardinality strings (names,
    # free text, ids that escaped _IDENTIFIER_NAME) make a useless breakdown.
    categoricals = [
        c
        for c in schema
        if str(c.get("inferred_type")) == "string" and 0.0 < float(c.get("unique_ratio") or 1.0) <= 0.4
    ]
    numerics = sorted(numerics, key=lambda c: (bool(c.get("nullable")), int(c.get("ordinal") or 0)))

    plan: list[dict[str, Any]] = [
        {
            "analysis_type": "eda",
            "config": {},
            "reason": "always_included",
            "columns": [],
        }
    ]

    if by_role.get("user_id") and by_role.get("event_time"):
        plan.append(
            {
                "analysis_type": "retention",
                "config": {"field_mapping": {"user_id": by_role["user_id"], "event_time": by_role["event_time"]}},
                "reason": "user_id_and_event_time_roles_present",
                "columns": [by_role["user_id"], by_role["event_time"]],
            }
        )
    elif datetimes and numerics:
        time_column = by_role.get("event_time") or str(datetimes[0]["name"])
        metric_column = str(numerics[0]["name"])
        plan.append(
            {
                "analysis_type": "trend",
                "config": {"time_column": time_column, "metric_column": metric_column},
                "reason": "first_datetime_column_and_most_complete_numeric_column",
                "columns": [time_column, metric_column],
            }
        )
    elif categoricals and numerics:
        group_column = str(categoricals[0]["name"])
        metric_column = str(numerics[0]["name"])
        plan.append(
            {
                "analysis_type": "group",
                "config": {"group_column": group_column, "metric_column": metric_column},
                "reason": "low_cardinality_string_column_and_numeric_column",
                "columns": [group_column, metric_column],
            }
        )

    if len(plan) < _AUTO_ANALYSIS_LIMIT and numerics:
        plan.append(
            {
                "analysis_type": "anomaly",
                "config": {"metric_column": str(numerics[0]["name"])},
                "reason": "numeric_column_available_for_outlier_scan",
                "columns": [str(numerics[0]["name"])],
            }
        )

    return plan[:_AUTO_ANALYSIS_LIMIT]


def _prepare_analysis_run(
    db: Session,
    project: Project,
    version: DatasetVersion,
    analysis_type: str,
    config: dict[str, Any],
    actor_id: str | None,
) -> tuple[AnalysisRun | None, dict[str, Any]]:
    """Validate, degrade and create an ``AnalysisRun`` without any HTTP coupling.

    ``create_analysis`` is the HTTP wrapper around this; the automatic pipeline in
    ``_handle_dataset_parse`` calls it directly.  Preconditions are *returned* as a
    ``rejection`` mapping rather than raised, so an unattended run can skip one
    analysis type and still produce the others.  The returned run is flushed but
    not committed -- the caller owns the transaction.
    """

    resolved = dict(config)
    kind = analysis_type
    validation = _analysis_config_validation(version, kind, resolved)
    config_errors = validation["errors"]
    degraded_from: str | None = None
    if (
        config_errors
        and validation["missing_mappings"]
        and not validation["invalid_mappings"]
        and not validation["unknown_mapping_keys"]
        and not validation["mapping_errors"]
    ):
        degraded_from = kind
        resolved = {
            key: value
            for key, value in resolved.items()
            if key not in {"field_mapping", "steps", "periods", "metric_column", "time_column", "group_column"}
        }
        resolved["degraded_from"] = degraded_from
        resolved["degrade_reason"] = "missing_field_mapping"
        resolved["missing_mappings"] = validation["missing_mappings"]
        kind = "descriptive"
        validation = _analysis_config_validation(version, kind, resolved)
        config_errors = validation["errors"]
    if config_errors:
        return None, {
            "reason": "config",
            "analysis_type": kind,
            "validation": validation,
            "errors": config_errors,
        }
    if validation["field_mapping"]:
        resolved["field_mapping"] = validation["field_mapping"]
    if version.status not in {"ready", "confirmed"}:
        return None, {"reason": "not_ready", "analysis_type": kind, "validation": validation, "errors": []}
    if version.quality_report is not None and version.quality_report.status == "failed" and not bool(resolved.get("accept_quality_risk")):
        return None, {
            "reason": "quality",
            "analysis_type": kind,
            "validation": validation,
            "errors": [],
            "quality_status": version.quality_report.status,
        }
    run = AnalysisRun(
        workspace_id=project.workspace_id,
        project_id=project.id,
        dataset_version_id=version.id,
        analysis_type=kind,
        config_json=resolved,
        status="queued",
        requested_by=actor_id,
        result_summary={},
    )
    db.add(run)
    db.flush()
    return run, {
        "analysis_type": kind,
        "degraded_from": degraded_from,
        "field_mapping": validation["field_mapping"],
        "config": resolved,
    }


@app.post("/api/v1/analysis-runs")
def create_analysis(body: AnalysisCreate, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, project = _dataset_version_for(db, user, body.dataset_version_id, "editor")
    if project.id != body.project_id:
        raise error("FORBIDDEN", "Dataset and project do not match", 403)
    config = _analysis_request_config(body)
    # A cleaned business table often has no user/event/time columns, so the
    # requested analysis type may simply not apply to it.  That is not a user
    # error: _prepare_analysis_run falls back to a descriptive run rather than
    # blocking the pipeline.  Only missing mappings degrade -- a mapping that
    # points at a nonexistent column is a real mistake and still surfaces as 400.
    run, outcome = _prepare_analysis_run(db, project, version, body.analysis_type, config, user.id)
    if run is None:
        validation = outcome["validation"]
        analysis_type = str(outcome["analysis_type"])
        config_errors = outcome["errors"]
        if outcome["reason"] == "quality":
            raise error("DATASET_NOT_READY", "Quality report requires confirmation before analysis", 422, {"dataset_version_id": version.id, "quality_status": outcome["quality_status"]})
        if outcome["reason"] == "not_ready":
            raise error("DATASET_NOT_READY", "Dataset version is not ready for analysis", 422, {"dataset_version_id": version.id})
        if validation["missing_mappings"]:
            raise error(
                "FIELD_MAPPING_REQUIRED",
                _field_mapping_error_message(analysis_type),
                400,
                {
                    "missing_mappings": validation["missing_mappings"],
                    "available_columns": sorted(column.name for column in version.columns),
                    "errors": config_errors,
                },
            )
        if validation["invalid_mappings"] or validation["unknown_mapping_keys"] or validation["mapping_errors"]:
            raise error(
                "FIELD_MAPPING_INVALID",
                "Field mapping contains columns or semantic fields that are not available",
                400,
                {
                    "invalid_mappings": validation["invalid_mappings"],
                    "available_columns": sorted(column.name for column in version.columns),
                    "errors": config_errors,
                },
            )
        raise error("VALIDATION_ERROR", "Analysis configuration is invalid", 400, config_errors)
    analysis_type = run.analysis_type
    config = outcome["config"]
    job = _job(db, project.workspace_id, "analysis_run", {"analysis_run_id": run.id, "analysis_type": analysis_type, "dataset_version_id": version.id, "config": config, "field_mapping": outcome["field_mapping"], "_actor_id": user.id}, result_type="analysis_run", result_id=run.id)
    audit(db, project.workspace_id, user.id, "analysis.queued", "analysis_run", run.id, {"analysis_type": analysis_type, "requested_analysis_type": body.analysis_type, "degraded_from": outcome["degraded_from"], "job_id": job.id})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"analysis_run": model_dict(run), "artifacts": [], "job": _job_payload(job)})


@app.get("/api/v1/analysis-runs/{run_id}")
def get_analysis(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id)
    return ok(model_dict(run, {"artifacts": [model_dict(item) for item in run.artifacts]}))


@app.get("/api/v1/analysis-runs/{run_id}/artifacts")
def list_artifacts(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id)
    return ok([model_dict(item) for item in run.artifacts], page=1, page_size=len(run.artifacts), total=len(run.artifacts))


@app.get("/api/v1/analysis-artifacts/{artifact_id}")
def get_artifact(artifact_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    artifact = db.get(AnalysisArtifact, artifact_id)
    if artifact is None:
        raise error("NOT_FOUND", "Analysis artifact not found", 404)
    run = db.get(AnalysisRun, artifact.analysis_run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id)
    return ok(model_dict(artifact))


@app.post("/api/v1/analysis-runs/{run_id}/rerun")
def rerun_analysis(run_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id, "editor")
    body = AnalysisCreate(project_id=run.project_id, dataset_version_id=run.dataset_version_id, analysis_type=run.analysis_type, config=run.config_json or {})
    return create_analysis(body, background_tasks, user, db)


def _feedback_payload(item: FeedbackItem) -> dict[str, Any]:
    # Clients consume the documented ``labels`` field, not the storage column
    # name ``labels_json`` (BUG-021).
    return model_dict(item, {"labels": list(item.labels_json or [])})


@app.get("/api/v1/feedback-items")
def list_feedback(project_id: str | None = Query(default=None), status: str | None = Query(default=None), channel: str | None = Query(default=None), label: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(FeedbackItem).where(FeedbackItem.project_id == project.id).order_by(FeedbackItem.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(FeedbackItem).where(FeedbackItem.workspace_id.in_(workspace_ids)).order_by(FeedbackItem.created_at.desc())).all() if workspace_ids else []
    if status:
        rows = [row for row in rows if row.status == status]
    if channel:
        rows = [row for row in rows if row.channel == channel]
    if label:
        rows = [row for row in rows if label in (row.labels_json or [])]
    return paged([_feedback_payload(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/feedback-items")
def create_feedback(body: FeedbackCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    item = FeedbackItem(workspace_id=project.workspace_id, project_id=project.id, content=body.content, external_ref=body.external_ref, user_ref=body.user_ref, channel=body.channel, rating=body.rating, feedback_at=body.feedback_at, labels_json=body.labels, status=body.status)
    db.add(item)
    db.flush()
    db.add(FeedbackNote(project_id=project.id, content=body.content, source=body.channel or "manual", label=(body.labels or [""])[0][:120], sentiment="unknown"))
    audit(db, project.workspace_id, user.id, "feedback.created", "feedback_item", item.id)
    db.commit()
    return ok(_feedback_payload(item))


@app.post("/api/v1/feedback-items/import")
async def import_feedback(background_tasks: BackgroundTasks, project_id: str = Form(...), file: UploadFile = File(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    filename = _safe_name(file.filename or "feedback.csv")
    _reject_unsupported_upload(filename)
    content = await file.read()
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise error("FILE_TOO_LARGE", "Feedback file is too large", 413)
    path = settings.data_path / "uploads" / f"{uuid4().hex}_{filename}"
    path.write_bytes(content)
    relative_path = str(path.relative_to(settings.data_path))
    job = _job(db, project.workspace_id, "feedback_import", {"project_id": project.id, "file_name": filename, "rows": 0, "_storage_path": relative_path, "_actor_id": user.id}, result_type="feedback_items")
    audit(db, project.workspace_id, user.id, "feedback.import_queued", "job", job.id, {"project_id": project.id, "file_name": filename})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"imported": 0, "queued": True, "job": _job_payload(job), "items": []})


@app.get("/api/v1/feedback-imports")
def list_feedback_imports(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    project = project_for(db, user, project_id)
    jobs = db.scalars(
        select(Job)
        .where(Job.workspace_id == project.workspace_id, Job.job_type == "feedback_import")
        .order_by(Job.created_at.desc())
    ).all()
    rows = []
    for job in jobs:
        job_input = job.input_json if isinstance(job.input_json, dict) else {}
        if job_input.get("project_id") != project.id:
            continue
        rows.append({
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "progress": job.progress,
            "current_step": job.current_step,
            "input_json": {"project_id": project.id, "rows": job_input.get("rows", 0)},
            "result_type": job.result_type,
            "attempt_count": job.attempt_count,
            "created_at": serialize(job.created_at),
            "completed_at": serialize(job.completed_at),
        })
    return paged(rows, *pagination, len(rows))


@app.patch("/api/v1/feedback-items/{feedback_id}")
def patch_feedback(feedback_id: str, body: FeedbackPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.get(FeedbackItem, feedback_id)
    if item is None:
        raise error("NOT_FOUND", "Feedback item not found", 404)
    membership(db, user, item.workspace_id, "editor")
    if body.labels is not None:
        item.labels_json = body.labels
    if body.status is not None:
        item.status = body.status
    if body.project_id is not None:
        project = project_for(db, user, body.project_id, "editor")
        if project.workspace_id != item.workspace_id:
            raise error("FORBIDDEN", "Feedback cannot be moved to another workspace", 403)
        item.project_id = project.id
    audit(db, item.workspace_id, user.id, "feedback.updated", "feedback_item", item.id)
    db.commit()
    return ok(_feedback_payload(item))


@app.get("/api/v1/feedback-clusters")
def list_feedback_clusters(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(FeedbackCluster).where(FeedbackCluster.project_id == project.id).order_by(FeedbackCluster.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(FeedbackCluster).where(FeedbackCluster.workspace_id.in_(workspace_ids)).order_by(FeedbackCluster.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/feedback-clusters/generate")
def generate_feedback_clusters(project_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    job = _job(db, project.workspace_id, "feedback_cluster_generation", {"project_id": project.id, "_actor_id": user.id}, result_type="feedback_clusters")
    audit(db, project.workspace_id, user.id, "feedback.clusters_queued", "job", job.id, {"project_id": project.id})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"clusters": [], "queued": True, "job": _job_payload(job)})


@app.patch("/api/v1/feedback-clusters/{cluster_id}")
def patch_feedback_cluster(cluster_id: str, body: FeedbackClusterPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    cluster = db.get(FeedbackCluster, cluster_id)
    if cluster is None:
        raise error("NOT_FOUND", "Feedback cluster not found", 404)
    membership(db, user, cluster.workspace_id, "editor")
    if body.name is not None:
        cluster.name = body.name
    if body.summary is not None:
        cluster.summary = body.summary
    if body.status is not None:
        cluster.status = body.status
    db.commit()
    return ok(model_dict(cluster))


@app.post("/api/v1/feedback-clusters/{cluster_id}/link-task")
def link_cluster_task(cluster_id: str, task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    cluster = db.get(FeedbackCluster, cluster_id)
    task = db.get(Task, task_id)
    if cluster is None or task is None or cluster.workspace_id != task.workspace_id or cluster.project_id != task.project_id:
        raise error("NOT_FOUND", "Feedback cluster or task not found", 404)
    membership(db, user, cluster.workspace_id, "editor")
    evidence = list(cluster.evidence_json or [])
    evidence.append({"type": "task", "id": task.id, "title": task.title})
    cluster.evidence_json = evidence
    db.commit()
    return ok(model_dict(cluster))


@app.get("/api/v1/insights")
def list_insights(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(Insight).where(Insight.project_id == project.id).order_by(Insight.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Insight).where(Insight.workspace_id.in_(workspace_ids)).order_by(Insight.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/insights")
def create_insight(body: InsightCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    _task_for_project(db, project, body.task_id)
    # Drafts may be started without evidence and completed later.  Confirmed
    # insights are the publication boundary, so direct creation at that status
    # must satisfy the same evidence contract as the PATCH lifecycle route.
    evidence = list(body.evidence or [])
    if body.status == "confirmed":
        evidence = _require_nonempty_evidence(evidence, subject="Confirmed insight")
    elif evidence:
        evidence = _require_nonempty_evidence(evidence)
    _check_evidence_scope(db, project.workspace_id, evidence, project.id)
    insight = Insight(workspace_id=project.workspace_id, project_id=project.id, task_id=body.task_id, title=body.title, insight_type=body.insight_type, content=body.content, confidence=body.confidence, evidence_json=evidence, status=body.status, created_by=user.id)
    db.add(insight)
    db.flush()
    audit(db, project.workspace_id, user.id, "insight.created", "insight", insight.id)
    db.commit()
    return ok(model_dict(insight))


@app.get("/api/v1/insights/{insight_id}")
def get_insight(insight_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise error("NOT_FOUND", "Insight not found", 404)
    membership(db, user, insight.workspace_id)
    return ok(model_dict(insight))


@app.patch("/api/v1/insights/{insight_id}")
def patch_insight(insight_id: str, body: InsightPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise error("NOT_FOUND", "Insight not found", 404)
    membership(db, user, insight.workspace_id, "editor")
    candidate_evidence = body.evidence if body.evidence is not None else insight.evidence_json
    if body.status == "confirmed":
        candidate_evidence = _require_nonempty_evidence(candidate_evidence, subject="Confirmed insight")
    elif body.evidence is not None:
        candidate_evidence = _require_nonempty_evidence(candidate_evidence)
    if body.evidence is not None or body.status == "confirmed":
        _check_evidence_scope(db, insight.workspace_id, candidate_evidence, insight.project_id)
    for field, column in (("title", "title"), ("content", "content"), ("confidence", "confidence"), ("evidence", "evidence_json"), ("status", "status")):
        value = getattr(body, field)
        if field == "evidence" and value is not None:
            value = candidate_evidence
        if value is not None:
            setattr(insight, column, value)
    audit(db, insight.workspace_id, user.id, "insight.updated", "insight", insight.id, {"status": insight.status})
    db.commit()
    return ok(model_dict(insight))


def _problem_for(db: Session, user: User, problem_id: str, minimum: str = "viewer") -> ProductProblem:
    problem = db.get(ProductProblem, problem_id)
    if problem is None:
        raise error("NOT_FOUND", "Product problem not found", 404)
    membership(db, user, problem.workspace_id, minimum)
    return problem


def _validate_source_insights(db: Session, project: Project, insight_ids: list[str]) -> list[str]:
    """Confirm every referenced insight exists inside the same project.

    Stage 9 is the point where scattered observations become a named problem, so
    a dangling or cross-project insight id would silently break traceability
    back to the data that motivated the problem.
    """

    cleaned: list[str] = []
    for insight_id in insight_ids:
        insight = db.get(Insight, insight_id)
        if insight is None or insight.project_id != project.id:
            raise error("VALIDATION_ERROR", f"Insight {insight_id} is not part of this project", 422)
        if insight_id not in cleaned:
            cleaned.append(insight_id)
    return cleaned


@app.get("/api/v1/problems")
def list_problems(project_id: str | None = Query(default=None), status: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    stmt = select(ProductProblem)
    if project_id:
        project = project_for(db, user, project_id)
        stmt = stmt.where(ProductProblem.project_id == project.id)
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        if not workspace_ids:
            return paged([], *pagination, 0)
        stmt = stmt.where(ProductProblem.workspace_id.in_(workspace_ids))
    if status:
        stmt = stmt.where(ProductProblem.status == status)
    rows = db.scalars(stmt.order_by(ProductProblem.created_at.desc())).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/problems")
def create_problem(body: ProblemCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    insight_ids = _validate_source_insights(db, project, body.source_insight_ids)
    if body.status == "confirmed" and not insight_ids:
        raise error("VALIDATION_ERROR", "A confirmed problem requires at least one source insight", 422)
    problem = ProductProblem(
        workspace_id=project.workspace_id,
        project_id=project.id,
        title=body.title,
        statement=body.statement,
        impact_scope=body.impact_scope,
        source_insight_ids=insight_ids,
        status=body.status,
        priority=body.priority,
        created_by=user.id,
    )
    db.add(problem)
    db.flush()
    audit(db, project.workspace_id, user.id, "problem.created", "product_problem", problem.id, {"status": problem.status})
    db.commit()
    return ok(model_dict(problem))


@app.get("/api/v1/problems/{problem_id}")
def get_problem(problem_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id)
    return ok(model_dict(problem, {"solutions": [model_dict(option) for option in problem.solutions]}))


@app.patch("/api/v1/problems/{problem_id}")
def patch_problem(problem_id: str, body: ProblemPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id, "editor")
    project = db.get(Project, problem.project_id)
    if project is None:
        raise error("NOT_FOUND", "Project not found", 404)
    candidate_ids = list(problem.source_insight_ids or [])
    if body.source_insight_ids is not None:
        candidate_ids = _validate_source_insights(db, project, body.source_insight_ids)
        problem.source_insight_ids = candidate_ids
    if body.status == "confirmed" and not candidate_ids:
        raise error("VALIDATION_ERROR", "A confirmed problem requires at least one source insight", 422)
    for field in ("title", "statement", "impact_scope", "priority", "status"):
        value = getattr(body, field)
        if value is not None:
            setattr(problem, field, value)
    audit(db, problem.workspace_id, user.id, "problem.updated", "product_problem", problem.id, {"status": problem.status})
    db.commit()
    return ok(model_dict(problem))


@app.get("/api/v1/problems/{problem_id}/solutions")
def list_solutions(problem_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id)
    rows = db.scalars(select(SolutionOption).where(SolutionOption.problem_id == problem.id).order_by(SolutionOption.created_at)).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/problems/{problem_id}/solutions")
def create_solution(problem_id: str, body: SolutionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id, "editor")
    option = SolutionOption(
        workspace_id=problem.workspace_id,
        problem_id=problem.id,
        title=body.title,
        approach=body.approach,
        pros=list(body.pros),
        cons=list(body.cons),
        effort=body.effort,
        created_by=user.id,
    )
    db.add(option)
    db.flush()
    audit(db, problem.workspace_id, user.id, "solution.created", "solution_option", option.id)
    db.commit()
    return ok(model_dict(option))


@app.patch("/api/v1/solutions/{solution_id}")
def patch_solution(solution_id: str, body: SolutionPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    option = db.get(SolutionOption, solution_id)
    if option is None:
        raise error("NOT_FOUND", "Solution option not found", 404)
    membership(db, user, option.workspace_id, "editor")
    if body.status == "selected":
        raise error("VALIDATION_ERROR", "Use POST /api/v1/solutions/{id}/select to choose an option", 422)
    if body.status == "rejected":
        reason = body.reject_reason if body.reject_reason is not None else option.reject_reason
        if not (reason or "").strip():
            raise error("VALIDATION_ERROR", "A rejected option requires reject_reason", 422)
    for field in ("title", "approach", "pros", "cons", "effort", "status", "reject_reason"):
        value = getattr(body, field)
        if value is not None:
            setattr(option, field, value)
    audit(db, option.workspace_id, user.id, "solution.updated", "solution_option", option.id, {"status": option.status})
    db.commit()
    return ok(model_dict(option))


@app.post("/api/v1/solutions/{solution_id}/select")
def select_solution(solution_id: str, body: SolutionSelect, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Choose one option and reject the rest, recording why each lost.

    The losing rationale is mandatory: a decision record that keeps only the
    winner cannot explain itself later.
    """

    option = db.get(SolutionOption, solution_id)
    if option is None:
        raise error("NOT_FOUND", "Solution option not found", 404)
    membership(db, user, option.workspace_id, "editor")
    siblings = db.scalars(select(SolutionOption).where(SolutionOption.problem_id == option.problem_id, SolutionOption.id != option.id)).all()
    missing = [sibling.id for sibling in siblings if not (body.reject_reasons.get(sibling.id) or sibling.reject_reason or "").strip()]
    if missing:
        raise error("VALIDATION_ERROR", "Every non-selected option requires a reject reason", 422, {"missing_reject_reasons": missing})
    option.status = "selected"
    option.reject_reason = ""
    for sibling in siblings:
        sibling.status = "rejected"
        supplied = (body.reject_reasons.get(sibling.id) or "").strip()
        if supplied:
            sibling.reject_reason = supplied
    audit(db, option.workspace_id, user.id, "solution.selected", "solution_option", option.id, {"problem_id": option.problem_id, "rejected": [s.id for s in siblings]})
    db.commit()
    return ok(model_dict(option, {"rejected": [model_dict(s) for s in siblings]}))


@app.get("/api/v1/solutions")
def list_all_solutions(project_id: str | None = Query(default=None), status: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    """Flat solution list across problems, for the stage-10 gate.

    The nested route under a problem stays the canonical way to read one
    problem's options; this exists so the workflow header can tell whether any
    option has been selected without walking every problem.
    """

    stmt = select(SolutionOption)
    if project_id:
        project = project_for(db, user, project_id)
        problem_ids = db.scalars(select(ProductProblem.id).where(ProductProblem.project_id == project.id)).all()
        if not problem_ids:
            return paged([], *pagination, 0)
        stmt = stmt.where(SolutionOption.problem_id.in_(problem_ids))
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        if not workspace_ids:
            return paged([], *pagination, 0)
        stmt = stmt.where(SolutionOption.workspace_id.in_(workspace_ids))
    if status:
        stmt = stmt.where(SolutionOption.status == status)
    rows = db.scalars(stmt.order_by(SolutionOption.created_at.desc())).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@app.get("/api/v1/discussions")
def list_discussions(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    """Copilot sessions with their turn counts, for the stage-8 gate."""

    stmt = select(CopilotSession).where(CopilotSession.user_id == user.id)
    if project_id:
        project = project_for(db, user, project_id)
        stmt = stmt.where(CopilotSession.project_id == project.id)
    rows = db.scalars(stmt.order_by(CopilotSession.created_at.desc())).all()
    items = [
        model_dict(row, {"turn_count": len(row.messages), "stage": (row.page_context_json or {}).get("stage", "")})
        for row in rows
    ]
    return paged(items, *pagination, len(items))


@app.get("/api/v1/decision-proposals")
def list_decisions(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(DecisionProposal).where(DecisionProposal.project_id == project.id).order_by(DecisionProposal.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(DecisionProposal).where(DecisionProposal.workspace_id.in_(workspace_ids)).order_by(DecisionProposal.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/decision-proposals")
def create_decision(body: DecisionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    _task_for_project(db, project, body.task_id)
    _check_evidence_scope(db, project.workspace_id, body.evidence, project.id)
    proposal = DecisionProposal(workspace_id=project.workspace_id, project_id=project.id, task_id=body.task_id, title=body.title, problem_statement=body.problem_statement, proposed_action=body.proposed_action, expected_impact=body.expected_impact, risk_summary=body.risk_summary, validation_plan=body.validation_plan, priority=body.priority, evidence_json=body.evidence, status="draft", created_by=user.id)
    db.add(proposal)
    db.flush()
    audit(db, project.workspace_id, user.id, "decision.created", "decision_proposal", proposal.id)
    db.commit()
    return ok(model_dict(proposal))


@app.get("/api/v1/decision-proposals/{proposal_id}")
def get_decision(proposal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    proposal = db.get(DecisionProposal, proposal_id)
    if proposal is None:
        raise error("NOT_FOUND", "Decision proposal not found", 404)
    membership(db, user, proposal.workspace_id)
    return ok(model_dict(proposal))


@app.patch("/api/v1/decision-proposals/{proposal_id}")
def patch_decision(proposal_id: str, body: DecisionPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    proposal = db.get(DecisionProposal, proposal_id)
    if proposal is None:
        raise error("NOT_FOUND", "Decision proposal not found", 404)
    membership(db, user, proposal.workspace_id, "editor")
    if body.version is not None and body.version != proposal.version:
        raise error("VERSION_CONFLICT", "Proposal version is stale", 409)
    if body.evidence is not None:
        _check_evidence_scope(db, proposal.workspace_id, body.evidence, proposal.project_id)
    for field, column in (("title", "title"), ("problem_statement", "problem_statement"), ("proposed_action", "proposed_action"), ("expected_impact", "expected_impact"), ("risk_summary", "risk_summary"), ("validation_plan", "validation_plan"), ("priority", "priority"), ("evidence", "evidence_json")):
        value = getattr(body, field)
        if value is not None:
            setattr(proposal, column, value)
    proposal.version += 1
    audit(db, proposal.workspace_id, user.id, "decision.updated", "decision_proposal", proposal.id, {"version": proposal.version})
    db.commit()
    return ok(model_dict(proposal))


@app.post("/api/v1/decision-proposals/{proposal_id}/submit")
def submit_decision(proposal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    proposal = db.get(DecisionProposal, proposal_id)
    if proposal is None:
        raise error("NOT_FOUND", "Decision proposal not found", 404)
    membership(db, user, proposal.workspace_id, "editor")
    if proposal.status not in {"draft", "rejected"}:
        raise error("INVALID_STATE", "Proposal cannot be submitted in current state", 409)
    proposal.status = "pending_approval"
    request = ApprovalRequest(workspace_id=proposal.workspace_id, target_type="decision_proposal", target_id=proposal.id, action_type="approve_decision", requested_by=user.id, version=proposal.version)
    db.add(request)
    audit(db, proposal.workspace_id, user.id, "decision.submitted", "decision_proposal", proposal.id)
    db.commit()
    return ok({"proposal": model_dict(proposal), "approval_request": model_dict(request)})


@app.get("/api/v1/approval-requests")
def list_approvals(user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
    rows = db.scalars(select(ApprovalRequest).where(ApprovalRequest.workspace_id.in_(workspace_ids), ApprovalRequest.status == "pending").order_by(ApprovalRequest.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


def _decide_approval(approval_id: str, body: ApprovalDecision, user: User, db: Session, decision: str) -> dict[str, Any]:
    request = db.get(ApprovalRequest, approval_id)
    if request is None:
        raise error("NOT_FOUND", "Approval request not found", 404)
    # Editors can process approvals inside a workspace; Viewer remains read-only.
    membership(db, user, request.workspace_id, "editor")
    if request.status != "pending":
        raise error("INVALID_STATE", "Approval request is already decided", 409)
    if request.version != body.version:
        raise error("VERSION_CONFLICT", "Approval version is stale", 409)
    request.status = decision
    request.decided_by = user.id
    request.decision_note = body.decision_note
    request.decided_at = now()
    proposal = db.get(DecisionProposal, request.target_id) if request.target_type == "decision_proposal" else None
    if proposal is not None and proposal.version != request.version:
        raise error("VERSION_CONFLICT", "Decision proposal changed after approval request", 409)
    if proposal:
        proposal.status = "approved" if decision == "approved" else "rejected"
    audit(db, request.workspace_id, user.id, f"approval.{decision}", request.target_type, request.target_id, {"note": body.decision_note})
    db.commit()
    return ok({"approval_request": model_dict(request), "target": model_dict(proposal) if proposal else None})


@app.post("/api/v1/approval-requests/{approval_id}/approve")
def approve(approval_id: str, body: ApprovalDecision, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _decide_approval(approval_id, body, user, db, "approved")


@app.post("/api/v1/approval-requests/{approval_id}/reject")
def reject(approval_id: str, body: ApprovalDecision, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not body.decision_note.strip():
        raise error("VALIDATION_ERROR", "Rejection reason is required", 400)
    return _decide_approval(approval_id, body, user, db, "rejected")


def _document_payload(document: Document, db: Session) -> dict[str, Any]:
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number)).all()
    current = next((version for version in versions if version.id == document.current_version_id), versions[-1] if versions else None)
    return model_dict(document, {"current_version": model_dict(current) if current else None, "versions": [model_dict(version) for version in versions]})


@app.get("/api/v1/documents")
def list_documents(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(Document).where(Document.project_id == project.id).order_by(Document.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Document).where(Document.workspace_id.in_(workspace_ids)).order_by(Document.created_at.desc())).all() if workspace_ids else []
    return paged([_document_payload(row, db) for row in rows], *pagination, len(rows))


@app.post("/api/v1/documents")
def create_document(body: DocumentCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    _require_confirmed_insight_refs(db, project.workspace_id, body.evidence, project.id)
    document = Document(workspace_id=project.workspace_id, project_id=project.id, document_type=body.document_type, title=body.title, status="draft", created_by=user.id)
    db.add(document)
    db.flush()
    version = DocumentVersion(document_id=document.id, version_number=1, content_markdown=body.content_markdown or f"# {body.title}\n\nDraft pending review.", evidence_json=body.evidence, created_by=user.id)
    db.add(version)
    db.flush()
    document.current_version_id = version.id
    audit(db, project.workspace_id, user.id, "document.created", "document", document.id)
    db.commit()
    return ok(_document_payload(document, db))


def _render_document_markdown(body: DocumentGenerate, db: Session, user: User) -> tuple[str, list[dict[str, Any]]]:
    project = project_for(db, user, body.project_id)
    _require_confirmed_insight_refs(db, project.workspace_id, body.source_refs, project.id)
    evidence: list[dict[str, Any]] = []
    dataset_version_ids: set[str] = set()
    analysis_run_ids: set[str] = set()
    generation_timestamp = serialize(now())
    options = body.template_options if isinstance(body.template_options, dict) else {}
    period_label = str(options.get("period_label") or options.get("period") or "Current period")[:120]
    audience = str(options.get("audience") or "Product team")[:120]
    include_evidence = bool(options.get("include_evidence", True))
    include_acceptance = bool(options.get("include_acceptance_criteria", True))
    include_tracking = bool(options.get("include_tracking_plan", False))
    include_risks = bool(options.get("include_risks", True))
    sections = [f"# {body.title}", "", f"_Draft for {audience} | {period_label}_", ""]
    evidence_sections: list[str] = []
    for reference in body.source_refs:
        if not isinstance(reference, dict):
            raise error("VALIDATION_ERROR", "Document source references must be objects", 400)
        ref_type, ref_id = reference.get("type"), reference.get("id")
        if not ref_type or not ref_id:
            raise error("VALIDATION_ERROR", "Document source references require type and id", 400)
        ref_type = str(ref_type).strip().lower().replace("-", "_")
        safe_reference = {"type": str(ref_type)[:80], "id": str(ref_id)[:120]}
        evidence.append(safe_reference)
        item: Any = None
        if ref_type == "insight":
            item = db.get(Insight, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item is not None and item.status != "confirmed":
                raise error("INSIGHT_NOT_CONFIRMED", "Only confirmed insights can be referenced by a document", 409, {"insight_id": str(ref_id), "status": item.status})
            if item:
                evidence_sections.extend([f"### Insight: {item.title}", "", item.content, ""])
        elif ref_type == "decision_proposal":
            item = db.get(DecisionProposal, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item:
                evidence_sections.extend([f"### Decision proposal: {item.title}", "", f"- Problem: {item.problem_statement}", f"- Action: {item.proposed_action}", f"- Validation: {item.validation_plan}", ""])
        elif ref_type == "analysis_artifact":
            item = db.get(AnalysisArtifact, ref_id)
            analysis_run = db.get(AnalysisRun, item.analysis_run_id) if item else None
            if item and (analysis_run is None or analysis_run.workspace_id != project.workspace_id or analysis_run.project_id != project.id):
                item = None
            if item:
                analysis_run_ids.add(str(item.analysis_run_id))
                analysis_run = db.get(AnalysisRun, item.analysis_run_id)
                if analysis_run is not None:
                    dataset_version_ids.add(str(analysis_run.dataset_version_id))
                evidence_sections.extend([f"### Analysis: {item.title}", "", "```json", json.dumps(item.payload_json, ensure_ascii=False, indent=2, default=str), "```", ""])
        elif ref_type == "feedback_cluster":
            item = db.get(FeedbackCluster, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item:
                evidence_sections.extend([f"### Feedback theme: {item.name}", "", item.summary, ""])
        if item is None:
            raise error("VALIDATION_ERROR", f"Document source reference {ref_type} '{ref_id}' was not found in this project", 400)

        # A confirmed insight can point at an artifact/run/version. Include
        # those upstream identifiers in the immutable evidence manifest too.
        if ref_type == "insight" and isinstance(item.evidence_json, list):
            for nested in item.evidence_json:
                if not isinstance(nested, dict):
                    continue
                nested_type = str(nested.get("type") or "").strip().lower().replace("-", "_")
                nested_id = str(nested.get("id") or "")
                if not nested_id:
                    continue
                if nested_type in {"dataset_version", "data_version"}:
                    dataset_version_ids.add(nested_id)
                elif nested_type in {"analysis_run", "analysis"}:
                    analysis_run_ids.add(nested_id)
                elif nested_type in {"analysis_artifact", "artifact"}:
                    artifact = db.get(AnalysisArtifact, nested_id)
                    if artifact is not None:
                        analysis_run_ids.add(str(artifact.analysis_run_id))
                        run = db.get(AnalysisRun, artifact.analysis_run_id)
                        if run is not None:
                            dataset_version_ids.add(str(run.dataset_version_id))

    if body.document_type == "weekly_report":
        sections.extend([
            "## This period",
            "",
            "Draft summary of confirmed metrics, insights, feedback and completed work.",
            "",
            "## Key changes",
            "",
            "- Confirmed changes and metric movement: pending review.",
            "",
            "## Core issues and feedback",
            "",
            "- Prioritize issues supported by the evidence below.",
            "",
            "## Completed work",
            "",
            "- Confirm completed tasks before publishing this report.",
            "",
            "## Next period plan",
            "",
            "- Convert approved decisions into owned tasks with due dates.",
            "",
        ])
        if include_risks:
            sections.extend(["## Risks and open questions", "", "- Items without evidence remain open questions.", ""])
    elif body.document_type == "retrospective":
        sections.extend([
            "## Background and goal",
            "",
            "Describe the product context, intended outcome and review period.",
            "",
            "## Facts and outcomes",
            "",
            "Separate observed results from interpretation.",
            "",
            "## Root-cause hypotheses",
            "",
            "Record hypotheses with the evidence needed to validate them.",
            "",
            "## Decisions and improvements",
            "",
            "List approved actions, owners and validation plans.",
            "",
            "## Follow-up",
            "",
            "- Add follow-up tasks and due dates before the retrospective is finalized.",
            "",
        ])
        if include_risks:
            sections.extend(["## Risks and unresolved items", "", "- Mark unresolved assumptions explicitly.", ""])
    else:
        sections.extend([
            "## Requirement background",
            "",
            "Describe the user problem and the evidence that motivates this draft.",
            "",
            "## Problem and evidence",
            "",
            "Summarize the confirmed problem, affected users and supporting evidence.",
            "",
            "## Goals and non-goals",
            "",
            "- Goals: define the outcome this proposal should achieve.",
            "- Non-goals: record explicitly excluded scope.",
            "",
            "## Target users and scenarios",
            "",
            "Describe the target user, scenario and expected value.",
            "",
            "## Feature scope",
            "",
            "Describe the in-scope functionality and explicit exclusions.",
            "",
            "## User flow",
            "",
            "Describe the primary user steps and important decision points.",
            "",
            "## Page and interaction",
            "",
            "Describe page states, inputs, outputs and interaction requirements.",
            "",
            "## Data and tracking",
            "",
            "Define the metric dictionary entries and events needed to evaluate the change.",
            "",
        ])
        if include_tracking:
            tracking_events = options.get("tracking_events") or options.get("events") or []
            if isinstance(tracking_events, list) and tracking_events:
                sections.extend(["### Tracking plan", "", *[f"- {str(event)[:240]}" for event in tracking_events[:30]], ""])
            else:
                sections.extend(["### Tracking plan", "", "- Add event names, properties and success metrics before implementation.", ""])
        if include_acceptance:
            sections.extend(["## Acceptance criteria", "", "- The user flow is testable with explicit inputs and expected outputs.", "- Results are tied to approved evidence and the relevant dataset version.", ""])
        if include_risks:
            sections.extend(["## Risks and open questions", "", "- Record rollout risks, dependencies and items awaiting confirmation.", ""])

    if include_evidence:
        sections.extend(["## Evidence", "", *evidence_sections])
    sections.extend(
        [
            "## Evidence manifest",
            "",
            f"- Generated at: {generation_timestamp}",
            f"- Dataset version IDs: {', '.join(sorted(dataset_version_ids)) or 'none'}",
            f"- Analysis run IDs: {', '.join(sorted(analysis_run_ids)) or 'none'}",
            f"- Source refs: {json.dumps(evidence, ensure_ascii=False, sort_keys=True)}",
            "",
        ]
    )
    sections.extend(["## Draft status", "", "This document is a draft. Human editing, evidence review and explicit confirmation are required before publication.", ""])
    return "\n".join(sections), evidence


@app.post("/api/v1/documents/generate")
def generate_document(body: DocumentGenerate, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    flags = _workspace_settings(workspace).get("feature_flags", {}) if workspace else {}
    if not bool(flags.get("document_generation_enabled", True)):
        audit(db, project.workspace_id, user.id, "document.feature_disabled", "project", project.id, {"feature": "document_generation"})
        db.commit()
        raise error("AI_FEATURE_DISABLED", "Document generation is disabled for this workspace", 403)
    _require_confirmed_insight_refs(db, project.workspace_id, body.source_refs, project.id)
    markdown, evidence = _render_document_markdown(body, db, user)
    document = Document(workspace_id=project.workspace_id, project_id=project.id, document_type=body.document_type, title=body.title, status="draft", created_by=user.id)
    db.add(document)
    db.flush()
    version = DocumentVersion(document_id=document.id, version_number=1, content_markdown=markdown, evidence_json=evidence, created_by=user.id)
    db.add(version)
    db.flush()
    document.current_version_id = version.id
    job = _job(
        db,
        project.workspace_id,
        "document_generation",
        {
            "document_id": document.id,
            "source_refs": evidence,
            "template_options": body.template_options,
            "document_type": body.document_type,
            "_actor_id": user.id,
        },
        result_type="document",
        result_id=document.id,
    )
    audit(db, project.workspace_id, user.id, "document.generated", "document", document.id, {"source_count": len(evidence)})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"document": _document_payload(document, db), "job": _job_payload(job)})


@app.get("/api/v1/documents/{document_id}")
def get_document(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id)
    return ok(_document_payload(document, db))


@app.get("/api/v1/documents/{document_id}/versions")
def list_document_versions(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id)
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number.desc())).all()
    return ok([model_dict(item) for item in versions], page=1, page_size=len(versions), total=len(versions))


@app.post("/api/v1/documents/{document_id}/versions")
def create_document_version(document_id: str, body: DocumentVersionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id, "editor")
    document_project = db.get(Project, document.project_id)
    _require_confirmed_insight_refs(db, document.workspace_id, body.evidence, document_project.id if document_project else document.project_id)
    latest = db.scalar(select(func.max(DocumentVersion.version_number)).where(DocumentVersion.document_id == document.id)) or 0
    if body.version is not None and body.version != latest + 1:
        raise error("VERSION_CONFLICT", "Document version does not follow latest version", 409)
    version = DocumentVersion(document_id=document.id, version_number=latest + 1, content_markdown=body.content_markdown, evidence_json=body.evidence, created_by=user.id)
    db.add(version)
    db.flush()
    document.current_version_id = version.id
    document.status = "draft"
    audit(db, document.workspace_id, user.id, "document.version_created", "document", document.id, {"version": version.version_number})
    db.commit()
    return ok(model_dict(version))


@app.post("/api/v1/documents/{document_id}/submit")
def submit_document(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id, "editor")
    document.status = "in_review"
    audit(db, document.workspace_id, user.id, "document.submitted", "document", document.id)
    db.commit()
    return ok(_document_payload(document, db))


@app.get("/api/v1/documents/{document_id}/export")
def export_document(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id)
    version = db.get(DocumentVersion, document.current_version_id) if document.current_version_id else db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number.desc()))
    if version is None:
        raise error("NOT_FOUND", "Document has no versions", 404)
    audit(db, document.workspace_id, user.id, "document.exported", "document", document.id)
    db.commit()
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", document.title)[:120] or "document"
    return Response(content=version.content_markdown, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}.md"'})


def _copilot_orchestrator(
    db: Session,
    user: User,
    workspace_id: str,
    *,
    project_id: str | None = None,
    dataset_id: str | None = None,
    model_id: str | None = None,
    max_output_tokens: int | None = None,
) -> CopilotOrchestrator:
    """Build a request-scoped, workspace-bound Copilot tool registry."""

    expected_project_id = project_id
    expected_dataset_id = dataset_id

    def resolve_version(version_id: str) -> DatasetVersion:
        version = db.get(DatasetVersion, version_id)
        if version is None:
            raise AnalysisPlanError("dataset version was not found")
        dataset = db.get(Dataset, version.dataset_id)
        if dataset is None or dataset.deleted_at is not None or dataset.workspace_id != workspace_id:
            raise ToolPermissionError("dataset is outside the current workspace")
        if expected_project_id and dataset.project_id != expected_project_id:
            raise ToolPermissionError("dataset is outside the current project")
        if expected_dataset_id and version.id != expected_dataset_id:
            raise ToolPermissionError("dataset version is outside the requested scope")
        return version

    def resolve_data(version_id: str) -> Any:
        version = resolve_version(version_id)
        try:
            return _read_dataframe(settings.data_path / version.storage_path, version.file_name)
        except HTTPException as exc:
            raise AnalysisPlanError(str(exc.detail)) from exc
        except Exception as exc:
            raise AnalysisPlanError(f"dataset could not be loaded: {type(exc).__name__}") from exc

    def project_context(requested_project_id: str) -> dict[str, Any]:
        project = db.get(Project, requested_project_id)
        if project is None or project.workspace_id != workspace_id:
            raise ToolPermissionError("project is outside the current workspace")
        if expected_project_id and project.id != expected_project_id:
            raise ToolPermissionError("project is outside the current session")
        tasks = db.scalars(select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc()).limit(20)).all()
        return {"project": model_dict(project), "tasks": [model_dict(task) for task in tasks]}

    def dataset_schema(version_id: str) -> dict[str, Any]:
        version = resolve_version(version_id)
        return {"dataset_version": _version_payload(version), "columns": [model_dict(column) for column in version.columns], "quality_report": model_dict(version.quality_report) if version.quality_report else None}

    def feedback_summary(project_id: str, _dataset_id: str | None = None) -> dict[str, Any]:
        project = db.get(Project, project_id)
        if project is None or project.workspace_id != workspace_id:
            raise ToolPermissionError("project is outside the current workspace")
        items = db.scalars(select(FeedbackItem).where(FeedbackItem.project_id == project.id)).all()
        ratings = [float(item.rating) for item in items if item.rating is not None]
        channel_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        label_counts: dict[str, int] = {}
        for item in items:
            channel = str(item.channel or "unknown")[:80]
            channel_counts[channel] = channel_counts.get(channel, 0) + 1
            status = str(item.status or "unknown")[:40]
            status_counts[status] = status_counts.get(status, 0) + 1
            labels = item.labels_json if isinstance(item.labels_json, list) else []
            for label in labels:
                label_name = str(label or "").strip()[:80]
                if label_name:
                    label_counts[label_name] = label_counts.get(label_name, 0) + 1
        return {
            "project_id": project.id,
            "count": len(items),
            "ratings": {
                "average": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "count": len(ratings),
                "min": min(ratings) if ratings else None,
                "max": max(ratings) if ratings else None,
            },
            "channels": channel_counts,
            "statuses": status_counts,
            "labels": label_counts,
        }

    registry = build_default_tool_registry(resolve_data, project_context=project_context, dataset_schema=dataset_schema, feedback_summary=feedback_summary)
    adapter_settings = DeepSeekSettings.from_app_settings(settings)
    if model_id or max_output_tokens:
        adapter_settings = replace(
            adapter_settings,
            model=str(model_id or adapter_settings.model),
            default_max_tokens=max(1, int(max_output_tokens or adapter_settings.default_max_tokens)),
        )
    return CopilotOrchestrator(DeepSeekAdapter(adapter_settings), registry)


async def _deepseek_answer(question: str, context: dict[str, Any], db: Session | None = None, user: User | None = None, workspace_id: str | None = None, workspace_settings: dict[str, Any] | None = None) -> tuple[str, str, dict[str, Any]]:
    fallback = (
        "我已收到这个问题。当前回答仅基于工作区中已确认的上下文生成，建议先检查数据质量报告、分析产物和证据引用，再确认下一步行动。"
    )
    if not settings.deepseek_api_key:
        return fallback, "not_configured", {
            "provider": "deepseek",
            "configured": False,
            "structured_answer": empty_ai_output(summary=fallback, limitation="AI provider is not configured."),
        }
    try:
        # All outbound provider calls use the explicit V1.1 allow-list.  The
        # request may contain a dataframe or legacy page context, but only
        # aggregate artifacts, schema names/types, goal, metrics and question
        # survive this projection.
        safe_context = build_ai_context(
            project=context.get("project") or {"goal_statement": context.get("goal_statement", context.get("goal", ""))},
            metrics=context.get("metrics") or context.get("metric_definitions") or [],
            artifacts=context.get("artifacts") or [],
            quality=context.get("quality") or context.get("quality_report") or {},
            schema=context.get("schema") or context.get("data_columns") or [],
            dataframe=context.get("dataframe"),
            question=question,
        )
        adapter = DeepSeekAdapter(DeepSeekSettings.from_app_settings(settings))
        project_id = str(context.get("project_id") or "") or None
        dataset_id = str(context.get("dataset_version_id") or context.get("dataset_id") or "") or None
        if db is not None and user is not None and workspace_id:
            result = await _copilot_orchestrator(
                db,
                user,
                workspace_id,
                project_id=project_id,
                dataset_id=dataset_id,
                model_id=str((workspace_settings or {}).get("ai_model_id") or "") or None,
                max_output_tokens=_token_count((workspace_settings or {}).get("ai_max_output_tokens")) or None,
            ).answer(question=question, context=safe_context, project_id=project_id, dataset_id=dataset_id)
            answer_payload = result.get("answer") if isinstance(result.get("answer"), dict) else None
            if result.get("state") == "AskClarification":
                answer = str(result.get("clarifying_question") or "请补充项目或数据版本信息。")
            else:
                answer_value = result.get("answer") or {}
                answer = answer_value if isinstance(answer_value, str) else json.dumps(answer_value, ensure_ascii=False, default=str)
            events: list[dict[str, Any]] = [{"type": "plan.created", "data": {"plan": result.get("plan")}}]
            if result.get("state") == "AskClarification":
                events.append({"type": "plan.validated", "data": {"validated": True, "needs_context": True}})
            else:
                events.append({"type": "plan.validated", "data": {"validated": True}})
            for evidence in result.get("evidence", []):
                tool = evidence.get("tool", "tool")
                events.extend([{"type": "tool.started", "data": {"tool": tool}}, {"type": "tool.completed", "data": {"tool": tool, "evidence": evidence}}])
            if answer_payload:
                recommendations = answer_payload.get("recommendations") or []
                if any(bool(item.get("requires_approval")) for item in recommendations if isinstance(item, dict)):
                    events.append({"type": "approval.required", "data": {"reason": "回答包含需要人工确认的建议"}})
            events.append({"type": "text.delta", "data": {"text": answer}})
            events.append({"type": "run.completed", "data": {"status": "succeeded", "state": result.get("state")}})
            ai_run_details = result.get("ai_run") or {}
            return answer, "succeeded", {
                "provider": "deepseek",
                "configured": True,
                "orchestrated": True,
                "events": events,
                "ai_run": ai_run_details,
                "usage": {"prompt_tokens": ai_run_details.get("prompt_tokens"), "completion_tokens": ai_run_details.get("completion_tokens")},
                "structured_answer": answer_payload,
            }
        result = await adapter.complete(
            messages=[
                ChatMessage("system", "You are the AI Product Workspace Copilot. Distinguish facts, hypotheses, and recommendations. Never execute SQL, code, filesystem or external actions. Return JSON matching this schema exactly: " + json.dumps(AI_OUTPUT_SCHEMA, ensure_ascii=True, separators=(",", ":"))),
                ChatMessage("user", json.dumps(safe_context, ensure_ascii=False, default=str)),
            ],
            response_schema=AI_OUTPUT_SCHEMA,
            request_metadata=AiRequestMetadata(feature_name="copilot", max_tokens=_token_count((workspace_settings or {}).get("ai_max_output_tokens")) or 1800),
        )
        raw_answer = result.structured
        if raw_answer is None and result.content:
            try:
                raw_answer = json.loads(result.content)
            except (TypeError, json.JSONDecodeError):
                raw_answer = None
        try:
            structured_answer = validate_ai_output(raw_answer) if raw_answer is not None else empty_ai_output(summary=result.content or fallback, limitation="Provider response was not structured JSON.")
        except AIOutputValidationError as exc:
            raise AnalysisPlanError(f"AI answer validation failed: {exc}") from exc
        answer = json.dumps(structured_answer, ensure_ascii=False, default=str)
        return answer, "succeeded", {
            "provider": "deepseek",
            "configured": True,
            "orchestrated": False,
            "usage": {"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens},
            "provider_request_id": result.provider_request_id,
            "structured_answer": structured_answer,
            "events": [
                {"type": "plan.created", "data": {"plan": None}},
                {"type": "plan.validated", "data": {"validated": True}},
                {"type": "text.delta", "data": {"text": answer}},
                {"type": "run.completed", "data": {"status": "succeeded"}},
            ],
        }
    except DeepSeekConfigurationError:
        return fallback, "not_configured", {"provider": "deepseek", "configured": False, "events": [{"type": "run.failed", "data": {"code": "LLM_NOT_CONFIGURED", "retryable": False}}]}
    except DeepSeekProviderError as exc:
        return f"{fallback}\n\n模型服务暂时不可用，请稍后重试。", "failed", {"provider": "deepseek", "configured": True, "error": exc.code, "retryable": exc.retryable, "events": [{"type": "run.failed", "data": {"code": exc.code, "retryable": exc.retryable}}]}
    except (AnalysisPlanError, ToolPermissionError, ValueError) as exc:
        code = getattr(exc, "code", "INVALID_ANALYSIS_PLAN")
        return f"{fallback}\n\n当前问题无法安全执行：{str(exc)[:300]}", "failed", {"provider": "deepseek", "configured": True, "error": code, "retryable": False, "events": [{"type": "run.failed", "data": {"code": code, "retryable": False}}]}
    except DeepSeekError as exc:
        code = getattr(exc, "code", "LLM_ERROR")
        retryable = getattr(exc, "retryable", False)
        return f"{fallback}\n\n模型编排失败，请稍后重试。", "failed", {"provider": "deepseek", "configured": True, "error": code, "retryable": retryable, "events": [{"type": "run.failed", "data": {"code": code, "retryable": retryable}}]}
    except Exception as exc:
        return f"{fallback}\n\n模型编排暂时不可用，请稍后重试。", "failed", {"provider": "deepseek", "configured": True, "error": type(exc).__name__, "retryable": False, "events": [{"type": "run.failed", "data": {"code": type(exc).__name__, "retryable": False}}]}


def _ai_interpret_question(body: AIInterpretRequest) -> str:
    """Resolve legacy question aliases without retaining the whole request."""

    for candidate in (body.question, body.prompt, body.user_question, body.content):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()[:4000]
    context = body.context if isinstance(body.context, dict) else {}
    for key in ("question", "user_question"):
        candidate = context.get(key)
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()[:4000]
    return "请解读这些分析结果，并指出与项目目标相关的事实、假设和建议。"


_FEEDBACK_CONTEXT_KEYS = frozenset(
    {
        "feedback",
        "feedback_item",
        "feedback_items",
        "feedback_text",
        "feedback_content",
        "comment",
        "comments",
        "comment_text",
        "message",
        "messages",
        "message_text",
        "review",
        "reviews",
        "review_text",
        "sample",
        "samples",
        "content",
        "content_text",
        "verbatim",
        "verbatims",
    }
)


def _feedback_note_payload(note: FeedbackNote) -> dict[str, Any]:
    """Return the stable V1.1 feedback contract without legacy fields."""

    return {
        "id": note.id,
        "project_id": note.project_id,
        "dataset_version_id": note.dataset_version_id,
        "content": note.content,
        "source": note.source,
        "label": note.label,
        "sentiment": note.sentiment,
        "cluster_name": note.cluster_name,
        "created_at": serialize(note.created_at),
    }


@app.get("/api/v1/feedback-notes")
def list_feedback_notes(
    project_id: str | None = Query(default=None),
    dataset_version_id: str | None = Query(default=None),
    label: str | None = Query(default=None),
    sentiment: str | None = Query(default=None),
    cluster_name: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pagination: tuple[int, int] = Depends(page_params),
) -> dict[str, Any]:
    """List the normalized feedback notes used by V1.1 context builders."""

    query = select(FeedbackNote).order_by(FeedbackNote.created_at.desc())
    if project_id:
        project = project_for(db, user, project_id)
        query = query.where(FeedbackNote.project_id == project.id)
    else:
        # A note without a project is allowed for staging, but it is only
        # visible to an authenticated user when explicitly addressed by id.
        workspace_ids = list(db.scalars(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)).all())
        project_ids = list(db.scalars(select(Project.id).where(Project.workspace_id.in_(workspace_ids))).all()) if workspace_ids else []
        query = query.where(FeedbackNote.project_id.in_(project_ids)) if project_ids else query.where(False)
    if dataset_version_id:
        query = query.where(FeedbackNote.dataset_version_id == dataset_version_id)
    if label:
        query = query.where(FeedbackNote.label == label)
    if sentiment:
        query = query.where(FeedbackNote.sentiment == sentiment)
    if cluster_name:
        query = query.where(FeedbackNote.cluster_name == cluster_name)
    rows = db.scalars(query).all()
    return paged([_feedback_note_payload(row) for row in rows], *pagination, len(rows))


@app.post("/api/v1/feedback-notes")
def create_feedback_note(body: FeedbackNoteCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor") if body.project_id else None
    if body.dataset_version_id:
        version, _dataset, version_project = _dataset_version_for(db, user, body.dataset_version_id, "editor")
        if project is not None and version_project.id != project.id:
            raise error("FORBIDDEN", "Dataset version is outside the selected project", 403)
        if project is None:
            project = version_project
    note = FeedbackNote(project_id=project.id if project else None, dataset_version_id=body.dataset_version_id, content=body.content.strip(), source=body.source.strip(), label=body.label.strip(), sentiment=body.sentiment.strip() or "unknown", cluster_name=body.cluster_name.strip() if body.cluster_name else None)
    db.add(note)
    if project:
        audit(db, project.workspace_id, user.id, "feedback_note.created", "feedback_note", note.id)
    db.commit()
    return ok(_feedback_note_payload(note))


@app.patch("/api/v1/feedback-notes/{note_id}")
def patch_feedback_note(note_id: str, body: FeedbackNotePatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    note = db.get(FeedbackNote, note_id)
    if note is None:
        raise error("NOT_FOUND", "Feedback note not found", 404)
    project = project_for(db, user, note.project_id, "editor") if note.project_id else None
    if body.project_id is not None:
        target = project_for(db, user, body.project_id, "editor")
        note.project_id = target.id
        project = target
    if body.label is not None:
        note.label = body.label.strip()
    if body.sentiment is not None:
        note.sentiment = body.sentiment.strip() or "unknown"
    if body.cluster_name is not None:
        note.cluster_name = body.cluster_name.strip() or None
    if project:
        audit(db, project.workspace_id, user.id, "feedback_note.updated", "feedback_note", note.id)
    db.commit()
    return ok(_feedback_note_payload(note))


def _drop_feedback_content(value: Any, *, key: str | None = None) -> Any:
    """Remove free-form feedback text before artifacts reach the AI boundary."""

    raw_key = str(key or "").strip().replace("-", "_")
    # Imported payloads often use camelCase (for example ``feedbackText``).
    # Normalize it before checking the deny-list so casing cannot bypass it.
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw_key).lower()
    normalized = re.sub(r"[^a-z0-9_]", "", normalized)
    if normalized in _FEEDBACK_CONTEXT_KEYS:
        return None
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, child in value.items():
            cleaned = _drop_feedback_content(child, key=str(raw_key))
            if cleaned is not None:
                output[str(raw_key)] = cleaned
        return output
    if isinstance(value, (list, tuple, set)):
        return [cleaned for item in list(value)[:100] if (cleaned := _drop_feedback_content(item, key=key)) is not None]
    return value


def _ai_interpret_version(
    body: AIInterpretRequest,
    user: User,
    db: Session,
) -> tuple[Project, DatasetVersion | None]:
    """Resolve the project/version pair and enforce the project boundary."""

    requested_version_id = body.dataset_version_id or body.dataset_id
    project: Project | None = None
    if body.project_id:
        project = project_for(db, user, body.project_id)
    if requested_version_id:
        version, _dataset, version_project = _dataset_version_for(db, user, requested_version_id)
        if project is not None and project.id != version_project.id:
            raise error("FORBIDDEN", "Dataset version is outside the selected project", 403)
        project = version_project
        return project, version
    if project is None:
        raise error("VALIDATION_ERROR", "project_id or dataset_version_id is required", 400)
    return project, None


def _ai_interpret_context(
    body: AIInterpretRequest,
    project: Project,
    version: DatasetVersion | None,
    db: Session,
) -> dict[str, Any]:
    """Build the provider allow-list from persisted aggregate metadata only."""

    metric_rows = db.scalars(
        select(MetricDefinition)
        .where(MetricDefinition.workspace_id == project.workspace_id, MetricDefinition.deleted_at.is_(None))
        .order_by(MetricDefinition.name, MetricDefinition.created_at)
        .limit(100)
    ).all()
    artifact_query = (
        select(AnalysisArtifact)
        .join(AnalysisRun, AnalysisRun.id == AnalysisArtifact.analysis_run_id)
        .where(
            AnalysisRun.workspace_id == project.workspace_id,
            AnalysisRun.project_id == project.id,
            AnalysisRun.status == "succeeded",
        )
        .order_by(AnalysisArtifact.created_at.desc())
        .limit(100)
    )
    if version is not None:
        artifact_query = artifact_query.where(AnalysisRun.dataset_version_id == version.id)
    persisted_artifacts = db.scalars(artifact_query).all()

    # Feedback contributes durable context only as an aggregate.  Individual
    # note text never crosses the AI boundary, but labels/sentiment counts can
    # still help the model connect a quantitative result to recurring themes.
    feedback_notes = db.scalars(select(FeedbackNote).where(FeedbackNote.project_id == project.id)).all()
    label_counts: dict[str, int] = {}
    sentiment_counts: dict[str, int] = {}
    cluster_counts: dict[str, int] = {}
    for note in feedback_notes:
        if note.label:
            label_counts[note.label[:120]] = label_counts.get(note.label[:120], 0) + 1
        sentiment = note.sentiment or "unknown"
        sentiment_counts[sentiment] = sentiment_counts.get(sentiment, 0) + 1
        if note.cluster_name:
            cluster_counts[note.cluster_name[:120]] = cluster_counts.get(note.cluster_name[:120], 0) + 1
    feedback_summary = {
        "id": f"feedback-summary-{project.id}",
        "artifact_type": "feedback_summary",
        "title": "Feedback note aggregates",
        "payload_json": {
            "note_count": len(feedback_notes),
            "label_counts": label_counts,
            "sentiment_counts": sentiment_counts,
            "cluster_counts": cluster_counts,
        },
    }

    # A client may provide an already-aggregated artifact for a just-completed
    # run. It is projected through build_ai_context and never persisted raw.
    request_context = body.context if isinstance(body.context, dict) else {}
    request_artifacts = request_context.get("artifacts") or request_context.get("analysis_artifacts") or []
    if isinstance(request_artifacts, dict):
        request_artifacts = [request_artifacts]
    if not isinstance(request_artifacts, (list, tuple)):
        request_artifacts = []

    schema = [model_dict(column) for column in version.columns] if version is not None else None
    quality = version.quality_report if version is not None else None
    safe_request_context = _drop_feedback_content(request_context)
    context = build_ai_context(
        safe_request_context,
        project=project,
        metrics=metric_rows,
        artifacts=[*persisted_artifacts, feedback_summary, *list(_drop_feedback_content(request_artifacts) or [])[:100]],
        quality=quality,
        schema=schema,
        question=_ai_interpret_question(body),
    )
    # Keep this assertion next to the provider boundary. It makes accidental
    # additions to the context contract fail before a request can be sent.
    return assert_safe_ai_context(context)


_AI_EVIDENCE_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)


_AI_ARTIFACT_LIST_LIMIT = 24

# Persisted artifacts carry their numbers under keys ``build_ai_context``
# discards.  Anything not mapped here is dropped by ``_sanitize_aggregate``
# (app/ai_context.py:265), which would leave the model narrating from the schema
# alone.  Left side: real artifact keys.  Right side: allow-listed aggregate keys.
_AI_ARTIFACT_KEY_MAP = {
    "columns": "metrics",
    "stats": "metrics",
    "cohort_results": "cohorts",
    "group_results": "categories",
    "anomalies": "evidence",
    "trend_points": "series",
}
# Row-level or render-only payload that must never reach a provider.
_AI_ARTIFACT_DROP_KEYS = frozenset({"rows", "records", "data", "samples", "chart", "option", "configSnapshot", "chartType", "datasetVersionId"})


def _reduce_artifact_payload_for_ai(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one persisted artifact payload into AI-context-safe keys.

    Verified behaviour: passing a raw artifact to ``build_ai_context`` yields
    ``payload == {}`` because ``_sanitize_aggregate`` drops every list whose key
    is not in its aggregate allowlist, and EDA numbers live under ``columns``
    while tables live under ``rows``.  This maps the aggregate carriers onto
    permitted keys and drops genuine row-level data, so the narration is grounded
    in real numbers instead of a plausible guess.
    """

    reduced: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _AI_ARTIFACT_DROP_KEYS:
            continue
        if isinstance(value, (list, tuple)):
            target = _AI_ARTIFACT_KEY_MAP.get(key, key)
            entries = [item for item in list(value)[:_AI_ARTIFACT_LIST_LIMIT]]
            reduced.setdefault(target, [])
            if isinstance(reduced[target], list):
                reduced[target].extend(entries)
            continue
        if isinstance(value, Mapping):
            nested = _reduce_artifact_payload_for_ai(value)
            if nested:
                reduced[key] = nested
            continue
        reduced[key] = value
    return reduced


def _narration_context(
    project: Project,
    version: DatasetVersion,
    runs: Sequence[AnalysisRun],
    artifacts: Sequence[AnalysisArtifact],
    quality: DataQualityReport | None,
) -> dict[str, Any]:
    """Assemble the allow-listed context for a report narration call."""

    return build_ai_context(
        goal=project.goal_statement or "",
        schema=version.schema_json or [],
        quality=(quality.summary_json if quality is not None else None),
        artifacts=[
            {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "payload": _reduce_artifact_payload_for_ai(artifact.payload_json or {}),
            }
            for artifact in artifacts
        ],
        question="请解读这些自动分析结果：指出可验证的事实、需要进一步验证的假设，以及下一步建议。自动选列的局限必须写进 limitations。",
        metrics=[{"name": "auto_analysis_count", "value": len(runs)}, {"name": "row_count", "value": version.row_count}],
    )


def _normalize_ai_interpret_evidence(
    output: dict[str, Any],
    context: dict[str, Any],
    dataset_version_id: str | None,
) -> dict[str, Any]:
    """Convert model citation prose into real, scoped resource references.

    Providers sometimes return a citation such as ``"artifact <uuid> ..."``
    instead of the exact ID requested by the schema.  Keep only IDs present in
    the allow-listed context.  Descriptions that clearly refer to the artifact
    or version may use the corresponding known resource; all other malformed
    citations are dropped so the UI can mark the claim as unsupported.
    """

    artifacts = [item for item in (context.get("artifacts") or []) if isinstance(item, dict)]
    artifact_ids = {str(item.get("id")) for item in artifacts if item.get("id")}
    first_artifact_id = next((str(item.get("id")) for item in artifacts if item.get("id")), None)
    version_id = str(dataset_version_id) if dataset_version_id else None

    def resolve(reference: Any) -> dict[str, str] | None:
        reference_text = reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False, default=str)
        candidate = reference.get("id") if isinstance(reference, dict) else reference
        candidate_text = str(candidate or "").strip()
        candidates = [candidate_text, *_AI_EVIDENCE_UUID_RE.findall(candidate_text), *_AI_EVIDENCE_UUID_RE.findall(reference_text)]
        for value in candidates:
            if value in artifact_ids:
                return {"type": "analysis_artifact", "id": value}
            if version_id and value == version_id:
                return {"type": "dataset_version", "id": value}

        text_value = reference_text.lower()
        if first_artifact_id and ("artifact" in text_value or "分析产物" in text_value or "留存" in text_value):
            return {"type": "analysis_artifact", "id": first_artifact_id}
        if version_id and any(token in text_value for token in ("quality", "schema", "字段", "数据集", "row_count", "column_count")):
            return {"type": "dataset_version", "id": version_id}
        return None

    normalized_output = dict(output)
    for section in ("facts", "hypotheses", "recommendations"):
        normalized_claims: list[dict[str, Any]] = []
        for claim in output.get(section, []):
            normalized_claim = dict(claim)
            references: list[dict[str, str]] = []
            for raw_reference in claim.get("evidence", []):
                resolved = resolve(raw_reference)
                if resolved and resolved not in references:
                    references.append(resolved)
            normalized_claim["evidence"] = references
            normalized_claims.append(normalized_claim)
        normalized_output[section] = normalized_claims
    return normalized_output


async def _run_ai_stage(
    *,
    db: Session,
    user: User,
    workspace: Workspace,
    feature_name: str,
    system_prompt: str,
    context: dict[str, Any],
    flag_name: str = "insight_suggestions_enabled",
) -> dict[str, Any]:
    """Shared draft-generating AI call for the stage 9/10 endpoints.

    Wraps the same budget reservation, feature-flag check, structured-output
    validation and audit trail as /ai/interpret so a provider outage or an
    unset key degrades to an empty draft instead of a 500.
    """

    budget = _workspace_ai_budget(workspace)
    workspace_settings = _workspace_settings(workspace)
    request_fingerprint = hashlib.sha256(
        json.dumps({"feature": feature_name, "context": context}, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    ai_run = AIRun(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name=feature_name,
        provider="deepseek",
        model=budget["model"],
        request_fingerprint=request_fingerprint,
        status="running",
        input_summary_json={"context": context, "draft": True},
    )
    db.add(ai_run)
    db.flush()

    if not bool(workspace_settings.get("feature_flags", {}).get(flag_name, True)):
        ai_run.status = "failed"
        ai_run.error_code = "AI_FEATURE_DISABLED"
        audit(db, workspace.id, user.id, f"ai.{feature_name}.feature_disabled", "ai_run", ai_run.id, {"feature": feature_name})
        db.commit()
        output = empty_ai_output(summary="AI 当前不可用，请手动填写。", limitation="AI feature is disabled for this workspace.")
        return {"run_id": ai_run.id, "status": "failed", "output": output, "error_code": "AI_FEATURE_DISABLED", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    reserved_daily = _workspace_token_usage(db, workspace, budget["per_request"])
    if reserved_daily > budget["daily"]:
        _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=reserved_daily, reason="Workspace daily AI token budget has been exhausted")
    db.commit()

    structured = empty_ai_output(summary="AI 当前不可用，请手动填写。", limitation="AI provider is not configured.")
    result_status = "not_configured"
    error_code: str | None = None
    provider_request_id: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    started = time.perf_counter()
    adapter = DeepSeekAdapter(DeepSeekSettings.from_app_settings(settings))
    if adapter.configured:
        try:
            result = await adapter.complete(
                messages=[
                    ChatMessage("system", system_prompt + " 返回 JSON，必须包含 facts、hypotheses、recommendations、limitations；每条都必须有 evidence 数组。输出默认是 draft。Schema: " + json.dumps(AI_OUTPUT_SCHEMA, ensure_ascii=True, separators=(",", ":"))),
                    ChatMessage("user", json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)),
                ],
                response_schema=AI_OUTPUT_SCHEMA,
                request_metadata=AiRequestMetadata(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    feature_name=feature_name,
                    request_fingerprint=request_fingerprint,
                    max_tokens=budget["max_output"],
                ),
            )
            raw = result.structured
            if raw is None and result.content:
                try:
                    raw = json.loads(result.content)
                except (TypeError, json.JSONDecodeError):
                    raw = None
            structured = validate_ai_output(raw) if raw is not None else empty_ai_output(summary="模型未返回结构化结果。", limitation="Provider response was not structured JSON.")
            result_status = "succeeded"
            provider_request_id = result.provider_request_id
            prompt_tokens = _token_count(result.prompt_tokens)
            completion_tokens = _token_count(result.completion_tokens)
        except DeepSeekConfigurationError:
            error_code = "LLM_NOT_CONFIGURED"
        except AIOutputValidationError:
            result_status = "failed"
            error_code = "INVALID_AI_OUTPUT"
            structured = empty_ai_output(summary="模型返回格式无法验证。", limitation="Provider response failed the structured output contract.")
        except DeepSeekProviderError:
            result_status = "failed"
            error_code = "LLM_PROVIDER_ERROR"
            structured = empty_ai_output(summary="模型服务暂时不可用。", limitation="Provider request failed; retry later or enter a manual draft.")
        except Exception:
            result_status = "failed"
            error_code = "LLM_ERROR"
            structured = empty_ai_output(summary="AI 暂时不可用。", limitation="Unexpected provider failure; enter a manual draft.")
    else:
        error_code = "LLM_NOT_CONFIGURED"

    observed_tokens = prompt_tokens + completion_tokens
    if result_status == "succeeded":
        prior_daily = _workspace_token_usage(db, workspace, budget["per_request"], exclude_run_id=ai_run.id)
        if observed_tokens > budget["per_request"] or prior_daily + observed_tokens > budget["daily"]:
            _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=prior_daily + observed_tokens, observed_tokens=observed_tokens, reason="AI token budget exceeded for this workspace")
    ai_run = db.get(AIRun, ai_run.id) or ai_run
    ai_run.status = result_status
    ai_run.prompt_tokens = prompt_tokens or None
    ai_run.completion_tokens = completion_tokens or None
    ai_run.latency_ms = int((time.perf_counter() - started) * 1000)
    ai_run.error_code = error_code
    ai_run.output_reference = ai_run.id
    ai_run.input_summary_json = {"context": context, "draft": True, "structured_output": structured, "provider_request_id": provider_request_id}
    audit(db, workspace.id, user.id, f"ai.{feature_name}", "ai_run", ai_run.id, {"status": result_status})
    db.commit()
    return {
        "run_id": ai_run.id,
        "status": result_status,
        "output": structured,
        "error_code": error_code,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": observed_tokens},
    }


@app.post("/api/v1/ai/interpret")
async def ai_interpret(body: AIInterpretRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Interpret aggregate analysis artifacts and return a draft AI result."""

    project, version = _ai_interpret_version(body, user, db)
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    context = _ai_interpret_context(body, project, version, db)
    budget = _workspace_ai_budget(workspace)
    workspace_settings = _workspace_settings(workspace)
    request_fingerprint = hashlib.sha256(
        json.dumps({"feature": "interpret", "context": context}, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    ai_run = AIRun(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="interpret",
        provider="deepseek",
        model=budget["model"],
        request_fingerprint=request_fingerprint,
        status="running",
        input_summary_json={
            "context": context,
            "dataset_version_id": version.id if version is not None else None,
            "draft": True,
        },
    )
    db.add(ai_run)
    db.flush()
    if not bool(workspace_settings.get("feature_flags", {}).get("insight_suggestions_enabled", True)):
        ai_run.status = "failed"
        ai_run.error_code = "AI_FEATURE_DISABLED"
        ai_run.input_summary_json = {**(ai_run.input_summary_json or {}), "events": [{"type": "run.failed", "data": {"code": "AI_FEATURE_DISABLED"}}]}
        audit(db, workspace.id, user.id, "ai.interpret.feature_disabled", "ai_run", ai_run.id, {"feature": "interpret"})
        db.commit()
        output = empty_ai_output(summary="AI 当前不可用，请手动填写解读。", limitation="AI feature is disabled for this workspace.")
        return ok({"run_id": ai_run.id, "status": "failed", "provider": "deepseek", "model": budget["model"], "output": output, "structured": output, "ai_output": output, "insight_status": "draft", "draft": True, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "error_code": ai_run.error_code})

    # Reserve the configured budget before the provider call. This also makes
    # concurrent requests observe the same daily limit as Copilot.
    reserved_daily = _workspace_token_usage(db, workspace, budget["per_request"])
    if reserved_daily > budget["daily"]:
        _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=reserved_daily, reason="Workspace daily AI token budget has been exhausted")
    db.commit()

    fallback = empty_ai_output(summary="AI 当前不可用，请根据分析产物手动填写解读。", limitation="AI provider is not configured.")
    structured = fallback
    result_status = "not_configured"
    error_code: str | None = None
    provider_request_id: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    started = time.perf_counter()
    adapter = DeepSeekAdapter(DeepSeekSettings.from_app_settings(settings))
    if adapter.configured:
        try:
            result = await adapter.complete(
                messages=[
                    ChatMessage(
                        "system",
                        "你是产品分析助手。只根据给定的聚合证据回答，不要猜测原始数据。返回 JSON，必须包含 facts、hypotheses、recommendations、limitations；每条事实、假设和建议都必须有 evidence 数组。输出默认是 draft。Schema: "
                        + json.dumps(AI_OUTPUT_SCHEMA, ensure_ascii=True, separators=(",", ":")),
                    ),
                    ChatMessage("user", json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)),
                ],
                response_schema=AI_OUTPUT_SCHEMA,
                request_metadata=AiRequestMetadata(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    feature_name="interpret",
                    request_fingerprint=request_fingerprint,
                    max_tokens=budget["max_output"],
                ),
            )
            raw = result.structured
            if raw is None and result.content:
                try:
                    raw = json.loads(result.content)
                except (TypeError, json.JSONDecodeError):
                    raw = None
            structured = validate_ai_output(raw) if raw is not None else empty_ai_output(summary="模型未返回结构化结果。", limitation="Provider response was not structured JSON.")
            structured = _normalize_ai_interpret_evidence(structured, context, version.id if version is not None else None)
            result_status = "succeeded"
            provider_request_id = result.provider_request_id
            prompt_tokens = _token_count(result.prompt_tokens)
            completion_tokens = _token_count(result.completion_tokens)
        except DeepSeekConfigurationError:
            result_status = "not_configured"
            error_code = "LLM_NOT_CONFIGURED"
        except AIOutputValidationError:
            result_status = "failed"
            error_code = "INVALID_AI_OUTPUT"
            structured = empty_ai_output(summary="模型返回格式无法验证。", limitation="Provider response failed the structured output contract.")
        except DeepSeekProviderError:
            result_status = "failed"
            error_code = "LLM_PROVIDER_ERROR"
            structured = empty_ai_output(summary="模型服务暂时不可用。", limitation="Provider request failed; retry later or enter a manual draft.")
        except Exception:
            result_status = "failed"
            error_code = "LLM_ERROR"
            structured = empty_ai_output(summary="AI 解读暂时不可用。", limitation="Unexpected provider failure; enter a manual draft.")
    else:
        error_code = "LLM_NOT_CONFIGURED"

    observed_tokens = prompt_tokens + completion_tokens
    if result_status == "succeeded":
        prior_daily = _workspace_token_usage(db, workspace, budget["per_request"], exclude_run_id=ai_run.id)
        if observed_tokens > budget["per_request"] or prior_daily + observed_tokens > budget["daily"]:
            _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=prior_daily + observed_tokens, observed_tokens=observed_tokens, reason="AI token budget exceeded for this workspace")
    ai_run = db.get(AIRun, ai_run.id) or ai_run
    ai_run.status = result_status
    ai_run.prompt_tokens = prompt_tokens or None
    ai_run.completion_tokens = completion_tokens or None
    ai_run.latency_ms = int((time.perf_counter() - started) * 1000)
    ai_run.error_code = error_code
    ai_run.output_reference = ai_run.id
    # Persist only the validated structure and allow-listed input. Never store
    # the provider's raw response or the original request body.
    ai_run.input_summary_json = {
        "context": context,
        "dataset_version_id": version.id if version is not None else None,
        "draft": True,
        "structured_output": structured,
        "provider_request_id": provider_request_id,
    }
    audit(db, workspace.id, user.id, "ai.interpret", "ai_run", ai_run.id, {"status": result_status, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})
    db.commit()
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": observed_tokens}
    payload = {
        "run_id": ai_run.id,
        "status": result_status,
        "provider": "deepseek",
        "model": budget["model"],
        "output": structured,
        "structured": structured,
        "ai_output": structured,
        "insight_status": "draft",
        "draft": True,
        "usage": usage,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "error_code": error_code,
    }
    return ok(payload)


@app.post("/api/v1/ai/frame-problem")
async def ai_frame_problem(body: AIFrameProblemRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stage 9: turn confirmed insights into candidate problem statements.

    Returns a draft only. Nothing is written to product_problems until the user
    posts it back through POST /api/v1/problems.
    """

    project = project_for(db, user, body.project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    insight_ids = _validate_source_insights(db, project, body.insight_ids)
    if not insight_ids:
        raise error("VALIDATION_ERROR", "At least one insight is required to frame a problem", 422)
    insights = [db.get(Insight, insight_id) for insight_id in insight_ids]
    context = {
        "project": {"id": project.id, "name": project.name},
        "question": body.question,
        "insights": [
            {"id": row.id, "title": row.title, "content": row.content, "confidence": row.confidence, "evidence": row.evidence_json}
            for row in insights
            if row is not None
        ],
    }
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="frame_problem",
        system_prompt="你是产品分析助手。只根据给定的洞察证据，把观察归纳成清晰的产品问题陈述，不要猜测原始数据。每个问题必须能追溯到给定的洞察 id。",
        context=context,
        flag_name="insight_suggestions_enabled",
    )
    return ok({**result, "provider": "deepseek", "draft": True, "source_insight_ids": insight_ids})


@app.post("/api/v1/dataset-versions/{version_id}/report-narration")
async def report_narration(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stage 5: narrate the auto-analysis results as a reviewable AI draft.

    Deliberately a separate user-triggered endpoint rather than part of the parse
    job: the job body runs in ``asyncio.to_thread``
    (app/infrastructure/jobs.py:129) and a budget rejection raises 429, which
    would fail an otherwise successful upload.  Output stays a draft -- the AI
    boundary after stage 5 is unchanged.
    """

    version, _, project = _dataset_version_for(db, user, version_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)

    runs = db.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.dataset_version_id == version.id, AnalysisRun.status == "succeeded")
        .order_by(AnalysisRun.created_at)
    ).all()
    if not runs:
        raise error("VALIDATION_ERROR", "No completed analysis to narrate", 400)
    artifacts = db.scalars(
        select(AnalysisArtifact).where(AnalysisArtifact.analysis_run_id.in_([run.id for run in runs]))
    ).all()

    context = _narration_context(project, version, runs, artifacts, version.quality_report)
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="report_narration",
        system_prompt=(
            "你是产品数据分析助手。只根据给定的分析产物解读结果，不要臆测未提供的数据。"
            "分析维度和字段是系统自动选择的，这一点必须作为局限写进 limitations。"
            "每条结论都要引用给定的 artifact id 作为 evidence。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
    )
    result["output"] = _normalize_ai_interpret_evidence(result.get("output") or {}, context, version.id)
    return ok(
        {
            **result,
            "provider": "deepseek",
            "draft": True,
            "dataset_version_id": version.id,
            "analysis_run_ids": [run.id for run in runs],
            "requires_human_confirmation": True,
        }
    )


# ---------------------------------------------------------------------------
# Project-level auto analysis report (upload -> auto analysis -> report)
# ---------------------------------------------------------------------------

_REPORT_DATASET_LIMIT = 5
_REPORT_TREND_POINT_LIMIT = 80


def _latest_project_versions(db: Session, project: Project) -> list[DatasetVersion]:
    """Latest ready version of every active dataset, newest dataset first."""

    datasets = db.scalars(
        select(Dataset)
        .where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None))
        .order_by(Dataset.created_at.desc())
    ).all()
    versions: list[DatasetVersion] = []
    for dataset in datasets:
        ready = [item for item in dataset.versions if item.status in {"ready", "confirmed"}]
        if not ready:
            continue
        versions.append(max(ready, key=lambda item: item.version_number or 0))
    return versions[:_REPORT_DATASET_LIMIT]


def _compute_report_aggregates(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Deterministic per-dataset aggregates that ground the report.

    Runs in a worker thread on plain data (no ORM session).  Payload keys are
    chosen to survive ``build_ai_context``'s sanitizer: lists of mappings must
    live under aggregate keys (``metrics``, ``categories``, ``periods``,
    ``counts``); row-like lists would be dropped at the boundary.  No raw rows
    are ever included -- the model narrates statistics, not cells.
    """

    _require_pandas()
    frame = _read_dataframe(settings.data_path / snapshot["storage_path"], snapshot["file_name"])
    engine = AnalysisEngine(snapshot["version_id"])
    eda = engine.run_eda(frame, top_n=5).to_dict()
    eda_payload = dict(eda.get("payload_json") or {})

    schema_types = {str(item["name"]): str(item["type"]) for item in snapshot.get("columns") or []}
    datetime_column = next((name for name, kind in schema_types.items() if kind == "datetime"), None)
    numeric_column = next((name for name, kind in schema_types.items() if kind in {"integer", "float"}), None)

    columns: list[dict[str, Any]] = []
    for item in list(eda_payload.get("columns") or [])[:30]:
        entry: dict[str, Any] = {
            "name": str(item.get("name")),
            "type": str(item.get("dtype")),
            "missing_rate": round(float(item.get("missing_rate") or 0), 4),
            "unique_count": int(item.get("unique_count") or 0),
        }
        stats = item.get("statistics")
        if isinstance(stats, dict) and stats:
            entry["statistics"] = {
                key: stats.get(key)
                for key in ("count", "mean", "median", "std", "min", "max")
                if stats.get(key) is not None
            }
        top_values = item.get("top_values")
        if isinstance(top_values, list) and top_values:
            entry["categories"] = [
                {"value": str(row.get("value")), "count": int(row.get("count") or 0), "rate": round(float(row.get("rate") or 0), 4)}
                for row in top_values[:5]
                if isinstance(row, Mapping)
            ]
        columns.append(entry)

    aggregates: dict[str, Any] = {
        "dataset_version_id": snapshot["version_id"],
        "name": snapshot["dataset_name"],
        "version_number": snapshot["version_number"],
        "row_count": int(snapshot.get("row_count") or len(frame)),
        "column_count": int(snapshot.get("column_count") or len(frame.columns)),
        "duplicate_rows": int(eda_payload.get("duplicate_rows") or 0),
        "metrics": columns,
    }
    if snapshot.get("quality_score") is not None:
        aggregates["quality_score"] = snapshot["quality_score"]
        aggregates["quality_status"] = snapshot.get("quality_status")
    if isinstance(snapshot.get("missing_values"), dict) and snapshot["missing_values"]:
        aggregates["missing_values"] = snapshot["missing_values"]
    if isinstance(snapshot.get("anomalies"), dict) and snapshot["anomalies"]:
        aggregates["anomalies"] = snapshot["anomalies"]

    correlations: dict[str, float] = {}
    for pair in list(eda_payload.get("correlations") or [])[:12]:
        if isinstance(pair, Mapping) and pair.get("correlation") is not None:
            correlations[f"{pair.get('left')} ~ {pair.get('right')}"] = round(float(pair["correlation"]), 4)
    if correlations:
        aggregates["correlation_pairs"] = correlations

    if datetime_column and numeric_column:
        try:
            parsed = pd.to_datetime(frame[datetime_column], errors="coerce", utc=True, format="mixed").dropna()
            span_days = (parsed.max() - parsed.min()).days if len(parsed) >= 2 else 0
            frequency = "W" if span_days > 70 else "D"
            trend = engine.run_trend_analysis(frame, time_column=datetime_column, metric_column=numeric_column, frequency=frequency).to_dict()
            rows = [row for row in (trend.get("payload_json") or {}).get("rows", []) if isinstance(row, Mapping)]
            if rows:
                values = [row.get("value") for row in rows]
                numeric_values = [float(value) for value in values if value is not None]
                aggregates["trend"] = {
                    "time_column": datetime_column,
                    "metric_column": numeric_column,
                    "frequency": frequency,
                    "periods": [str(row.get("period")) for row in rows[:_REPORT_TREND_POINT_LIMIT]],
                    "counts": values[:_REPORT_TREND_POINT_LIMIT],
                    "first_value": values[0],
                    "last_value": values[-1],
                    "max_value": max(numeric_values) if numeric_values else None,
                    "min_value": min(numeric_values) if numeric_values else None,
                    "last_period_change": rows[-1].get("period_over_period"),
                }
        except Exception:  # noqa: BLE001 - trend is optional grounding for the report
            pass
    return aggregates


def _compute_report_aggregates_batch(snapshots: list[dict[str, Any]]) -> list[Any]:
    """Aggregate every snapshot, keeping per-dataset failures isolated.

    One unreadable file must not sink the whole report: a failing dataset
    returns its exception instance, which the caller records as a
    ``read_failure`` while the remaining datasets still produce sections.
    """

    results: list[Any] = []
    for snapshot in snapshots:
        try:
            results.append(_compute_report_aggregates(snapshot))
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            results.append(exc)
    return results


def _deterministic_report_parts(
    project_name: str, aggregates: list[dict[str, Any]]
) -> tuple[str, str, list[dict[str, str]], list[str]]:
    """Deterministic report body used directly when AI is unavailable, and as
    the persisted trace of the numbers behind an AI-written report."""

    title = f"{project_name} 数据分析报告"
    total_rows = sum(int(item.get("row_count") or 0) for item in aggregates)
    summary = (
        f"本次分析覆盖 {len(aggregates)} 个数据集，共 {total_rows} 行数据。"
        "以下统计全部由确定性计算生成。"
    )

    sections: list[dict[str, str]] = []
    findings: list[str] = []

    overview_lines: list[str] = []
    for item in aggregates:
        line = f"- **{item.get('name')}**：{item.get('row_count')} 行 × {item.get('column_count')} 列"
        if item.get("quality_score") is not None:
            line += f"，质量分 {item.get('quality_score')}（{item.get('quality_status')}）"
        overview_lines.append(line)
    if overview_lines:
        sections.append({"heading": "一、数据概况", "content": "\n".join(overview_lines)})

    distribution_lines: list[str] = []
    statistic_lines: list[str] = []
    for item in aggregates:
        for column in item.get("metrics") or []:
            label = f"{item.get('name')} · {column.get('name')}"
            categories = column.get("categories") or []
            if categories:
                top = categories[0]
                line = f"- {label}：最高占比「{top.get('value')}」{top.get('count')} 条（{round(float(top.get('rate') or 0) * 100, 1)}%）"
                runners = "、".join(f"「{row.get('value')}」{round(float(row.get('rate') or 0) * 100, 1)}%" for row in categories[1:3])
                if runners:
                    line += f"，其次 {runners}"
                distribution_lines.append(line)
                findings.append(f"{label} 中「{top.get('value')}」占比最高（{top.get('count')} 条，{round(float(top.get('rate') or 0) * 100, 1)}%）")
            stats = column.get("statistics")
            if isinstance(stats, dict) and stats.get("mean") is not None:
                median = stats.get("median")
                statistic_lines.append(
                    f"- {label}：均值 {round(float(stats['mean']), 4)}"
                    + (f"，中位数 {round(float(median), 4)}" if median is not None else "")
                    + (f"，范围 [{round(float(stats['min']), 4)}, {round(float(stats['max']), 4)}]" if stats.get("min") is not None and stats.get("max") is not None else "")
                )
                if column.get("missing_rate"):
                    findings.append(f"{label} 缺失率 {round(float(column['missing_rate']) * 100, 1)}%")
    if distribution_lines:
        sections.append({"heading": "二、维度分布", "content": "\n".join(distribution_lines)})
    if statistic_lines:
        sections.append({"heading": "三、数值统计", "content": "\n".join(statistic_lines)})

    trend_lines: list[str] = []
    for item in aggregates:
        trend = item.get("trend")
        if not isinstance(trend, dict):
            continue
        change = trend.get("last_period_change")
        change_text = f"，最近一期环比 {round(float(change) * 100, 1)}%" if isinstance(change, (int, float)) else ""
        trend_lines.append(
            f"- {item.get('name')}：{trend.get('metric_column')} 按 {'周' if trend.get('frequency') == 'W' else '日'} 汇总共 {len(trend.get('periods') or [])} 期，"
            f"从 {trend.get('first_value')} 变化到 {trend.get('last_value')}{change_text}"
        )
    if trend_lines:
        sections.append({"heading": "四、时间趋势", "content": "\n".join(trend_lines)})

    return title, summary, sections, findings[:12]


def _report_markdown(title: str, summary: str, sections: list[dict[str, str]], findings: list[str], recommendations: list[str], limitations: list[str]) -> str:
    parts: list[str] = [f"# {title}", ""]
    if summary:
        parts += [summary, ""]
    for section in sections:
        parts += [f"## {section.get('heading', '')}", "", section.get("content", ""), ""]
    if findings:
        parts += ["## 关键发现", ""] + [f"- {item}" for item in findings] + [""]
    if recommendations:
        parts += ["## 建议", ""] + [f"- {item}" for item in recommendations] + [""]
    if limitations:
        parts += ["## 局限", ""] + [f"- {item}" for item in limitations] + [""]
    return "\n".join(parts).strip()


def _auto_report_payload(report: AutoAnalysisReport) -> dict[str, Any]:
    payload = model_dict(report)
    # Convenience alias: the web client renders the markdown directly.
    payload["markdown"] = report.content_markdown
    return payload


@app.post("/api/v1/projects/{project_id}/auto-report")
async def generate_auto_report(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Generate a project-wide analysis report from deterministic aggregates.

    Numbers first, prose second: pandas computes per-dataset aggregates (EDA,
    distributions, correlations, trend), the provider call only narrates them,
    and the deterministic body is persisted alongside the AI sections so the
    report stays readable when AI is disabled or fails.  The report is stored
    as a draft -- confirmation is a separate user action.
    """

    project = project_for(db, user, project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    versions = _latest_project_versions(db, project)
    if not versions:
        raise error("VALIDATION_ERROR", "No parsed dataset versions in this project; upload data first", 400)

    # Collect plain-data snapshots in this thread, then read files and compute
    # aggregates in a worker thread so the event loop is never blocked.
    snapshots: list[dict[str, Any]] = []
    for version in versions:
        quality = version.quality_report
        quality_summary = quality.summary_json if quality is not None and isinstance(quality.summary_json, dict) else {}
        snapshots.append(
            {
                "version_id": version.id,
                "dataset_name": version.dataset.name,
                "version_number": version.version_number,
                "storage_path": version.storage_path,
                "file_name": version.file_name,
                "row_count": version.row_count,
                "column_count": version.column_count,
                "columns": [{"name": column.name, "type": column.confirmed_type or column.inferred_type} for column in version.columns],
                "quality_score": quality.overall_score if quality is not None else None,
                "quality_status": quality.status if quality is not None else None,
                "missing_values": quality_summary.get("missing_values"),
                "anomalies": quality_summary.get("anomalies"),
            }
        )
    compute_results = await asyncio.to_thread(_compute_report_aggregates_batch, snapshots)
    aggregates: list[dict[str, Any]] = []
    read_failures: list[dict[str, Any]] = []
    for snapshot, result in zip(snapshots, compute_results, strict=True):
        if isinstance(result, dict):
            aggregates.append(result)
        else:
            read_failures.append({"dataset_version_id": snapshot["version_id"], "name": snapshot["dataset_name"]})
    if not aggregates:
        raise error("VALIDATION_ERROR", "Could not read any dataset file to analyse", 400)

    default_title, deterministic_summary, deterministic_sections, deterministic_findings = _deterministic_report_parts(project.name, aggregates)

    budget = _workspace_ai_budget(workspace)
    workspace_settings = _workspace_settings(workspace)
    context = assert_safe_ai_context(
        build_ai_context(
            goal=project.goal_statement or "",
            artifacts=[
                {
                    "id": item["dataset_version_id"],
                    "artifact_type": "dataset_summary",
                    "title": item.get("name"),
                    "payload_json": item,
                }
                for item in aggregates
            ],
            question=(
                "请基于这些聚合统计生成分章节的数据分析报告：先概述数据规模与质量，"
                "再按维度分布、数值统计、时间趋势等主题分章展开，最后给出关键发现与建议。"
                "所有数字必须来自给定统计，不得编造。"
            ),
        )
    )
    ai_run = AIRun(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="auto_report",
        provider="deepseek",
        model=budget["model"],
        request_fingerprint=hashlib.sha256(json.dumps({"feature": "auto_report", "context": context}, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
        status="running",
        input_summary_json={"context": context, "dataset_version_ids": [item["dataset_version_id"] for item in aggregates], "draft": True},
    )
    db.add(ai_run)
    db.flush()
    # Reserve before the report row exists: a budget rejection raises 429 and
    # commits, so it must not leave a dangling draft report behind.  This
    # matches the reservation order used by /ai/interpret and Copilot.
    if bool(workspace_settings.get("feature_flags", {}).get("auto_report_enabled", True)):
        reserved_daily = _workspace_token_usage(db, workspace, budget["per_request"])
        if reserved_daily > budget["daily"]:
            _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=reserved_daily, reason="Workspace daily AI token budget has been exhausted")
    report = AutoAnalysisReport(
        workspace_id=workspace.id,
        project_id=project.id,
        title=default_title,
        status="draft",
        dataset_version_ids=[item["dataset_version_id"] for item in aggregates],
        deterministic_json={"datasets": aggregates, "read_failures": read_failures},
        generated_by=user.id,
    )
    db.add(report)
    db.flush()

    async def _fail_run(code: str) -> None:
        ai_run.status = "failed"
        ai_run.error_code = code
        ai_run.latency_ms = 0
        ai_run.output_reference = report.id

    def _use_deterministic(limitation: str, code: str | None, status_value: str) -> dict[str, Any]:
        output = validate_report_output(
            {
                "title": default_title,
                "summary": deterministic_summary,
                "sections": deterministic_sections,
                "key_findings": deterministic_findings,
                "recommendations": [],
                "limitations": [limitation, "分析维度由系统按列类型自动选择；相关性不代表因果。"],
            }
        )
        report.status = status_value
        report.error_code = code
        return output

    provider_request_id: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    output: dict[str, Any]
    started = time.perf_counter()

    if not bool(workspace_settings.get("feature_flags", {}).get("auto_report_enabled", True)):
        await _fail_run("AI_FEATURE_DISABLED")
        output = _use_deterministic("AI 功能已在此工作空间关闭，本报告仅包含确定性统计结果。", "AI_FEATURE_DISABLED", "failed")
        audit(db, workspace.id, user.id, "report.feature_disabled", "ai_run", ai_run.id, {"feature": "auto_report"})
    else:
        adapter = DeepSeekAdapter(DeepSeekSettings.from_app_settings(settings))
        if not adapter.configured:
            await _fail_run("LLM_NOT_CONFIGURED")
            output = _use_deterministic("AI 服务未配置，本报告仅包含确定性统计结果，未包含模型解读。", "LLM_NOT_CONFIGURED", "not_configured")
        else:
            system_prompt = (
                "你是资深产品数据分析师，为产品团队撰写数据分析报告。只使用给定的聚合统计，"
                "禁止编造任何未提供的数字，禁止输出或猜测原始行数据。要求："
                "1) title 概括数据主题；2) summary 用 3-5 句话概述数据规模、质量与总体结论；"
                "3) sections 分 3-6 个主题章节（如 数据概况、核心维度分布、数值统计、时间趋势、数据质量），"
                "每章 content 用 Markdown，包含要点列表与具体数字，每章不超过 400 字；"
                "4) key_findings 列出最重要的发现（最多 8 条），每条必须包含具体数字；"
                "5) recommendations 给出可执行的下一步（最多 6 条），与发现一一对应；"
                "6) limitations 写明分析局限（自动选列、聚合统计、相关性不代表因果）。"
                "输出必须完整闭合 JSON，全部使用中文。Schema: "
                + json.dumps(REPORT_OUTPUT_SCHEMA, ensure_ascii=True, separators=(",", ":"))
            )
            # A full Chinese report needs far more room than the conversational
            # default; a truncated JSON body was the main cause of INVALID_AI_OUTPUT.
            report_max_tokens = min(8192, budget["per_request"])
            compact_retry = False
            try:
                result = await adapter.complete(
                    messages=[
                        ChatMessage("system", system_prompt),
                        ChatMessage("user", json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)),
                    ],
                    response_schema=REPORT_OUTPUT_SCHEMA,
                    request_metadata=AiRequestMetadata(
                        workspace_id=workspace.id,
                        user_id=user.id,
                        feature_name="auto_report",
                        max_tokens=report_max_tokens,
                    ),
                )
                raw = result.structured
                if raw is None and result.content:
                    try:
                        raw = json.loads(result.content)
                    except (TypeError, json.JSONDecodeError):
                        raw = None
                if raw is None and getattr(result, "finish_reason", None) == "length":
                    # The report hit the token ceiling mid-JSON. One compact retry
                    # costs a second call but usually salvages a complete answer.
                    compact_retry = True
                if raw is None and not compact_retry:
                    raise AIOutputValidationError("Provider response was not structured JSON.")
                if compact_retry:
                    compact_system = (
                        "你是数据分析师。基于给定聚合统计输出极简版报告 JSON：sections 最多 3 章、每章 content 不超过 200 字；"
                        "key_findings 最多 4 条；recommendations 最多 3 条；limitations 最多 3 条。"
                        "输出必须完整闭合 JSON，全部使用中文。Schema: "
                        + json.dumps(REPORT_OUTPUT_SCHEMA, ensure_ascii=True, separators=(",", ":"))
                    )
                    result = await adapter.complete(
                        messages=[
                            ChatMessage("system", compact_system),
                            ChatMessage("user", json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)),
                        ],
                        response_schema=REPORT_OUTPUT_SCHEMA,
                        request_metadata=AiRequestMetadata(
                            workspace_id=workspace.id,
                            user_id=user.id,
                            feature_name="auto_report_compact",
                            max_tokens=report_max_tokens,
                        ),
                    )
                    raw = result.structured
                    if raw is None and result.content:
                        try:
                            raw = json.loads(result.content)
                        except (TypeError, json.JSONDecodeError):
                            raw = None
                    if raw is None:
                        raise AIOutputValidationError("Provider response was not structured JSON.")
                    # Both provider calls share the run's token accounting.
                    prompt_tokens += _token_count(result.prompt_tokens)
                    completion_tokens += _token_count(result.completion_tokens)
                output = validate_report_output(raw)
                report.status = "succeeded"
                report.error_code = None
                provider_request_id = result.provider_request_id
                if not compact_retry:
                    prompt_tokens = _token_count(result.prompt_tokens)
                    completion_tokens = _token_count(result.completion_tokens)
            except DeepSeekConfigurationError:
                await _fail_run("LLM_NOT_CONFIGURED")
                output = _use_deterministic("AI 服务未配置，本报告仅包含确定性统计结果。", "LLM_NOT_CONFIGURED", "not_configured")
            except (AIOutputValidationError, AnalysisPlanError):
                await _fail_run("INVALID_AI_OUTPUT")
                output = _use_deterministic("模型返回格式无法验证，本报告仅包含确定性统计结果。", "INVALID_AI_OUTPUT", "failed")
            except DeepSeekProviderError:
                await _fail_run("LLM_PROVIDER_ERROR")
                output = _use_deterministic("模型服务暂时不可用，本报告仅包含确定性统计结果。", "LLM_PROVIDER_ERROR", "failed")
            except Exception:
                await _fail_run("LLM_ERROR")
                output = _use_deterministic("AI 暂时不可用，本报告仅包含确定性统计结果。", "LLM_ERROR", "failed")

            observed_tokens = prompt_tokens + completion_tokens
            if report.status == "succeeded":
                prior_daily = _workspace_token_usage(db, workspace, budget["per_request"], exclude_run_id=ai_run.id)
                if observed_tokens > budget["per_request"] or prior_daily + observed_tokens > budget["daily"]:
                    _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=prior_daily + observed_tokens, observed_tokens=observed_tokens, reason="AI token budget exceeded for this workspace")

    ai_run = db.get(AIRun, ai_run.id) or ai_run
    ai_run.status = "succeeded" if report.status == "succeeded" else ai_run.status
    ai_run.prompt_tokens = prompt_tokens or None
    ai_run.completion_tokens = completion_tokens or None
    ai_run.latency_ms = int((time.perf_counter() - started) * 1000)
    ai_run.output_reference = report.id
    ai_run.input_summary_json = {
        "context": context,
        "dataset_version_ids": [item["dataset_version_id"] for item in aggregates],
        "draft": True,
        "structured_output": output,
        "provider_request_id": provider_request_id,
    }
    report.title = str(output.get("title") or default_title)[:255]
    report.summary = str(output.get("summary") or "")
    report.sections_json = list(output.get("sections") or [])
    report.key_findings = list(output.get("key_findings") or [])
    report.recommendations = list(output.get("recommendations") or [])
    report.limitations = list(output.get("limitations") or [])
    report.content_markdown = _report_markdown(
        report.title, report.summary, report.sections_json, report.key_findings, report.recommendations, report.limitations
    )
    report.ai_run_id = ai_run.id
    audit(
        db,
        workspace.id,
        user.id,
        "report.generated",
        "auto_report",
        report.id,
        {"status": report.status, "datasets": len(aggregates), "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    )
    db.commit()
    return ok(
        {
            "report": _auto_report_payload(report),
            "run_id": ai_run.id,
            "status": report.status,
            "error_code": report.error_code,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }
    )


@app.get("/api/v1/projects/{project_id}/auto-reports")
def list_auto_reports(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pagination: tuple[int, int] = Depends(page_params),
) -> dict[str, Any]:
    project = project_for(db, user, project_id)
    filters = [AutoAnalysisReport.project_id == project.id]
    total = db.scalar(select(func.count()).select_from(AutoAnalysisReport).where(*filters)) or 0
    page, page_size = pagination
    rows = db.scalars(
        select(AutoAnalysisReport)
        .where(*filters)
        .order_by(AutoAnalysisReport.created_at.desc(), AutoAnalysisReport.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ok([_auto_report_payload(row) for row in rows], page=page, page_size=page_size, total=total)


@app.get("/api/v1/auto-reports/{report_id}")
def get_auto_report(report_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(AutoAnalysisReport, report_id)
    if report is None:
        raise error("NOT_FOUND", "Report not found", 404)
    project_for(db, user, report.project_id)
    return ok(_auto_report_payload(report))


@app.post("/api/v1/auto-reports/{report_id}/confirm")
def confirm_auto_report(report_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Human confirmation of a generated report.  Idempotent."""

    report = db.get(AutoAnalysisReport, report_id)
    if report is None:
        raise error("NOT_FOUND", "Report not found", 404)
    project_for(db, user, report.project_id, "editor")
    if report.status != "confirmed":
        report.status = "confirmed"
        report.confirmed_by = user.id
        report.confirmed_at = now()
        audit(db, report.workspace_id, user.id, "report.confirmed", "auto_report", report.id)
        db.commit()
    return ok(_auto_report_payload(report))


@app.post("/api/v1/ai/propose-solutions")
async def ai_propose_solutions(body: AIProposeSolutionsRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stage 10: draft candidate solution options for a confirmed problem."""

    problem = _problem_for(db, user, body.problem_id, "editor")
    workspace = db.get(Workspace, problem.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    existing = db.scalars(select(SolutionOption).where(SolutionOption.problem_id == problem.id)).all()
    context = {
        "problem": {"id": problem.id, "title": problem.title, "statement": problem.statement, "impact_scope": problem.impact_scope, "priority": problem.priority},
        "option_count": body.option_count,
        "existing_options": [{"id": row.id, "title": row.title, "approach": row.approach} for row in existing],
    }
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="propose_solutions",
        system_prompt=f"你是产品方案助手。针对给定的产品问题，提出 {body.option_count} 个互不重复的候选方案，每个方案说明做法、优点、缺点和工作量（S/M/L）。不要重复已有方案。",
        context=context,
        flag_name="insight_suggestions_enabled",
    )
    return ok({**result, "provider": "deepseek", "draft": True, "problem_id": problem.id})


@app.post("/api/v1/ai/draft-document")
def ai_draft_document(body: DocumentGenerate, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """V1.1 name for the existing draft document generation workflow."""

    return generate_document(body, background_tasks, user, db)


@app.post("/api/v1/ai/cluster-feedback")
def ai_cluster_feedback(body: AIClusterFeedbackRequest, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue deterministic feedback grouping; every resulting cluster stays draft."""

    project = project_for(db, user, body.project_id, "editor")
    version: DatasetVersion | None = None
    if body.dataset_version_id:
        version, _dataset, version_project = _dataset_version_for(db, user, body.dataset_version_id, "viewer")
        if version_project.id != project.id:
            raise error("FORBIDDEN", "Dataset version is outside the selected project", 403)
    # Validate and discard caller context so raw feedback text cannot be copied
    # into a job or an AI run. Clustering itself uses the existing deterministic
    # worker and writes status='draft'.
    _ = build_ai_context(
        body.context,
        project=project,
        schema=[model_dict(column) for column in version.columns] if version is not None else None,
        quality=version.quality_report if version is not None else None,
        question="反馈主题聚类",
    )
    return generate_feedback_clusters(body.project_id, background_tasks, user, db)


@app.get("/api/v1/ai/usage")
def ai_usage(workspace_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return aggregate AI token usage for workspaces visible to the user."""

    if workspace_id:
        membership(db, user, workspace_id)
        workspace_ids = [workspace_id]
    else:
        workspace_ids = list(db.scalars(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)).all())
    rows = db.scalars(select(AIRun).where(AIRun.workspace_id.in_(workspace_ids))).all() if workspace_ids else []
    prompt_total = sum(_token_count(row.prompt_tokens) for row in rows)
    completion_total = sum(_token_count(row.completion_tokens) for row in rows)
    by_feature: dict[str, dict[str, int]] = {}
    for row in rows:
        feature = str(row.feature_name or "unknown")[:80]
        bucket = by_feature.setdefault(feature, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        prompt = _token_count(row.prompt_tokens)
        completion = _token_count(row.completion_tokens)
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt
        bucket["completion_tokens"] += completion
        bucket["total_tokens"] += prompt + completion
    total = prompt_total + completion_total
    payload = {
        "workspace_ids": workspace_ids,
        "call_count": len(rows),
        "calls": len(rows),
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "total_tokens": total,
        "total_prompt_tokens": prompt_total,
        "total_completion_tokens": completion_total,
        "by_feature": by_feature,
        "successful_calls": sum(1 for row in rows if row.status == "succeeded"),
        "failed_calls": sum(1 for row in rows if row.status not in {"succeeded"}),
    }
    return ok(payload)


@app.post("/api/v1/copilot/sessions")
def create_copilot_session(body: CopilotSessionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, body.workspace_id)
    if body.project_id:
        project_for(db, user, body.project_id)
    session = CopilotSession(workspace_id=body.workspace_id, project_id=body.project_id, user_id=user.id, page_context_json=body.page_context)
    db.add(session)
    db.flush()
    audit(db, body.workspace_id, user.id, "copilot.session_created", "copilot_session", session.id)
    db.commit()
    return ok(model_dict(session, {"messages": []}))


@app.post("/api/v1/copilot/sessions/{session_id}/messages")
async def copilot_message(session_id: str, body: CopilotMessageCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session = db.get(CopilotSession, session_id)
    if session is None:
        raise error("NOT_FOUND", "Copilot session not found", 404)
    membership(db, user, session.workspace_id)
    if session.user_id != user.id:
        raise error("FORBIDDEN", "Copilot session belongs to another user", 403)
    workspace = db.get(Workspace, session.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    # Serialize budget reservations per workspace where the database supports
    # row locks; SQLite safely treats this as a no-op for local development.
    workspace = db.scalar(select(Workspace).where(Workspace.id == session.workspace_id).with_for_update()) or workspace
    workspace_settings = _workspace_settings(workspace)
    budget = _workspace_ai_budget(workspace)
    # Persist and forward only the V1.1 allowlisted context.  Legacy callers may
    # still send page metadata/raw_rows; those fields are intentionally omitted.
    safe_question = str(body.content)[:4000]
    safe_context = build_ai_context(body.context, question=safe_question)
    user_message = CopilotMessage(session_id=session.id, role="user", content_json={"content": safe_question, "context": safe_context})
    db.add(user_message)
    db.flush()
    ai_run = AIRun(workspace_id=session.workspace_id, user_id=user.id, feature_name="copilot", provider="deepseek", model=budget["model"], request_fingerprint=hashlib.sha256(json.dumps({"question": safe_question, "context": safe_context}, ensure_ascii=True, sort_keys=True, default=str).encode()).hexdigest(), status="running", input_summary_json={"session_id": session.id, "question": safe_question, "context": safe_context})
    db.add(ai_run)
    db.flush()
    if not bool(workspace_settings.get("feature_flags", {}).get("copilot_enabled", True)):
        ai_run.status = "failed"
        ai_run.error_code = "AI_FEATURE_DISABLED"
        audit(db, session.workspace_id, user.id, "copilot.feature_disabled", "ai_run", ai_run.id, {"feature": "copilot"})
        db.commit()
        raise error("AI_FEATURE_DISABLED", "Copilot is disabled for this workspace", 403)
    reserved_daily = _workspace_token_usage(db, workspace, budget["per_request"])
    if reserved_daily > budget["daily"]:
        _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=reserved_daily, reason="Workspace daily AI token budget has been exhausted")
    db.commit()
    started = time.perf_counter()
    copilot_context = {**safe_context, "project_id": session.project_id, "workspace_id": session.workspace_id, "user_id": user.id}
    answer, result_status, details = await _deepseek_answer(
        safe_question,
        copilot_context,
        db,
        user,
        session.workspace_id,
        {**workspace_settings, "ai_max_output_tokens": budget["max_output"], "ai_model_id": budget["model"]},
    )
    ai_run = db.get(AIRun, ai_run.id)
    ai_run.status = result_status
    ai_run.latency_ms = int((time.perf_counter() - started) * 1000)
    usage = details.get("usage", {})
    ai_run.prompt_tokens = _token_count(usage.get("prompt_tokens")) or None
    ai_run.completion_tokens = _token_count(usage.get("completion_tokens")) or None
    ai_run.error_code = details.get("error") if result_status == "failed" else None
    events = details.get("events")
    if not events:
        # Terminal SSE event must reflect the real outcome so clients can stop
        # spinners on failed/not-configured runs (BUG-002).
        terminal_type = "run.completed" if result_status == "succeeded" else "run.failed"
        terminal_data: dict[str, Any] = {"status": result_status}
        if details.get("error"):
            terminal_data["error"] = details.get("error")
        events = [{"type": terminal_type, "data": terminal_data}]
    bounded_answer = str(answer)[:6000]
    ai_run.input_summary_json = {"session_id": session.id, "question": safe_question, "context": build_ai_context(copilot_context, question=safe_question), "answer": bounded_answer, "structured_answer": details.get("structured_answer"), "events": events, "provider": {key: value for key, value in details.items() if key not in {"events", "structured_answer", "ai_run"}}}
    observed_tokens = _token_count(ai_run.prompt_tokens) + _token_count(ai_run.completion_tokens)
    if result_status == "succeeded":
        effective_tokens = observed_tokens or budget["per_request"]
        prior_daily = _workspace_token_usage(db, workspace, budget["per_request"], exclude_run_id=ai_run.id)
        if observed_tokens > budget["per_request"] or prior_daily + effective_tokens > budget["daily"]:
            _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=prior_daily + effective_tokens, observed_tokens=observed_tokens, reason="AI token budget exceeded for this workspace")
    ai_run.output_reference = ai_run.id
    assistant = CopilotMessage(session_id=session.id, role="assistant", content_json={"content": bounded_answer, "structured": details.get("structured_answer"), "status": result_status}, ai_run_id=ai_run.id)
    db.add(assistant)
    session.summary = bounded_answer[:1000]
    audit(db, session.workspace_id, user.id, "copilot.run", "ai_run", ai_run.id, {"status": result_status, "feature": "copilot", "prompt_tokens": _token_count(ai_run.prompt_tokens), "completion_tokens": _token_count(ai_run.completion_tokens), "total_tokens": observed_tokens})
    db.commit()
    return ok({"run_id": ai_run.id, "message_id": assistant.id, "status": ai_run.status})


def _sse(event_type: str, data: Any) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.get("/api/v1/copilot/runs/{run_id}/events")
async def copilot_events(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    run = db.get(AIRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Copilot run not found", 404)
    membership(db, user, run.workspace_id)
    if run.user_id != user.id:
        raise error("FORBIDDEN", "Copilot run belongs to another user", 403)
    events = (run.input_summary_json or {}).get("events", [])

    async def stream():
        for event in events:
            await asyncio.sleep(0)
            yield _sse(event.get("type", "message"), event.get("data", {}))
        yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/copilot/runs/{run_id}")
def get_copilot_run(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AIRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Copilot run not found", 404)
    membership(db, user, run.workspace_id)
    if run.user_id != user.id:
        raise error("FORBIDDEN", "Copilot run belongs to another user", 403)
    payload = run.input_summary_json or {}
    return ok({"id": run.id, "status": run.status, "feature_name": run.feature_name, "model": run.model, "answer": payload.get("answer"), "structured_answer": payload.get("structured_answer"), "events": payload.get("events", []), "created_at": serialize(run.created_at), "latency_ms": run.latency_ms, "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens, "error_code": run.error_code, "budget": payload.get("budget")})


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise error("NOT_FOUND", "Job not found", 404)
    membership(db, user, job.workspace_id)
    return ok(_job_payload(job))


@app.post("/api/v1/jobs/{job_id}/retry")
def retry_job(job_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise error("NOT_FOUND", "Job not found", 404)
    membership(db, user, job.workspace_id, "editor")
    if job.status not in {"failed", "cancelled"}:
        raise error("INVALID_STATE", "Only failed or cancelled jobs can be retried", 409)
    source = job.input_json if isinstance(job.input_json, dict) else {}
    if not bool(source.get("_retryable", True)) or not job_executor.has_handler(job.job_type):
        raise error("INVALID_STATE", "This job cannot be retried safely", 409)
    job.status = "queued"
    job.progress = 0
    job.current_step = "queued_for_retry"
    job.error_code = None
    job.error_message = None
    job.started_at = None
    job.completed_at = None
    audit(db, job.workspace_id, user.id, "job.retry_queued", "job", job.id, {"job_type": job.job_type})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok(_job_payload(job))


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise error("NOT_FOUND", "Job not found", 404)
    membership(db, user, job.workspace_id, "editor")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise error("INVALID_STATE", "Job is already completed", 409)
    job_executor.mark_cancelled(db, job)
    audit(db, job.workspace_id, user.id, "job.cancelled", "job", job.id)
    db.commit()
    return ok(_job_payload(job))


@app.get("/api/v1/copilot/sessions/{session_id}")
def get_copilot_session(session_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session = db.get(CopilotSession, session_id)
    if session is None:
        raise error("NOT_FOUND", "Copilot session not found", 404)
    membership(db, user, session.workspace_id)
    if session.user_id != user.id:
        raise error("FORBIDDEN", "Copilot session belongs to another user", 403)
    return ok(model_dict(session, {"messages": [model_dict(message) for message in session.messages]}))


# Register after all handler dependencies have been defined. Startup recovery can
# then safely replay jobs committed by an earlier process.
_register_job_handlers()
