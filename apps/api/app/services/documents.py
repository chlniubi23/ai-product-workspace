from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import build_ai_context
from ..common import error, model_dict, serialize
from ..models import (
    AnalysisArtifact,
    AnalysisRun,
    AutoAnalysisReport,
    DecisionProposal,
    Document,
    DocumentVersion,
    FeedbackCluster,
    Insight,
    InterviewQuestion,
    ProductProblem,
    Project,
    SolutionOption,
    User,
    now,
)
from ..schemas import DocumentGenerate
from ..services.access import project_for
from .evidence import _require_confirmed_insight_refs

# Caps for the AI-bound document context. The delivery document is the one
# stage that deserves a large budget, but the context itself stays bounded so
# a project with hundreds of rows cannot price the call out of usefulness.
_DOC_INSIGHT_LIMIT = 20
_DOC_INTERVIEW_LIMIT = 30
_DOC_DECISION_LIMIT = 10
_DOC_DATASET_SUMMARY_LIMIT = 5
_DOC_FINDING_LIMIT = 12

_DOCUMENT_SECTION_BRIEFS = {
    "weekly_report": "章节结构：本期概览、关键变化、核心问题与反馈、已完成工作、下期计划",
    "prd": "章节结构：需求背景、目标与非目标、用户与场景、功能范围、用户流程、数据与埋点、验收标准",
    "retrospective": "章节结构：背景、事实与结果、根因假设、决策与改进、跟进事项",
}


# Batch 17: per-type section plans for the two-pass outline.  prd is the
# deep product document; weekly/retrospective keep their editorial shapes.
_DOCUMENT_TYPE_SECTIONS: dict[str, list[str]] = {
    "prd": [
        "需求背景与数据发现",
        "根因判断",
        "目标与非目标",
        "用户与场景",
        "功能范围与优先级",
        "用户流程",
        "数据与埋点",
        "验收标准",
        "风险与缓解",
        "迭代规划",
    ],
    "weekly_report": ["本期概览", "关键变化", "核心问题与反馈", "已完成工作", "下期计划"],
    "retrospective": ["背景与结果", "根因分析", "决策与改进", "跟进事项"],
}


def _outline_system_prompt(document_type: str, audience: str) -> str:
    """Pass-1 prompt: findings, section plan and root cause (batch 17)."""

    plan = "、".join(_DOCUMENT_TYPE_SECTIONS.get(document_type, _DOCUMENT_TYPE_SECTIONS["prd"]))
    return (
        "你是资深产品文档架构师。基于给定的证据材料（洞察、采访回答、已批准决策、数据集聚合、数据侧重点发现）"
        "为一份产品文档产出大纲：findings 列出最多 6 条关键数据发现（id 用 finding-1 这样的序号，title 一句话并包含具体数字，"
        "severity 只能从 高/中/低 中选，evidence_hint 指明数据来源如 分析产物/采访/洞察）；"
        f"sections 按顺序给出本文档的章节计划（heading 与 purpose），{document_type} 文档必须依次覆盖：{plan}；"
        "root_cause 用 3-5 句话概括数据背后的根因判断，必须引用具体数字。"
        "篇幅约束：大纲输出整体保持在 2000 tokens 以内——findings 每条一句话，purpose 每节不超过 40 字，不要展开正文。"
        "所有内容必须来自给定上下文，禁止编造数据。输出面向读者：" f"{audience}。"
    )


