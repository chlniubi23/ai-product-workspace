from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai_context import (
    AI_OUTPUT_SCHEMA,
    DECISION_DRAFT_SCHEMA,
    PROBLEM_DRAFT_SCHEMA,
    SOLUTION_DRAFTS_SCHEMA,
    AIOutputValidationError,
    empty_ai_output,
    validate_ai_output,
    validate_decision_draft,
    validate_problem_draft,
    validate_solution_drafts,
)
from ..analytics.digest import build_findings_digest
from ..auth import get_current_user
from ..common import error, ok, page_params
from ..config import settings
from ..db import get_db
from ..infrastructure.llm.deepseek import (
    AiRequestMetadata,
    ChatMessage,
    DeepSeekAdapter,
    DeepSeekConfigurationError,
    DeepSeekProviderError,
    DeepSeekSettings,
)
from ..models import (
    AIRun,
    AnalysisArtifact,
    AnalysisRun,
    AutoAnalysisReport,
    Insight,
    Project,
    SolutionOption,
    User,
    Workspace,
    WorkspaceMember,
    now,
)
from ..schemas import AIDraftDecisionRequest, AIFrameProblemRequest, AIInterpretRequest, AIProposeSolutionsRequest
from ..services.access import _dataset_version_for, _problem_for, membership, project_for
from ..services.ai_stages import (
    _ai_interpret_context,
    _ai_interpret_version,
    _narration_context,
    _normalize_ai_interpret_evidence,
    _run_ai_stage,
)
from ..services.audit import audit
from ..services.auto_report import (
    _auto_report_payload,
    _compute_report_aggregates_batch,
    _deterministic_report_parts,
    _latest_project_versions,
    _narrate_report,
    _report_markdown,
)
from ..services.evidence import _validate_source_insights
from ..services.job_handlers import _active_narration_job, _job, _job_payload, job_executor
from ..services.workspace_settings import (
    _reject_ai_budget,
    _token_count,
    _workspace_ai_budget,
    _workspace_settings,
    _workspace_token_usage,
)

router = APIRouter()




@router.post("/api/v1/ai/interpret")
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
                        "你是产品分析助手。只根据给定的聚合证据回答，不要猜测原始数据。返回 JSON，必须包含 facts、hypotheses、recommendations、limitations；每条事实、假设和建议都必须有 evidence 数组。Schema: "
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


@router.post("/api/v1/ai/frame-problem")
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
            "used_insight_ids 必须标注你的陈述实际依据了哪些洞察 id（只能从给定洞察中选）。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
        response_schema=PROBLEM_DRAFT_SCHEMA,
        output_validator=validate_problem_draft,
        empty_output={"title": "", "statement": "", "impact_scope": "", "priority": "P2", "limitations": ["AI provider is not configured."], "used_insight_ids": []},
    )
    # Batch 19: keep the evidence chain unbroken -- the route's answer carries
    # only the intersection of what the AI cited and the caller's validated
    # insight ids.  An empty/hallucinated citation falls back to ALL caller
    # ids, so a saved problem never loses its grounding.
    used_ids = [item for item in (result.get("output") or {}).get("used_insight_ids") or [] if item in insight_ids]
    grounded_ids = used_ids or insight_ids
    return ok({**result, "provider": "deepseek", "draft": True, "source_insight_ids": insight_ids, "used_insight_ids": grounded_ids})


@router.post("/api/v1/dataset-versions/{version_id}/report-narration")
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


