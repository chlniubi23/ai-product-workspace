from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai_context import (
    AI_OUTPUT_SCHEMA,
    PROBLEM_DRAFT_SCHEMA,
    REPORT_OUTPUT_SCHEMA,
    SOLUTION_DRAFTS_SCHEMA,
    AIOutputValidationError,
    assert_safe_ai_context,
    build_ai_context,
    empty_ai_output,
    validate_ai_output,
    validate_problem_draft,
    validate_report_output,
    validate_solution_drafts,
)
from ..auth import get_current_user
from ..common import error, ok, page_params
from ..config import settings
from ..db import get_db
from ..infrastructure.llm.deepseek import (
    AiRequestMetadata,
    AnalysisPlanError,
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
    SolutionOption,
    User,
    Workspace,
    WorkspaceMember,
    now,
)
from ..schemas import AIFrameProblemRequest, AIInterpretRequest, AIProposeSolutionsRequest
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
    _report_markdown,
)
from ..services.evidence import _validate_source_insights
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
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
        response_schema=PROBLEM_DRAFT_SCHEMA,
        output_validator=validate_problem_draft,
        empty_output={"title": "", "statement": "", "impact_scope": "", "priority": "P2", "limitations": ["AI provider is not configured."]},
    )
    return ok({**result, "provider": "deepseek", "draft": True, "source_insight_ids": insight_ids})


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


@router.post("/api/v1/projects/{project_id}/auto-report")
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
    return ok([_auto_report_payload(row) for row in rows], page=page, page_size=page_size, total=total)


@router.get("/api/v1/auto-reports/{report_id}")
def get_auto_report(report_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(AutoAnalysisReport, report_id)
    if report is None:
        raise error("NOT_FOUND", "Report not found", 404)
    project_for(db, user, report.project_id)
    return ok(_auto_report_payload(report))


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
    return ok(_auto_report_payload(report))


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