def _section_system_prompt(
    document_type: str,
    title: str,
    index: int,
    total: int,
    heading: str,
    purpose: str,
    written_summary: str,
    audience: str,
    solution: dict[str, Any] | None,
    decision: dict[str, Any] | None,
) -> str:
    """Pass-2 prompt for one section (batch 17).

    The decision chain is the main narrative axis for a PRD: the approved
    decision and the selected solution are quoted directly so the section
    writes the product design around them instead of generic analysis.
    """

    axis = ""
    if decision:
        axis += (
            f"已批准决策（本文档的主叙事轴）：问题=「{decision.get('problem_statement') or ''}」，"
            f"行动=「{decision.get('proposed_action') or ''}」，验证=「{decision.get('validation_plan') or ''}」。"
        )
    if solution:
        axis += (
            f"选定方案：{solution.get('title') or ''}——{solution.get('approach') or ''}"
            f"（工作量 {solution.get('effort') or 'M'}）。"
        )
    if document_type == "prd":
        axis += "本文档是围绕已批准决策与选定方案的产品设计文档，数据发现是论据，功能设计是主体；禁止输出与决策无关的泛泛分析。"
    format_rules = (
        "格式要求：涉及发现清单用 Markdown 表格（|编号|发现|数据证据|严重程度|）；"
        "涉及目标用 Markdown 表格（|目标|衡量指标|目标值|）且每个目标必须量化；"
        "涉及验收标准用 Markdown 表格（|编号|验收点|预期结果|）；"
        "功能设计必须包含边界情况与异常兜底（badcase）小节；用户流程用「场景一/场景二…」编号叙述。"
        if document_type == "prd"
        else "格式要求：使用 Markdown 小标题与列表，涉及数据必须引用具体数字。"
    )
    depth = (
        # Batch 17 hotfix: a hard per-section budget keeps every first attempt
        # under the output ceiling (no truncation-retry) and lands the whole
        # document at ~10-15k chars -- the quality benchmark.
        "本节正文 800–1500 字；表格 cell 保持简洁；不要重复其他章节内容。写深写透但严格遵守篇幅上限。"
        if document_type == "prd"
        else "本节正文 400–800 字；不要重复其他章节内容。"
    )
    return (
        f"你负责撰写《{title}》的第 {index}/{total} 节「{heading}」。本节目的：{purpose or '按标题展开'}。"
        f"{axis}{format_rules}"
        "写作依据：给定的证据材料与 outline_findings；引用证据时标注来源（引用证据标题或 id）；禁止编造数据；"
        "全文使用简体中文，语气面向指定读者：" f"{audience}。"
        f"{depth}"
        + (
            f"已写前文摘要（保证连贯，不要重复）：{'；'.join(written_summary.splitlines())}"
            if written_summary
            else ""
        )
        + ' 只输出 JSON：{"heading": 节标题, "content": Markdown 正文}。'
    )


def _fallback_section_content(section: dict[str, Any], outline: dict[str, Any]) -> str:
    """Deterministic in-place content for a failed section call (batch 17).

    The section still appears in the document (outline bullets + findings) so
    one failed call never breaks the whole deliverable."""

    lines: list[str] = []
    purpose = str(section.get("purpose") or "").strip()
    lines.append(f"- 本节要点：{purpose}" if purpose else "- 本节要点：按大纲展开（本节自动生成失败，请人工补充）。")
    for item in outline.get("findings") or []:
        if isinstance(item, dict) and item.get("title"):
            lines.append(f"- 相关发现：{item['title']}（严重程度：{item.get('severity') or '中'}）")
    return chr(10).join(lines) if len(lines) > 1 else "（本节内容生成失败，请结合证据清单人工补充。）"


def _document_payload(document: Document, db: Session) -> dict[str, Any]:
    from .job_handlers import _active_document_generation_job  # in-function: job_handlers imports this module

    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number)).all()
    current = next((version for version in versions if version.id == document.current_version_id), versions[-1] if versions else None)
    payload = model_dict(document, {"current_version": model_dict(current) if current else None, "versions": [model_dict(version) for version in versions]})
    # Batch 16: in-flight generation job id (null once terminal) so the
    # delivery page can resume its poll after a page switch.
    active = _active_document_generation_job(db, document.id)
    payload["generation_job_id"] = active.id if active is not None else None
    return payload


