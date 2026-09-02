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
    Project,
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

_DOCUMENT_SECTION_BRIEFS = {
    "weekly_report": "章节结构：本期概览、关键变化、核心问题与反馈、已完成工作、下期计划",
    "prd": "章节结构：需求背景、目标与非目标、用户与场景、功能范围、用户流程、数据与埋点、验收标准",
    "retrospective": "章节结构：背景、事实与结果、根因假设、决策与改进、跟进事项",
}


def _document_payload(document: Document, db: Session) -> dict[str, Any]:
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number)).all()
    current = next((version for version in versions if version.id == document.current_version_id), versions[-1] if versions else None)
    return model_dict(document, {"current_version": model_dict(current) if current else None, "versions": [model_dict(version) for version in versions]})


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
    return {
        "evidence": evidence,
        "dataset_version_ids": dataset_version_ids,
        "analysis_run_ids": analysis_run_ids,
        "evidence_sections": evidence_sections,
    }


def _build_document_context(body: DocumentGenerate, db: Session, user: User) -> dict[str, Any]:
    """Assemble the grounded, firewall-safe context for AI document rendering.

    The artifacts channel carries four evidence classes: confirmed insights,
    answered interview questions, approved decisions and the per-dataset
    aggregates of the latest auto-report.  Everything passes through
    ``build_ai_context``; no raw rows or storage paths ever leave.
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
        **collected,
    }


def _evidence_manifest(generation_timestamp: str, dataset_version_ids: set[str], analysis_run_ids: set[str], evidence: list[dict[str, Any]]) -> list[str]:
    """Immutable provenance block shared by the AI and fallback renderers."""

    return [
        "## Evidence manifest",
        "",
        f"- Generated at: {generation_timestamp}",
        f"- Dataset version IDs: {', '.join(sorted(dataset_version_ids)) or 'none'}",
        f"- Analysis run IDs: {', '.join(sorted(analysis_run_ids)) or 'none'}",
        f"- Source refs: {json.dumps(evidence, ensure_ascii=False, sort_keys=True)}",
        "",
    ]


def _document_system_prompt(document_type: str, audience: str) -> str:
    brief = _DOCUMENT_SECTION_BRIEFS.get(document_type, _DOCUMENT_SECTION_BRIEFS["prd"])
    return (
        "你是产品交付文档撰写助手。基于给定的证据材料（洞察、采访回答、已批准决策、数据集聚合）撰写文档。"
        f"{brief}。"
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
    sections.extend(["## Draft status", "", "This document is a draft. Human editing, evidence review and explicit confirmation are required before publication.", ""])
    return "\n".join(sections)


def _render_document_markdown(body: DocumentGenerate, db: Session, user: User) -> tuple[str, list[dict[str, Any]]]:
    """Deterministic fallback renderer (template + evidence + manifest)."""

    context = _build_document_context(body, db, user)
    generation_timestamp = serialize(now())
    options = context["options"]
    period_label = str(options.get("period_label") or options.get("period") or "Current period")[:120]
    audience = str(options.get("audience") or "Product team")[:120]
    include_evidence = bool(options.get("include_evidence", True))
    include_acceptance = bool(options.get("include_acceptance_criteria", True))
    include_tracking = bool(options.get("include_tracking_plan", False))
    include_risks = bool(options.get("include_risks", True))
    sections = [f"# {body.title}", "", f"_Draft for {audience} | {period_label}_", ""]
    evidence_sections: list[str] = context["evidence_sections"]

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
    sections.extend(_evidence_manifest(generation_timestamp, context["dataset_version_ids"], context["analysis_run_ids"], context["evidence"]))
    sections.extend(["## Draft status", "", "This document is a draft. Human editing, evidence review and explicit confirmation are required before publication.", ""])
    return "\n".join(sections), context["evidence"]
