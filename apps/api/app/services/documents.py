from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import assert_safe_ai_context, build_ai_context
from ..analytics.id_hygiene import build_label_map, strip_resource_ids
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


def _outline_system_prompt(document_type: str, title: str, audience: str) -> str:
    """批 35 重写：大纲产出 findings + 每章 key_refs（分节裁剪的依据）。"""

    plan = "、".join(_DOCUMENT_TYPE_SECTIONS.get(document_type, _DOCUMENT_TYPE_SECTIONS["prd"]))
    return (
        "你是产品文档架构师。基于证据材料（数据发现、决策链、字段口径）为"
        f"《{title}》（类型 {document_type}，读者 {audience}）产出大纲。"
        "1) findings：≤6 条，每条 id 形如 finding-N，一句话含具体数字，severity ∈ 高/中/低，按信息量降序排列；"
        f"2) sections：按顺序覆盖固定章节计划：{plan}；"
        "每章给 heading（与计划一致）、purpose（≤40 字）、"
        "key_refs（本章依赖的 finding 编号列表，2-4 个，不得为空）；"
        "3) root_cause：3-5 句，含数字。"
        "全文 ≤2000 tokens。禁止编造数字。"
    )


_DOC_OUTLINE_FIELD_LABEL_LIMIT = 40
_DOC_OUTLINE_FINDING_LIMIT = 12


def _outline_context(doc_context: dict[str, Any]) -> dict[str, Any]:
    """Batch 25: the summary layer the outline call sees instead of the full
    aggregate context (9k tokens -> a fraction of that, ~70s -> ~20-30s).

    Goal, the findings digest, the decision-chain axis and the field-semantics
    labels are what an outline actually needs; the per-dataset aggregates stay
    available to the section writers, which is where the numbers get used.
    Direct structured dict (same convention as the section context add-ons --
    never re-passed through the firewall).
    """

    safe = doc_context.get("safe_context") or {}
    labels: list[str] = []
    for item in doc_context.get("field_labels") or []:
        text = str(item).strip()
        if text and text not in labels:
            labels.append(text)
    findings = [str(item) for item in (doc_context.get("findings_summary") or [])[:_DOC_OUTLINE_FINDING_LIMIT]]
    # 批 35：findings 编号说明 —— 让模型知道 finding-N 对应哪条材料，key_refs
    # 才能引用得准（一行序号映射，来自 findings_summary 的顺序）。
    finding_refs = "；".join(f"finding-{index}: {title}" for index, title in enumerate(findings, start=1))
    return {
        "goal": safe.get("goal") or "",
        "question": safe.get("question") or "",
        "findings": findings,
        "finding_refs": finding_refs,
        "solution": doc_context.get("solution"),
        "decision": doc_context.get("decision"),
        "field_labels": labels[:_DOC_OUTLINE_FIELD_LABEL_LIMIT],
    }


def _section_context(
    doc_context: dict[str, Any],
    key_refs: list[str] | None,
    outline_findings: list[dict[str, Any]],
    written_summary: str,
    outline_plan: str,
) -> dict[str, Any]:
    """批 35：章节专属上下文 —— 大块材料按本章 key_refs 裁剪。

    保留 goal/question 与全部非数据集聚合产物（findings/洞察/采访答案：体积小、
    是叙事证据）；``dataset_summary`` 只保留 key_refs 命中的切片。解析规则：
    ① ref 直接等于任一材料 id；② ``finding-N`` → 第 N 条 finding 产物（与
    findings_summary 同序）→ 其 ``payload.dataset`` 指向的数据集聚合。
    key_refs 缺失、全部解析不到、或命中了 finding 却定位不到任何数据集时，
    回退注入全量（坏引用绝不挂掉整节，也不让章节被饿死）。

    裁剪只发生在已过防火墙的 ``safe_context`` 上；裁剪后的白名单键再次通过
    ``assert_safe_ai_context`` 复核（防止切片过程引入任何越界字段），
    ``_AGGREGATE_LIST_KEYS`` 等防火墙键一律不动。
    """

    safe = doc_context.get("safe_context") or {}
    artifacts = safe.get("artifacts") or []
    finding_artifacts = [item for item in artifacts if item.get("artifact_type") == "finding"]
    dataset_artifacts = [item for item in artifacts if item.get("artifact_type") == "dataset_summary"]

    refs = [str(ref).strip() for ref in (key_refs or []) if str(ref).strip()]
    matched_ids: set[str] = set()
    for ref in refs:
        for item in artifacts:
            if str(item.get("id")) == ref:
                matched_ids.add(str(item.get("id")))
        match = re.fullmatch(r"finding-(\d+)", ref)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(finding_artifacts):
                finding = finding_artifacts[index]
                matched_ids.add(str(finding.get("id")))
                dataset_name = str((finding.get("payload") or {}).get("dataset") or "")
                if dataset_name:
                    for dataset in dataset_artifacts:
                        if dataset_name in str(dataset.get("title") or ""):
                            matched_ids.add(str(dataset.get("id")))

    if refs and matched_ids:
        matched_datasets = matched_ids & {str(item.get("id")) for item in dataset_artifacts}
        if dataset_artifacts and not matched_datasets:
            # findings 命中但定位不到数据集：回退全量，避免章节被饿死。
            sliced = artifacts
        else:
            sliced = [
                item
                for item in artifacts
                if item.get("artifact_type") != "dataset_summary" or str(item.get("id")) in matched_ids
            ]
    else:
        sliced = artifacts

    allowed = assert_safe_ai_context({**safe, "artifacts": sliced})
    section_context: dict[str, Any] = {
        **allowed,
        "outline_findings": outline_findings,
        "solution": doc_context.get("solution"),
        "decision": doc_context.get("decision"),
    }
    if written_summary:
        section_context["written_summary"] = written_summary
    if outline_plan:
        section_context["outline_plan"] = outline_plan
    return section_context