def _collect_source_refs(body: DocumentGenerate, db: Session, project: Project) -> dict[str, Any]:
    """Validate source_refs and collect the deterministic-template evidence.

    Returns the safe evidence list, upstream dataset/run id sets (for the
    immutable manifest) and the human-readable evidence sections. Shared by
    both the deterministic fallback renderer and the AI context builder.
    """

    evidence: list[dict[str, Any]] = []
    dataset_version_ids: set[str] = set()
    analysis_run_ids: set[str] = set()
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
                evidence_sections.extend([f"### 洞察: {item.title}", "", item.content, ""])
        elif ref_type == "decision_proposal":
            item = db.get(DecisionProposal, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item:
                evidence_sections.extend([f"### 决策提案: {item.title}", "", f"- 问题: {item.problem_statement}", f"- 做法: {item.proposed_action}", f"- 验证: {item.validation_plan}", ""])
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
                evidence_sections.extend([f"### 分析产物: {item.title}", "", "```json", json.dumps(item.payload_json, ensure_ascii=False, indent=2, default=str), "```", ""])
        elif ref_type == "feedback_cluster":
            item = db.get(FeedbackCluster, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item:
                evidence_sections.extend([f"### 反馈主题: {item.name}", "", item.summary, ""])
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
    return {
        "evidence": evidence,
        "dataset_version_ids": dataset_version_ids,
        "analysis_run_ids": analysis_run_ids,
        "evidence_sections": evidence_sections,
    }


def _build_document_context(body: DocumentGenerate, db: Session, user: User) -> dict[str, Any]:
    """Assemble the grounded, firewall-safe context for AI document rendering.

    The artifacts channel carries six evidence classes: confirmed insights,
    answered interview questions, approved decisions, the selected solution
    (batch 17), the per-dataset aggregates of the latest auto-report and its
    landed finding artifacts.  Everything passes through ``build_ai_context``;
    no raw rows or storage paths ever leave.  ``solution``/``decision`` are
    additionally returned as top-level dicts for the two-pass section prompts
    (they never pass through the firewall themselves).
    """

    project = project_for(db, user, body.project_id)
    _require_confirmed_insight_refs(db, project.workspace_id, body.source_refs, project.id)
    collected = _collect_source_refs(body, db, project)

    artifacts: list[dict[str, Any]] = []

    insights = db.scalars(
        select(Insight)
        .where(Insight.project_id == project.id, Insight.status == "confirmed")
        .order_by(Insight.created_at.desc())
        .limit(_DOC_INSIGHT_LIMIT)
    ).all()
    for item in insights:
        artifacts.append(
            {
                "id": item.id,
                "artifact_type": "insight",
                "title": item.title,
                "payload_json": {"content": item.content, "confidence": item.confidence, "evidence": item.evidence_json},
            }
        )

    questions = db.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.project_id == project.id, InterviewQuestion.status == "answered")
        .order_by(InterviewQuestion.created_at)
        .limit(_DOC_INTERVIEW_LIMIT)
    ).all()
    for item in questions:
        artifacts.append(
            {
                "id": item.id,
                "artifact_type": "interview_answer",
                "title": item.topic or item.question_text[:120],
                "payload_json": {"question": item.question_text, "answer": item.answer_text},
            }
        )

    decisions = db.scalars(
        select(DecisionProposal)
        .where(DecisionProposal.project_id == project.id, DecisionProposal.status == "approved")
        .order_by(DecisionProposal.created_at.desc())
        .limit(_DOC_DECISION_LIMIT)
    ).all()
    for item in decisions:
        artifacts.append(
            {
                "id": item.id,
                "artifact_type": "decision",
                "title": item.title,
                "payload_json": {
                    "problem_statement": item.problem_statement,
                    "proposed_action": item.proposed_action,
                    "expected_impact": item.expected_impact,
                    "validation_plan": item.validation_plan,
                },
            }
        )

    # Batch 17: the decision chain is the PRD's narrative axis.  The selected
    # solution rides the artifacts channel AND the top-level keys, which the
    # per-section prompts quote directly (firewall untouched -- the artifacts
    # payloads stay within the aggregate sanitizer, the top-level dicts never
    # pass through build_ai_context).
    selected_solution = db.scalar(
        select(SolutionOption)
        .join(ProductProblem, ProductProblem.id == SolutionOption.problem_id)
        .where(ProductProblem.project_id == project.id, SolutionOption.status == "selected")
        .order_by(SolutionOption.created_at.desc())
        .limit(1)
    )
    solution_payload: dict[str, Any] | None = None
    if selected_solution is not None:
        solution_payload = {
            "title": selected_solution.title,
            "approach": selected_solution.approach,
            "pros": list(selected_solution.pros or []),
            "cons": list(selected_solution.cons or []),
            "effort": selected_solution.effort,
        }
        artifacts.append(
            {
                "id": selected_solution.id,
                "artifact_type": "solution",
                "title": selected_solution.title,
                "payload_json": {"approach": selected_solution.approach, "effort": selected_solution.effort},
            }
        )
    decision_payload: dict[str, Any] | None = (
        {
            "problem_statement": decisions[0].problem_statement,
            "proposed_action": decisions[0].proposed_action,
            "validation_plan": decisions[0].validation_plan,
            "expected_impact": decisions[0].expected_impact,
        }
        if decisions
        else None
    )

    report = db.scalar(
        select(AutoAnalysisReport)
        .where(AutoAnalysisReport.project_id == project.id)
        .order_by(AutoAnalysisReport.created_at.desc())
        .limit(1)
    )
    if report is not None:
        deterministic = report.deterministic_json if isinstance(report.deterministic_json, dict) else {}
        datasets = deterministic.get("datasets")
        if isinstance(datasets, list):
            for dataset in datasets[:_DOC_DATASET_SUMMARY_LIMIT]:
                if not isinstance(dataset, dict):
                    continue
                artifacts.append(
                    {
                        "id": str(dataset.get("dataset_version_id") or report.id),
                        "artifact_type": "dataset_summary",
                        "title": str(dataset.get("name") or "dataset"),
                        "payload_json": dataset,
                    }
                )
        # Batch 13: prefer the persisted finding artifacts -- real ids make the
        # document's citations traceable; the report's own digest is the
        # fallback when a project has no landed finding artifacts yet.
        finding_artifacts = db.scalars(
            select(AnalysisArtifact)
            .join(AnalysisRun, AnalysisRun.id == AnalysisArtifact.analysis_run_id)
            .where(
                AnalysisRun.project_id == project.id,
                AnalysisRun.status == "succeeded",
                AnalysisArtifact.artifact_type == "finding",
            )
            .order_by(AnalysisArtifact.created_at.asc())
            .limit(_DOC_FINDING_LIMIT)
        ).all()
        # Batch 17b: read-back order must equal digest order.  created_at ties
        # are unreliable, so artifacts persisted with a digest "order" key sort
        # by it; legacy rows without the key trail behind in stable time order.
        finding_artifacts = sorted(
            finding_artifacts,
            key=lambda item: (
                item.payload_json.get("order") if isinstance(item.payload_json, dict) and isinstance(item.payload_json.get("order"), int) else 10**9,
                item.created_at,
                item.id,
            ),
        )
        if finding_artifacts:
            for item in finding_artifacts:
                artifacts.append(
                    {
                        "id": item.id,
                        "artifact_type": "finding",
                        "title": item.title,
                        "payload_json": item.payload_json,
                    }
                )
        else:
            findings = deterministic.get("findings")
            if isinstance(findings, list):
                for index, item in enumerate(findings[:_DOC_FINDING_LIMIT], start=1):
                    if not isinstance(item, dict):
                        continue
                    artifacts.append(
                        {
                            "id": f"finding-{index}",
                            "artifact_type": "finding",
                            "title": str(item.get("statement") or "")[:200],
                            "payload_json": {
                                "kind": str(item.get("kind") or ""),
                                "dataset": str(item.get("dataset") or ""),
                                "severity": int(item.get("severity") or 1),
                                "rate": item.get("value"),
                                "metrics": [str(column) for column in item.get("columns") or []],
                            },
                        }
                    )

    options = body.template_options if isinstance(body.template_options, dict) else {}
    safe_context = build_ai_context(
        goal=project.goal_statement or "",
        artifacts=artifacts,
        question=f"请依据以上证据材料撰写一份{body.title}（文档类型 {body.document_type}）",
    )
    return {
        "project": project,
        "safe_context": safe_context,
        "options": options,
        "document_type": body.document_type,
        "title": body.title,
        "solution": solution_payload,
        "decision": decision_payload,
        **collected,
    }