def _persist_report_findings(
    db: Session, project: Project, snapshots: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> None:
    """Land the digest as real ``finding`` artifacts so the grounding chain can
    cite actual ids (batch 13).

    Each dataset's findings attach to that dataset's latest succeeded analysis
    run.  Idempotent: the target run's existing finding artifacts are deleted
    first, so recomputing a report never stacks duplicates.  A dataset without
    a succeeded run keeps its findings in the report digest but gets no
    artifacts.  NOTE: re-running an analysis clears that run's artifacts
    (findings included) -- regenerate the report to restore them.
    """

    for snapshot in snapshots:
        run = db.scalar(
            select(AnalysisRun)
            .where(
                AnalysisRun.project_id == project.id,
                AnalysisRun.dataset_version_id == snapshot["version_id"],
                AnalysisRun.status == "succeeded",
            )
            .order_by(AnalysisRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            continue
        dataset_findings = [item for item in findings if item.get("dataset") == snapshot["dataset_name"]]
        stale = db.scalars(
            select(AnalysisArtifact).where(
                AnalysisArtifact.analysis_run_id == run.id,
                AnalysisArtifact.artifact_type == "finding",
            )
        ).all()
        for artifact in stale:
            db.delete(artifact)
        db.flush()
        for item in dataset_findings:
            payload = {
                # Batch 17b: the digest index makes read-back order equal the
                # digest order -- created_at ties are unreliable and UUID ids
                # do not follow insertion order.
                "order": findings.index(item),
                "kind": str(item.get("kind") or ""),
                "dataset": str(item.get("dataset") or ""),
                "columns": [str(column) for column in item.get("columns") or []],
                "value": item.get("value"),
                "rate": item.get("value"),
            }
            db.add(
                AnalysisArtifact(
                    analysis_run_id=run.id,
                    artifact_type="finding",
                    title=str(item.get("statement") or "")[:255],
                    payload_json=payload,
                    fingerprint=hashlib.sha256(
                        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                )
            )


async def _compute_auto_report(project: Project, user: User, db: Session) -> AutoAnalysisReport:
    """Deterministic half of the auto report (batch 10): pandas aggregates
    plus the fallback body, persisted as ``not_configured``.  No AI call
    happens here -- the numbers are on screen in seconds, zero tokens spent."""

    versions = _latest_project_versions(db, project)
    if not versions:
        raise error("VALIDATION_ERROR", "No parsed dataset versions in this project; upload data first", 400)

    # Collect plain-data snapshots in this thread, then read files and compute
    # aggregates in a worker thread so the event loop is never blocked.
    snapshots: list[dict[str, Any]] = []
    for version in versions:
        quality = version.quality_report
        quality_summary = quality.summary_json if quality is not None and isinstance(quality.summary_json, dict) else {}
        schema_json = version.schema_json if isinstance(version.schema_json, dict) else {}
        # 批 32：解析凭证的标量摘要随快照进入聚合与报告概况；明细只存
        # schema_json 不出站。旧版本没有 manifest 时为 None，键缺席。
        manifest = schema_json.get("parse_manifest")
        manifest_summary: dict[str, Any] | None = None
        if isinstance(manifest, dict):
            warnings = manifest.get("warnings")
            manifest_summary = {
                "parse_encoding": str(manifest.get("encoding") or ""),
                "parse_rows": int(manifest.get("rows") or 0),
                "parse_warnings_count": len(warnings) if isinstance(warnings, list) else 0,
            }
        snapshots.append(
            {
                "version_id": version.id,
                "dataset_name": version.dataset.name,
                "version_number": version.version_number,
                "storage_path": version.storage_path,
                "file_name": version.file_name,
                "row_count": version.row_count,
                "column_count": version.column_count,
                # Batch 21: the field-semantics dictionary rides along so the
                # report AI reasons about named business fields, not raw names.
                "dataset_label": str(schema_json.get("dataset_label") or ""),
                "columns": [
                    {
                        "name": column.name,
                        "type": column.confirmed_type or column.inferred_type,
                        "label": column.semantic_label,
                        "description": column.semantic_description,
                    }
                    for column in version.columns
                ],
                "quality_score": quality.overall_score if quality is not None else None,
                "quality_status": quality.status if quality is not None else None,
                "missing_values": quality_summary.get("missing_values"),
                "anomalies": quality_summary.get("anomalies"),
                "parse_manifest_summary": manifest_summary,
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

    findings_digest = build_findings_digest(aggregates)
    _persist_report_findings(db, project, snapshots, findings_digest)
    # Batch 15: one live report per project.  The new report is created first
    # (flushed for its id below) and every predecessor is deleted afterwards,
    # so the project always holds at least one report inside the transaction.
    # Confirmation history survives in audit_logs only.
    coverage = {
        "included": len(aggregates),
        "total": len(snapshots),
        "omitted": [str(item.get("name") or "") for item in read_failures],
    }
    default_title, deterministic_summary, deterministic_sections, deterministic_findings = _deterministic_report_parts(
        project.name, aggregates, findings_digest, coverage
    )
    deterministic_limitations = ["分析维度由系统按列类型自动选择；相关性不代表因果。"]
    report = AutoAnalysisReport(
        workspace_id=project.workspace_id,
        project_id=project.id,
        title=default_title,
        status="not_configured",
        summary=deterministic_summary,
        sections_json=deterministic_sections,
        key_findings=deterministic_findings,
        recommendations=[],
        limitations=list(deterministic_limitations),
        dataset_version_ids=[item["dataset_version_id"] for item in aggregates],
        deterministic_json={
            "datasets": aggregates,
            "read_failures": read_failures,
            "findings": findings_digest,
            "coverage": coverage,
        },
        content_markdown=_report_markdown(default_title, deterministic_summary, deterministic_sections, deterministic_findings, [], deterministic_limitations),
        generated_by=user.id,
    )
    db.add(report)
    db.flush()
    # New report is flushed and safe: drop every predecessor (confirmed or
    # not) so the project keeps exactly one report.  Runs/audit rows are not
    # touched -- the confirmation history lives in audit_logs.
    for predecessor in db.scalars(
        select(AutoAnalysisReport).where(
            AutoAnalysisReport.project_id == project.id,
            AutoAnalysisReport.id != report.id,
        )
    ).all():
        db.delete(predecessor)
    db.flush()
    return report


@router.post("/api/v1/projects/{project_id}/auto-report/compute")
async def compute_auto_report(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Instant deterministic half of the auto report (batch 10).

    Aggregates the latest version of every dataset in the project, persists a
    ``not_configured`` report and returns immediately: zero AI calls, zero
    tokens.  The AI interpretation is a separate step
    (``POST /auto-reports/{id}/narrate``) so the browser renders the numbers
    while narration runs as a background job.
    """

    project = project_for(db, user, project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    report = await _compute_auto_report(project, user, db)
    audit(db, workspace.id, user.id, "report.computed", "auto_report", report.id, {"datasets": len(report.dataset_version_ids)})
    db.commit()
    return ok({"report": _auto_report_payload(report, db), "status": report.status, "error_code": report.error_code})


@router.post("/api/v1/auto-reports/{report_id}/narrate")
def narrate_auto_report(report_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue the AI narration for an existing report (batch 10).

    Runs as an ``auto_report_narration`` job, so the browser stays free while
    the provider call runs; the outcome is visible through the normal report
    endpoints.  Narration fills the interpretation gap -- a report that
    already carries AI prose (``succeeded``/``confirmed``) is rejected instead
    of silently rewritten, and a second narrate while one is in flight is
    rejected to avoid double spend.
    """

    report = db.get(AutoAnalysisReport, report_id)
    if report is None:
        raise error("NOT_FOUND", "Report not found", 404)
    project_for(db, user, report.project_id, "editor")
    if report.status in {"succeeded", "confirmed"}:
        raise error("REPORT_ALREADY_NARRATED", "该报告已有 AI 解读；如需更新请重新生成报告", 409)
    if _active_narration_job(db, report.id) is not None:
        raise error("NARRATION_IN_PROGRESS", "AI 解读正在生成中，请稍候", 409)
    job = _job(db, report.workspace_id, "auto_report_narration", {"report_id": report.id, "_actor_id": user.id}, result_type="auto_report", result_id=report.id)
    audit(db, report.workspace_id, user.id, "report.narration_queued", "auto_report", report.id)
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"report": _auto_report_payload(report, db), "job": _job_payload(job)})


@router.post("/api/v1/projects/{project_id}/auto-report")
async def generate_auto_report(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Backward-compatible combined flow: compute, then narrate in one request.

    Batch 10 split this endpoint into ``/compute`` (instant, deterministic)
    and ``/auto-reports/{id}/narrate`` (job-based AI); this path keeps both
    steps serialised for existing clients.  Status semantics are unchanged --
    ``succeeded`` means validated AI prose, ``not_configured`` means the
    deterministic report only.  One deliberate shift: a budget-valve rejection
    (429) now leaves the compute-only report behind instead of nothing, since
    the aggregates cost nothing and stay usable.
    """

    project = project_for(db, user, project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    report = await _compute_auto_report(project, user, db)
    audit(db, workspace.id, user.id, "report.computed", "auto_report", report.id, {"datasets": len(report.dataset_version_ids)})
    db.commit()
    result = await _narrate_report(db, user, workspace, project, report)
    usage = result.get("usage") or {}
    return ok(
        {
            "report": _auto_report_payload(report, db),
            "run_id": report.ai_run_id,
            "status": report.status,
            "error_code": report.error_code,
            "usage": {"prompt_tokens": usage.get("prompt_tokens") or 0, "completion_tokens": usage.get("completion_tokens") or 0},
        }
    )


@router.get("/api/v1/projects/{project_id}/auto-reports")
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
    return ok([_auto_report_payload(row, db) for row in rows], page=page, page_size=page_size, total=total)


@router.get("/api/v1/auto-reports/{report_id}")
def get_auto_report(report_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(AutoAnalysisReport, report_id)
    if report is None:
        raise error("NOT_FOUND", "Report not found", 404)
    project_for(db, user, report.project_id)
    return ok(_auto_report_payload(report, db))


@router.post("/api/v1/auto-reports/{report_id}/confirm")
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
    return ok(_auto_report_payload(report, db))


@router.post("/api/v1/ai/propose-solutions")
async def ai_propose_solutions(body: AIProposeSolutionsRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stage 10: draft candidate solution options for a confirmed problem."""

    problem = _problem_for(db, user, body.problem_id, "editor")
    workspace = db.get(Workspace, problem.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    existing = db.scalars(select(SolutionOption).where(SolutionOption.problem_id == problem.id)).all()
    context = {
        "problem": {"id": problem.id, "title": problem.title, "statement": problem.statement, "impact_scope": problem.impact_scope, "priority": problem.priority},
        "existing_options": [{"id": row.id, "title": row.title, "approach": row.approach} for row in existing],
    }
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="propose_solutions",
        system_prompt=(
            "你是产品方案助手。针对给定的产品问题，按问题复杂度提出 2-4 个互不重复的候选方案（宁少勿凑），"
            "每个方案输出 title（方案名称）、approach（具体做法）、pros（优点列表）、cons（缺点或代价列表）、"
            "effort（工作量，只能是 S/M/L）。不要重复已有方案。"
            "其中恰好一个是你综合可行性、成本与风险后最推荐的：该方案 recommended=true 且给出 recommendation_reason（一句话），其余方案 recommended=false。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
        response_schema=SOLUTION_DRAFTS_SCHEMA,
        output_validator=validate_solution_drafts,
        empty_output={"options": [], "limitations": ["AI provider is not configured."]},
    )
    return ok({**result, "provider": "deepseek", "draft": True, "problem_id": problem.id})


@router.post("/api/v1/ai/draft-decision")
async def ai_draft_decision(body: AIDraftDecisionRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stage 10: draft the decision proposal from the selected solution (batch 19).

    Pure draft endpoint -- nothing is persisted; the client fills its form
    from ``output`` and posts back through the regular decision-proposal +
    approval flow.
    """

    problem = _problem_for(db, user, body.problem_id, "editor")
    project = db.get(Project, problem.project_id)
    workspace = db.get(Workspace, problem.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    solutions = db.scalars(select(SolutionOption).where(SolutionOption.problem_id == problem.id)).all()
    selected = next((item for item in solutions if item.status == "selected"), None)
    if selected is None:
        raise error("SOLUTION_NOT_SELECTED", "请先在方案讨论中选定一个方案，再起草决策提案", 422)
    rejected = [
        {"title": item.title, "reject_reason": item.reject_reason}
        for item in solutions
        if item.status == "rejected"
    ]
    insights = db.scalars(
        select(Insight)
        .where(Insight.project_id == project.id, Insight.status == "confirmed")
        .order_by(Insight.created_at.desc())
        .limit(20)
    ).all()
    report = db.scalar(
        select(AutoAnalysisReport).where(AutoAnalysisReport.project_id == project.id).order_by(AutoAnalysisReport.created_at.desc()).limit(1)
    )
    findings = []
    if report is not None and isinstance(report.deterministic_json, dict):
        findings = [item for item in report.deterministic_json.get("findings") or [] if isinstance(item, dict)][:12]

    context = {
        "problem": {
            "id": problem.id,
            "title": problem.title,
            "statement": problem.statement,
            "impact_scope": problem.impact_scope,
            "priority": problem.priority,
        },
        "selected_solution": {
            "title": selected.title,
            "approach": selected.approach,
            "pros": list(selected.pros or []),
            "cons": list(selected.cons or []),
            "effort": selected.effort,
        },
        "rejected_solutions": rejected,
        "confirmed_insights": [
            {"title": row.title, "content": row.content, "confidence": row.confidence, "evidence": row.evidence_json}
            for row in insights
        ],
        "report_findings": findings,
    }
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="draft_decision",
        system_prompt=(
            "你是产品负责人，把选定方案写成正式的决策提案草稿。"
            "title 概括本次决策；problem_statement 沿用问题的陈述并补上数据佐证（引用给定洞察/发现中的数字）；"
            "proposed_action 按选定方案的具体做法展开为可执行的行动计划；expected_impact 尽量量化（给出指标与预期幅度）；"
            "risk_summary 写清主要风险与依赖；validation_plan 必须结合数据本身给出可执行路径——"
            "用哪个数据版本或指标、观察什么变化、什么周期内达到什么幅度算达标——不得写空话。"
            "所有内容必须来自给定上下文，禁止编造数据；全文使用简体中文。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
        response_schema=DECISION_DRAFT_SCHEMA,
        output_validator=validate_decision_draft,
        empty_output={
            "title": "",
            "problem_statement": "",
            "proposed_action": "",
            "expected_impact": "",
            "risk_summary": "",
            "validation_plan": "",
        },
    )
    return ok({**result, "provider": "deepseek", "draft": True, "problem_id": problem.id, "solution_id": selected.id})


@router.get("/api/v1/ai/usage")
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
