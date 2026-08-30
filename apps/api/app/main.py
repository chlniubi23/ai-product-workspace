from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from . import db as database
from .ai_context import (
    AI_OUTPUT_SCHEMA,
    PROBLEM_DRAFT_SCHEMA,
    REPORT_OUTPUT_SCHEMA,
    SOLUTION_DRAFTS_SCHEMA,
    AIOutputValidationError,
    assert_safe_ai_context,
    build_ai_context,
    empty_ai_output,
    extract_ai_insights,
    validate_ai_output,
    validate_problem_draft,
    validate_report_output,
    validate_solution_drafts,
)
from .auth import create_access_token, get_current_user, hash_password, password_needs_rehash, verify_password
from .common import (
    _redact_validation_details,
    _request_id,
    error,
    model_dict,
    ok,
    page_params,
    paged,
    serialize,
)
from .config import settings
from .db import get_db, init_db
from .infrastructure.llm.deepseek import (
    AiRequestMetadata,
    AnalysisPlanError,
    ChatMessage,
    DeepSeekAdapter,
    DeepSeekConfigurationError,
    DeepSeekProviderError,
    DeepSeekSettings,
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
    WorkspaceSettingsPatch,
)
from .services.access import (
    _check_assignee,
    _dataset_version_for,
    _problem_for,
    _task_for_project,
    membership,
    project_for,
    workspace_for_user,
)
from .services.ai_stages import (
    _ai_interpret_context,
    _ai_interpret_version,
    _deepseek_answer,
    _narration_context,
    _normalize_ai_interpret_evidence,
    _run_ai_stage,
)
from .services.analysis_pipeline import (
    _analysis_config_validation,
    _analysis_request_config,
    _field_mapping_error_message,
    _prepare_analysis_run,
)
from .services.audit import audit, audit_user_workspaces
from .services.auto_report import (
    _auto_report_payload,
    _compute_report_aggregates_batch,
    _deterministic_report_parts,
    _latest_project_versions,
    _report_markdown,
)
from .services.datasets import (
    _apply_cleaning,
    _cleaning_operation_parameters,
    _cleaning_operation_rows,
    _json_records,
    _normalise_cleaning_operations,
    _read_dataframe,
    _reject_unsupported_upload,
    _safe_name,
    _version_payload,
)
from .services.documents import _document_payload, _render_document_markdown
from .services.evidence import (
    _check_evidence_scope,
    _linked_resource_scope,
    _require_confirmed_insight_refs,
    _require_nonempty_evidence,
    _validate_source_insights,
)
from .services.job_handlers import _feedback_payload, _job, _job_payload, _register_job_handlers, job_executor
from .services.workspace_settings import (
    _merge_workspace_settings,
    _reject_ai_budget,
    _token_count,
    _workspace_ai_budget,
    _workspace_settings,
    _workspace_token_usage,
)

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
        system_prompt=(
            "你是产品分析助手。只根据给定的洞察证据，把观察归纳成一个清晰的产品问题草稿，不要猜测原始数据。"
            "title 是一句可验证的问题标题；statement 说明谁在什么场景遇到什么障碍、造成什么后果；"
            "impact_scope 说明影响范围与量级；priority 从 P0/P1/P2/P3 中选；limitations 写出该判断的局限。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
        response_schema=PROBLEM_DRAFT_SCHEMA,
        output_validator=validate_problem_draft,
        empty_output={"title": "", "statement": "", "impact_scope": "", "priority": "P2", "limitations": ["AI provider is not configured."]},
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
        system_prompt=(
            f"你是产品方案助手。针对给定的产品问题，提出 {body.option_count} 个互不重复的候选方案，"
            "每个方案输出 title（方案名称）、approach（具体做法）、pros（优点列表）、cons（缺点或代价列表）、"
            "effort（工作量，只能是 S/M/L）。不要重复已有方案。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
        response_schema=SOLUTION_DRAFTS_SCHEMA,
        output_validator=validate_solution_drafts,
        empty_output={"options": [], "limitations": ["AI provider is not configured."]},
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
    # The conversation is grounded in the insights the user already adopted.
    # They are loaded server-side from the session's project -- never from the
    # request body -- so a caller cannot choose which rows cross the boundary.
    insights_payload: list[dict[str, Any]] = []
    if session.project_id:
        confirmed_insights = db.scalars(
            select(Insight)
            .where(Insight.project_id == session.project_id, Insight.status == "confirmed")
            .order_by(Insight.created_at.desc())
            .limit(20)
        ).all()
        insights_payload = extract_ai_insights(confirmed_insights)
    copilot_context = {**safe_context, "project_id": session.project_id, "workspace_id": session.workspace_id, "user_id": user.id, "insights": insights_payload}
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