def _evidence_manifest(generation_timestamp: str, dataset_version_ids: set[str], analysis_run_ids: set[str], evidence: list[dict[str, Any]]) -> list[str]:
    """Immutable provenance block shared by the AI and fallback renderers."""

    return [
        "## 证据溯源",
        "",
        f"- 生成时间: {generation_timestamp}",
        f"- 数据版本 ID: {', '.join(sorted(dataset_version_ids)) or '无'}",
        f"- 分析运行 ID: {', '.join(sorted(analysis_run_ids)) or '无'}",
        f"- 来源引用: {json.dumps(evidence, ensure_ascii=False, sort_keys=True)}",
        "",
    ]


def _document_system_prompt(document_type: str, audience: str) -> str:
    brief = _DOCUMENT_SECTION_BRIEFS.get(document_type, _DOCUMENT_SECTION_BRIEFS["prd"])
    format_rules = (
        "格式要求：发现清单用 Markdown 表格（|编号|发现|数据证据|严重程度|）；目标用 Markdown 表格（|目标|衡量指标|目标值|）"
        "且每个目标必须量化；验收标准用 Markdown 表格（|编号|验收点|预期结果|）；功能设计必须包含边界情况与异常兜底（badcase）小节；"
        "用户流程用「场景一/场景二…」编号叙述；每个目标必须有衡量指标与目标值。"
        if document_type == "prd"
        else "格式要求：使用 Markdown 小标题与列表，涉及数据必须引用具体数字。"
    )
    return (
        "你是产品交付文档撰写助手。基于给定的证据材料（洞察、采访回答、已批准决策、数据集聚合、数据侧重点发现）撰写文档。"
        "artifact_type 为 finding 的条目是规则从数据中提炼的重点，正文应覆盖这些要点。"
        f"{brief}。"
        f"{format_rules}"
        "所有论断必须来自给定上下文，并在内容中自然标注依据（引用证据标题或 id）；禁止编造数据；"
        "禁止出现英文模板句或占位文案；全文使用简体中文，语气面向指定读者。"
        f"输出面向读者：{audience}。"
    )