# Batch 25: the harmonize output must rewrite sections verbatim (tables and
# numbers included), so a batch has to stay well under HARD_OUTPUT_CAP --
# a full 10-section PRD (~20k chars) physically cannot fit one 16384-token
# reply (found in the live run: LLM_PROVIDER_ERROR at a pinned 16384).  Six
# sections per call keeps the worst case around ~12k tokens.
_HARMONIZE_BATCH_SIZE = 6


def _harmonize_system_prompt() -> str:
    """Batch 25 pass 3: whole-document coherence pass over the assembled sections.

    Runs per batch of sections; ``document_headings`` in the context carries
    the full chapter order so cross-batch duplication also collapses to a
    short back-reference instead of a repeat.
    """

    return (
        "你是文档主编，对一份已经写完的多节文档做连贯校对。给定的 sections 是全文的一个批次，"
        "document_headings 是全文档的章节顺序。任务只有两件："
        "1) 消除重复——同一论点、同一表格或同一段论证在本批次多个章节（或已在其他批次章节出现，"
        "以 document_headings 为准）重复时，只保留最合适的一节，其余位置改写为一句简短承接"
        "（如「详见「X」一节」）；2) 平滑章节衔接——过渡自然、指代一致、语气统一。"
        "铁律：表格、数字、证据引用（证据标题或 id）必须逐字保留，不得改写数值或改述表格内容；"
        "不得新增任何论断、数据、建议或结论；不得合并、拆分、增加或删除章节，"
        "heading 与章节顺序必须与输入完全一致；改写后各节字数不得超过原文的 105%，"
        "不得新增任何数字或论断；校对以删除冗余为第一手段，改写为第二手段；每节只输出该节改写后的正文。"
        '只输出 JSON：{"sections": [{"heading", "content"}, ...]}，节数与顺序与输入相同。'
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
    outline_plan: str = "",
    materials_summary: str = "",
) -> str:
    """批 35 重写：可操作三要素（数字锚点/设计决策/badcase）+ 本章专属材料。

    ``materials_summary`` 来自裁剪后的本章切片标题，是本节唯一取数来源。
    wave-1（顺序撰写）带 ``written_summary``；wave-2（并行撰写）没有前文，
    反重复契约由 ``outline_plan`` 承担（结构不变，第五批两波/信号量断言不受影响）。
    """

    axis = ""
    if decision:
        axis += (
            f"主叙事轴：问题=「{decision.get('problem_statement') or ''}」，"
            f"行动=「{decision.get('proposed_action') or ''}」，验证=「{decision.get('validation_plan') or ''}」。"
        )
    if solution:
        axis += f"选定方案=「{solution.get('title') or ''}」（工作量 {solution.get('effort') or 'M'}）。"
    materials = (
        f"本章专属材料（只能从这里取数）：{materials_summary}。" if materials_summary else "本章材料见上下文 artifacts。"
    )
    head = f"你撰写《{title}》第 {index}/{total} 节「{heading}」（目的：{purpose or '按标题展开'}；读者 {audience}）。"
    if document_type == "prd":
        requirements = (
            "要求：1) 正文 700-1200 字，必须包含：≥1 个数字锚点（来自本章材料，句尾标 [finding-N]）、"
            "1 个明确的设计决策、1 个边界情况（badcase）及其兜底；"
            "2) 目标用表格（|目标|指标|目标值|），验收用表格（|编号|验收点|预期|），每格 ≤20 字；"
            "用户流程用「场景一/场景二」编号叙述；"
            "3) 禁止复述其他章节、禁止编造数字、禁止输出与本章无关的分析；"
            "4) 每个论点必须可追溯到本章材料，无法追溯的论断直接删除；"
            "同一信息不得在两处展开；拿不准时优先删弱论据，而不是稀释强论据。"
        )
    else:
        requirements = "本节正文 400–800 字；涉及数据必须引用具体数字；不要重复其他章节内容。"
    summary_line = f"前文摘要（不要重复）：{written_summary[:300]}。" if written_summary else ""
    plan_line = (
        f"各节范围以大纲为准，不得与其他章节重复；大纲全文（你的节是第 {index} 项）：{outline_plan}"
        if outline_plan
        else ""
    )
    return (
        f"{head}{axis}{materials}{summary_line}{plan_line}{requirements}"
        + ' 只输出 JSON：{"heading", "content"}，content 为 Markdown。'
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
    # Batch 25: live progress for the same in-flight job, so a page returning
    # mid-generation can render the bar immediately (no 2s poll wait).
    payload["generation_progress"] = (
        {"progress": active.progress, "current_step": active.current_step} if active is not None else None
    )
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
    # Batch 25: the slim outline layer needs the findings digest and the
    # field-semantics labels; collected while the artifacts are assembled so
    # no second query is required.
    findings_summary: list[str] = []
    field_labels: list[str] = []
    if report is not None:
        deterministic = report.deterministic_json if isinstance(report.deterministic_json, dict) else {}
        datasets = deterministic.get("datasets")
        if isinstance(datasets, list):
            for dataset in datasets[:_DOC_DATASET_SUMMARY_LIMIT]:
                if not isinstance(dataset, dict):
                    continue
                if dataset.get("dataset_label"):
                    field_labels.append(f"{dataset.get('name')}（{dataset.get('dataset_label')}）")
                for column in dataset.get("metrics") or []:
                    if isinstance(column, dict) and column.get("label"):
                        field_labels.append(f"{column.get('name')}（{column.get('label')}）")
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
                if item.title:
                    findings_summary.append(item.title)
        else:
            findings = deterministic.get("findings")
            if isinstance(findings, list):
                for index, item in enumerate(findings[:_DOC_FINDING_LIMIT], start=1):
                    if not isinstance(item, dict):
                        continue
                    statement = str(item.get("statement") or "")[:200]
                    if statement:
                        findings_summary.append(statement)
                    artifacts.append(
                        {
                            "id": f"finding-{index}",
                            "artifact_type": "finding",
                            "title": statement,
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
        "findings_summary": findings_summary,
        "field_labels": field_labels,
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
        "所有论断必须来自给定上下文，并在内容中自然标注依据（引用证据标题或 id）；"
        "禁止出现英文模板句或占位文案，语气面向指定读者。"
        f"输出面向读者：{audience}。"
    )


def _render_ai_document_markdown(body: DocumentGenerate, ai_output: dict[str, Any], context: dict[str, Any]) -> str:
    """Render the validated REPORT_OUTPUT_SCHEMA payload as Chinese Markdown."""

    generation_timestamp = serialize(now())
    # 批 37：正文确定性兜底清洗 —— 用户可见文本不得出现产物 id/hex 串
    # （label_map 来自文档上下文的产物映射；[finding-N] 非 hex，不受影响）。
    label_map = build_label_map((context.get("safe_context") or {}).get("artifacts") or [])
    sections: list[str] = [f"# {ai_output.get('title') or body.title}", ""]
    summary = strip_resource_ids(str(ai_output.get("summary") or "").strip(), label_map)
    if summary:
        sections.extend([f"> {summary}", ""])
    for section in ai_output.get("sections", []):
        heading = str(section.get("heading") or "").strip()
        content = strip_resource_ids(str(section.get("content") or "").strip(), label_map)
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
