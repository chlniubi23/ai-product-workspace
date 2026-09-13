from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import (
    _FEEDBACK_CONTENT_KEYS,
    AI_OUTPUT_SCHEMA,
    AIOutputValidationError,
    assert_safe_ai_context,
    build_ai_context,
    empty_ai_output,
    validate_ai_output,
)
from ..common import error, model_dict
from ..config import settings
from ..infrastructure.llm.deepseek import (
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
from ..models import (
    AIRun,
    AnalysisArtifact,
    AnalysisRun,
    DataQualityReport,
    Dataset,
    DatasetVersion,
    FeedbackItem,
    FeedbackNote,
    MetricDefinition,
    Project,
    Task,
    User,
    Workspace,
)
from ..schemas import AIInterpretRequest
from ..services.access import _dataset_version_for, project_for
from ..services.audit import audit
from .datasets import _read_dataframe, _version_payload
from .workspace_settings import (
    _reject_ai_budget,
    _token_count,
    _workspace_ai_budget,
    _workspace_settings,
    _workspace_token_usage,
)

# Hard ceiling on any single provider call's output, in tokens.  Deliberately
# a module constant, not a setting: "not unlimited" must not be configurable
# away.  Normal calls stay at the workspace-derived max_output (default 4096);
# the delivery document raises to 8192 and a truncation retry may reach this
# cap, never beyond it.
HARD_OUTPUT_CAP = 16384


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
            insights=context.get("insights") or None,
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


# Single source of truth for free-text feedback keys is the AI-context
# firewall (``_FEEDBACK_CONTENT_KEYS``); this request-side scrubber extends it
# with ``sample``/``samples`` and must never be narrower than the firewall.
_FEEDBACK_CONTEXT_KEYS = _FEEDBACK_CONTENT_KEYS | {"sample", "samples"}


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
    # "insights" is a server-side context key (the Copilot route loads confirmed
    # insights itself).  A client-supplied list was always dropped on this path
    # and must stay dropped: its entries carry a "content" field, which the
    # allowlist re-check below correctly treats as feedback text.
    if isinstance(safe_request_context, dict):
        safe_request_context.pop("insights", None)
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
    # Phase 1: EDA now carries the enhanced correlation statistics (p values,
    # robust estimates) under ``correlation_pairs_detail``.  Each entry is a
    # variable pair plus scalars -- no row data -- so it maps onto the
    # ``pairs`` aggregate key and survives the firewall.
    "correlation_pairs_detail": "pairs",
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
    response_schema: dict[str, Any] | None = None,
    output_validator: Callable[[Any], dict[str, Any]] | None = None,
    empty_output: dict[str, Any] | None = None,
    min_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Shared draft-generating AI call for the stage 9/10 endpoints.

    Budget model (batch 8): spend-then-account.  The only pre-call check is
    the workspace daily valve (``ai_daily_token_budget``) projected with a
    conservative worst case; the call itself is never throttled by
    ``ai_per_request_token_budget`` and a completed result is never discarded
    for budget reasons -- after the call we only record usage.  Output size is
    bounded by ``HARD_OUTPUT_CAP`` instead.  Callers may swap the default
    four-section contract for a stage-specific one via
    ``response_schema``/``output_validator``/``empty_output``.
    ``min_output_tokens`` raises the first attempt's token ceiling (still
    capped by ``HARD_OUTPUT_CAP``) for callers whose deliverable is inherently
    long -- the delivery document is the one such stage.
    """

    schema = response_schema or AI_OUTPUT_SCHEMA

    def fallback_output(summary: str, limitation: str) -> dict[str, Any]:
        return dict(empty_output) if empty_output is not None else empty_ai_output(summary=summary, limitation=limitation)

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

    # ---- total valve: the only pre-call budget check (batch 8) ----
    # A conservative prompt estimate plus the hard-capped output ceiling; if
    # the daily budget cannot cover the worst case, refuse before any provider
    # spend with actionable guidance.  This is the ONLY site that can produce
    # AI_BUDGET_EXCEEDED in this module -- after the provider call we only
    # account for usage, never reject.
    desired_output = max(budget["max_output"], min_output_tokens or 0)
    max_tokens = min(desired_output, HARD_OUTPUT_CAP)
    context_json_len = len(json.dumps(context, ensure_ascii=False, default=str))
    est_prompt = min(12000, max(1000, context_json_len // 2))
    worst_case = est_prompt + max_tokens
    reserved_daily = _workspace_token_usage(db, workspace, budget["per_request"])
    if reserved_daily + worst_case > budget["daily"]:
        details = {
            "daily_remaining": max(0, budget["daily"] - reserved_daily),
            "needed_tokens": worst_case,
            "hint": f"今日 AI 额度剩余不足（本次预计约 {worst_case} tokens），请到设置调大「每日 token 预算」或明天再试",
        }
        _reject_ai_budget(
            db,
            workspace,
            user,
            ai_run,
            budget,
            daily_used=reserved_daily,
            reason="今日 AI 额度剩余不足，请到设置调大「每日 token 预算」或明天再试",
            extra_details=details,
        )
    # Record the worst case so concurrent reservations can account for this
    # in-flight call (see _workspace_token_usage).
    ai_run.input_summary_json = {**(ai_run.input_summary_json or {}), "budget": {"worst_case": worst_case, "max_tokens": max_tokens}}
    db.commit()

    structured = fallback_output("AI 当前不可用，请手动填写。", "AI provider is not configured.")
    result_status = "not_configured"
    error_code: str | None = None
    provider_request_id: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    started = time.perf_counter()
    adapter = DeepSeekAdapter(DeepSeekSettings.from_app_settings(settings))
    if adapter.configured:
        try:
            json_instruction = (
                " 返回 JSON，必须包含 facts、hypotheses、recommendations、limitations；每条都必须有 evidence 数组。输出默认是 draft。Schema: "
                if response_schema is None
                else " 返回 JSON，输出默认是 draft。Schema: "
            )
            system_message = system_prompt + json_instruction + json.dumps(schema, ensure_ascii=True, separators=(",", ":"))
            user_message = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)

            # One attempt, then a single retry with a doubled token ceiling
            # when the output came back truncated or unparseable.  The retry
            # passes the daily valve only (projected with the observed prompt
            # tokens); if it cannot fit, the already-paid first attempt is
            # kept and the run fails honestly -- results are never discarded
            # for budget reasons, and the retry itself is never force-spent.
            finish_reason: str | None = None
            retried = False
            truncated = False
            attempt = 0
            while attempt < 2:
                attempt += 1
                if attempt == 2:
                    retry_tokens = min(desired_output * 2, HARD_OUTPUT_CAP)
                    projected = _workspace_token_usage(db, workspace, budget["per_request"]) + prompt_tokens + retry_tokens
                    if projected > budget["daily"]:
                        break
                    retried = True
                    max_tokens = retry_tokens
                    system_message += "\n只输出符合 Schema 的 JSON 对象，禁止任何截断或额外文字。"
                result = await adapter.complete(
                    messages=[
                        ChatMessage("system", system_message),
                        ChatMessage("user", user_message),
                    ],
                    response_schema=schema,
                    request_metadata=AiRequestMetadata(
                        workspace_id=workspace.id,
                        user_id=user.id,
                        feature_name=feature_name,
                        request_fingerprint=request_fingerprint,
                        max_tokens=max_tokens,
                    ),
                )
                finish_reason = result.finish_reason
                prompt_tokens += _token_count(result.prompt_tokens)
                completion_tokens += _token_count(result.completion_tokens)
                provider_request_id = result.provider_request_id

                raw = result.structured
                if raw is None and result.content:
                    try:
                        raw = json.loads(result.content)
                    except (TypeError, json.JSONDecodeError):
                        raw = None
                truncated = (result.finish_reason or "") == "length"
                if raw is not None and not truncated:
                    structured = output_validator(raw) if output_validator else validate_ai_output(raw)
                    result_status = "succeeded"
                    break

            if result_status != "succeeded":
                result_status = "failed"
                if truncated:
                    error_code = "LLM_TRUNCATED"
                    structured = fallback_output("AI 输出过长被截断。", "AI 输出过长被截断")
                else:
                    error_code = "INVALID_AI_OUTPUT"
                    structured = fallback_output("模型未返回结构化结果。", "Provider response was not structured JSON.")
            run_metadata: dict[str, Any] = {"finish_reason": finish_reason, "retried": retried}
        except DeepSeekConfigurationError:
            error_code = "LLM_NOT_CONFIGURED"
            run_metadata = {}
        except AIOutputValidationError:
            result_status = "failed"
            error_code = "INVALID_AI_OUTPUT"
            structured = fallback_output("模型返回格式无法验证。", "Provider response failed the structured output contract.")
            run_metadata = {"retried": False}
        except DeepSeekProviderError:
            result_status = "failed"
            error_code = "LLM_PROVIDER_ERROR"
            structured = empty_ai_output(summary="模型服务暂时不可用。", limitation="Provider request failed; retry later or enter a manual draft.")
            run_metadata = {}
        except Exception:
            result_status = "failed"
            error_code = "LLM_ERROR"
            structured = empty_ai_output(summary="AI 暂时不可用。", limitation="Unexpected provider failure; enter a manual draft.")
            run_metadata = {}
    else:
        error_code = "LLM_NOT_CONFIGURED"
        run_metadata = {}

    # Spend-then-account: after the call only bookkeeping happens.  A
    # completed result is never discarded for budget reasons.
    observed_tokens = prompt_tokens + completion_tokens
    ai_run = db.get(AIRun, ai_run.id) or ai_run
    ai_run.status = result_status
    ai_run.prompt_tokens = prompt_tokens or None
    ai_run.completion_tokens = completion_tokens or None
    ai_run.latency_ms = int((time.perf_counter() - started) * 1000)
    ai_run.error_code = error_code
    ai_run.output_reference = ai_run.id
    ai_run.input_summary_json = {
        "context": context,
        "draft": True,
        "structured_output": structured,
        "provider_request_id": provider_request_id,
        "provider_meta": run_metadata,
    }
    audit(db, workspace.id, user.id, f"ai.{feature_name}", "ai_run", ai_run.id, {"status": result_status})
    db.commit()
    return {
        "run_id": ai_run.id,
        "status": result_status,
        "output": structured,
        "error_code": error_code,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": observed_tokens},
    }