def _render_ai_document_markdown(body: DocumentGenerate, ai_output: dict[str, Any], context: dict[str, Any]) -> str:
    """Render the validated REPORT_OUTPUT_SCHEMA payload as Chinese Markdown."""

    generation_timestamp = serialize(now())
    sections: list[str] = [f"# {ai_output.get('title') or body.title}", ""]
    summary = str(ai_output.get("summary") or "").strip()
    if summary:
        sections.extend([f"> {summary}", ""])
    for section in ai_output.get("sections", []):
        heading = str(section.get("heading") or "").strip()
        content = str(section.get("content") or "").strip()
        if not heading and not content:
            continue
        sections.extend([f"## {heading or '章节'}", "", content, ""])
    findings = [str(item).strip() for item in ai_output.get("key_findings", []) if str(item).strip()]
    if findings:
        sections.extend(["## 关键发现", "", *[f"- {item}" for item in findings], ""])
    recommendations = [str(item).strip() for item in ai_output.get("recommendations", []) if str(item).strip()]
    if recommendations:
        sections.extend(["## 建议", "", *[f"- {item}" for item in recommendations], ""])
    limitations = [str(item).strip() for item in ai_output.get("limitations", []) if str(item).strip()]
    if limitations:
        sections.extend(["## 局限", "", *[f"- {item}" for item in limitations], ""])
    # The provenance manifest is deterministic and must never be overwritten by
    # model output; it is appended after every AI section.
    sections.extend(_evidence_manifest(generation_timestamp, context["dataset_version_ids"], context["analysis_run_ids"], context["evidence"]))
    sections.extend(["## 草稿状态", "", "本文档为 AI 生成的草稿：发布前需人工编辑、证据复核与明确确认。", ""])
    return "\n".join(sections)


def _render_document_markdown(body: DocumentGenerate, db: Session, user: User) -> tuple[str, list[dict[str, Any]]]:
    """Deterministic fallback renderer (template + evidence + manifest)."""

    context = _build_document_context(body, db, user)
    generation_timestamp = serialize(now())
    options = context["options"]
    period_label = str(options.get("period_label") or options.get("period") or "本期")[:120]
    audience = str(options.get("audience") or "产品团队")[:120]
    include_evidence = bool(options.get("include_evidence", True))
    include_acceptance = bool(options.get("include_acceptance_criteria", True))
    include_tracking = bool(options.get("include_tracking_plan", False))
    include_risks = bool(options.get("include_risks", True))
    sections = [f"# {body.title}", "", f"_草稿 · 面向 {audience} | {period_label}_", ""]
    evidence_sections: list[str] = context["evidence_sections"]

    if body.document_type == "weekly_report":
        sections.extend([
            "## 本期概览",
            "",
            "概述本期已确认的指标、洞察、反馈与已完成工作。",
            "",
            "## 关键变化",
            "",
            "- 已确认的变化与指标波动：待补充。",
            "",
            "## 核心问题与反馈",
            "",
            "- 优先处理下方证据支持的问题。",
            "",
            "## 已完成工作",
            "",
            "- 发布前请确认已完成事项。",
            "",
            "## 下期计划",
            "",
            "- 把已批准的决策转化为带截止日期的任务。",
            "",
        ])
        if include_risks:
            sections.extend(["## 风险与未决问题", "", "- 缺少证据的事项保持为未决问题。", ""])
    elif body.document_type == "retrospective":
        sections.extend([
            "## 背景与目标",
            "",
            "描述产品背景、预期结果与复盘周期。",
            "",
            "## 事实与结果",
            "",
            "区分观察到的事实与主观解读。",
            "",
            "## 根因假设",
            "",
            "记录假设及验证其所需的证据。",
            "",
            "## 决策与改进",
            "",
            "列出已批准的行动、负责人与验证计划。",
            "",
            "## 跟进事项",
            "",
            "- 复盘定稿前补充跟进任务与截止日期。",
            "",
        ])
        if include_risks:
            sections.extend(["## 风险与未决项", "", "- 明确标注尚未解决的假设。", ""])
    else:
        sections.extend([
            "## 需求背景",
            "",
            "描述用户问题以及支撑本草案的证据。",
            "",
            "## 问题与证据",
            "",
            "概述已确认的问题、受影响用户与支撑证据。",
            "",
            "## 目标与非目标",
            "",
            "- 目标：说明本方案要达成的结果。",
            "- 非目标：明确记录排除在外的范围。",
            "",
            "## 目标用户与场景",
            "",
            "描述目标用户、使用场景与预期价值。",
            "",
            "## 功能范围",
            "",
            "描述范围内功能与明确的排除项。",
            "",
            "## 用户流程",
            "",
            "描述主要用户步骤与关键决策点。",
            "",
            "## 页面与交互",
            "",
            "描述页面状态、输入、输出与交互要求。",
            "",
            "## 数据与埋点",
            "",
            "定义评估本次改动所需的指标字典条目与事件。",
            "",
        ])
        if include_tracking:
            tracking_events = options.get("tracking_events") or options.get("events") or []
            if isinstance(tracking_events, list) and tracking_events:
                sections.extend(["### 埋点计划", "", *[f"- {str(event)[:240]}" for event in tracking_events[:30]], ""])
            else:
                sections.extend(["### 埋点计划", "", "- 实现前补充事件名称、属性与成功指标。", ""])
        if include_acceptance:
            sections.extend(["## 验收标准", "", "- 用户流程可用明确的输入与预期输出验证。", "- 结果与已批准的证据及相应数据版本挂钩。", ""])
        if include_risks:
            sections.extend(["## 风险与未决问题", "", "- 记录上线风险、依赖项与待确认事项。", ""])

    if include_evidence:
        sections.extend(["## 证据清单", "", *evidence_sections])
    sections.extend(_evidence_manifest(generation_timestamp, context["dataset_version_ids"], context["analysis_run_ids"], context["evidence"]))
    sections.extend(["## 草稿状态", "", "本文档为草稿：发布前需人工编辑、证据复核与明确确认。", ""])
    return "\n".join(sections), context["evidence"]
