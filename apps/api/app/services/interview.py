"""Stage-6 AI interview: question rounds, dedup, and stage-7 distillation.

The interview replaces "AI dumps drafts" with "AI asks, humans answer".  A
round is one provider call proposing up to five grounded questions; the server
dedups against every question already asked for the project before persisting,
so repetition across rounds is impossible even when the model repeats itself.
Distillation turns answered questions plus the deterministic analysis
artifacts into a four-section insight *draft* -- the draft/adjudication
boundary in stage 7 is unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import (
    NEXT_QUESTION_SCHEMA,
    SUMMARY_SCHEMA,
    build_ai_context,
    validate_interview_summary,
    validate_next_question,
)
from ..analytics.id_hygiene import build_label_map, label_for_title, strip_resource_ids
from ..common import model_dict
from ..models import (
    AIRun,
    AnalysisArtifact,
    AnalysisRun,
    AutoAnalysisReport,
    Insight,
    InterviewQuestion,
    InterviewSummary,
    Project,
    User,
    Workspace,
    now,
)
from .ai_stages import _run_ai_stage
from .audit import audit

_ANSWER_CONTEXT_LIMIT = 50
_ARTIFACT_CONTEXT_LIMIT = 20
_REPORT_DATASET_LIMIT = 5
# Batch 18: an adaptive interview asks at most this many AI questions; the
# cap check is pre-provider so hitting it costs nothing.
_INTERVIEW_QUESTION_CAP = 10


def _normalise_question_text(text: str) -> str:
    """Reduce a question to its comparable core for server-side dedup."""

    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())


def _latest_report_context(db: Session, project: Project) -> list[dict[str, Any]]:
    """Dataset aggregates of the project's report -- the grounding chain.

    Batch 15: a project holds at most one report (compute deletes
    predecessors), so "the latest" is simply the only one; the old
    confirmed-first preference is gone.  Shape mirrors
    ``documents._build_document_context`` so both chains present the same
    evidence the same way; the per-report id prefix avoids collisions across
    reports.
    """

    report = db.scalar(
        select(AutoAnalysisReport)
        .where(AutoAnalysisReport.project_id == project.id)
        .order_by(AutoAnalysisReport.created_at.desc())
        .limit(1)
    )
    if report is None:
        return []
    deterministic = report.deterministic_json if isinstance(report.deterministic_json, dict) else {}
    datasets = deterministic.get("datasets")
    if not isinstance(datasets, list):
        return []
    return [
        {
            "id": f"{report.id}:{dataset.get('dataset_version_id')}",
            "artifact_type": "dataset_summary",
            # 批 37：业务名标签（数据集名 + 字段语义标签），title 即模型可见名。
            "title": label_for_title(str(dataset.get("name") or "dataset"), "dataset_summary", dataset),
            "payload_json": dataset,
        }
        for dataset in datasets[:_REPORT_DATASET_LIMIT]
        if isinstance(dataset, dict)
    ]


def _grounding_artifacts(db: Session, project: Project) -> list[dict[str, Any]]:
    """Report aggregates first, then raw artifact details (shared by both paths)."""

    return [*_latest_report_context(db, project), *_analysis_artifact_items(db, project)]


def _analysis_artifact_items(db: Session, project: Project) -> list[dict[str, Any]]:
    """Aggregate artifacts of the latest succeeded runs, provider-safe."""

    runs = db.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.project_id == project.id, AnalysisRun.status == "succeeded")
        .order_by(AnalysisRun.created_at.desc())
        .limit(3)
    ).all()
    if not runs:
        return []
    artifacts = db.scalars(
        select(AnalysisArtifact)
        .where(AnalysisArtifact.analysis_run_id.in_([run.id for run in runs]))
        # Stable read order (batch 17b): no ORDER BY here made the grounding
        # window's composition vary between processes.
        .order_by(AnalysisArtifact.created_at.asc(), AnalysisArtifact.id.asc())
        .limit(_ARTIFACT_CONTEXT_LIMIT)
    ).all()
    return [
        {
            "id": artifact.id,
            "artifact_type": artifact.artifact_type,
            # 批 37：业务名标签 —— 英文产物标题映射为中文业务名，模型引用时
            # 有可用的业务名称而不是 UUID。
            "title": label_for_title(artifact.title, artifact.artifact_type, artifact.payload_json),
            "payload_json": artifact.payload_json,
        }
        for artifact in artifacts
    ]


def _project_context(db: Session, project: Project, question: str) -> dict[str, Any]:
    return build_ai_context(
        goal=project.goal_statement or "",
        artifacts=_grounding_artifacts(db, project),
        question=question,
    )


async def generate_next_question(
    *, db: Session, user: User, workspace: Workspace, project: Project
) -> dict[str, Any]:
    """Adaptive interview: propose the next single question (batch 18/20).

    One provider call per question.  Idempotent: if a pending AI question is
    already on record (a duplicate request raced or the user navigated away),
    it is returned as-is with no provider call and no new row.  Termination
    is triadic: the model may declare the interview complete
    (``interview_complete``), the hard cap of ``_INTERVIEW_QUESTION_CAP`` AI
    questions is checked pre-provider, or the user ends manually via the
    completion summary.  Duplicate questions no longer end the interview --
    the model is asked for a different angle, then a template follow-up on an
    unexplored report finding is used as the last resort.
    """

    existing = db.scalars(
        select(InterviewQuestion).where(InterviewQuestion.project_id == project.id)
    ).all()
    # Batch 20 idempotency: a still-pending AI question IS the next question.
    # Repeated calls (double clicks, remounts, parallel tabs) reuse it instead
    # of queueing another provider call.
    pending = next((q for q in existing if q.source == "ai" and q.status == "pending"), None)
    if pending is not None:
        return {"status": "ok", "question": model_dict(pending), "reused": True}
    seen = {_normalise_question_text(q.question_text) for q in existing if q.question_text}
    ai_asked = sum(1 for q in existing if q.source == "ai")
    if ai_asked >= _INTERVIEW_QUESTION_CAP:
        return {"status": "complete", "reason": "cap_reached", "note": f"已达到 {_INTERVIEW_QUESTION_CAP} 个 AI 问题的上限，请生成小结或手动补充要点。"}

    answered = [q for q in existing if q.status == "answered"][:_ANSWER_CONTEXT_LIMIT]
    answered_items = [
        {
            "id": q.id,
            "artifact_type": "interview_answer",
            "title": q.topic or q.question_text[:120],
            "payload_json": {"question": q.question_text, "answer": q.answer_text},
        }
        for q in answered
    ]
    asked_items = [
        {
            "id": q.id,
            "artifact_type": "asked_question",
            # 批 36：title 与 question_text 重复，精简掉——asked_items 只保留
            # id/question/status，降低下一问调用的上下文体积。
            "payload_json": {"question": q.question_text, "status": q.status},
        }
        for q in existing
    ]
    context = build_ai_context(
        goal=project.goal_statement or "",
        artifacts=[*_grounding_artifacts(db, project), *answered_items, *asked_items],
        question="请提出下一个采访问题，或判定信息已收集充分",
    )

    next_round = ai_asked + 1
    last_error = "AI_UNAVAILABLE"
    for attempt in (1, 2, 3):  # up to 3 provider calls chasing a fresh angle
        extra = ""
        if attempt > 1:
            # Batch 20: duplicates no longer end the interview -- ask the
            # model to attack the topic from an unexplored angle.
            extra = "此前提出的问题与已有问题重复。换一个此前从未问过的角度重新提问，仍然不得与已有问题重复。"
        result = await _run_ai_stage(
            db=db,
            user=user,
            workspace=workspace,
            feature_name="interview_next_question",
            system_prompt=(
                "你是产品访谈者，一次只问一个问题。"
                "输入：项目目标、数据发现（含严重度）、已问清单、已回答摘要。"
                "规则：1) 下一问必须指向「高严重度发现中尚未被任何回答覆盖」的一个具体疑点；"
                "2) question_text ≤60 字，含一个可回答的落点（数字/原因/场景）；"
                "3) rationale ≤50 字，用业务名称指明所依据的材料与指标（如「渗透率与周活跃用户的强相关」）；"
                "4) 若高严重度发现均已被回答覆盖，返回 interview_complete=true，completion_note ≤80 字说明判定依据；"
                "5) 不得与已问清单语义重复。"
                "6) question_text 与 rationale 中禁止出现任何 UUID、哈希或资源 id 字符串；"
                "引用依据一律使用业务名称（列名/数据集名/模块名）。" + extra
            ),
            context=context,
            flag_name="insight_suggestions_enabled",
            response_schema=NEXT_QUESTION_SCHEMA,
            output_validator=validate_next_question,
            empty_output={
                "question_text": "",
                "topic": "",
                "rationale": "",
                "interview_complete": True,
                "completion_note": "AI provider is not configured.",
            },
        )
        if result["status"] != "succeeded":
            return {
                "status": result.get("status") or "failed",
                "error_code": result.get("error_code") or last_error,
                "message": "AI 采访暂不可用，可手动补充要点后重试。",
            }
        last_error = result.get("error_code") or last_error
        output = result["output"]
        # 批 37：确定性兜底 —— 用户可见文本（问题/依据/完成说明）中的
        # UUID/8位hex 一律替换为业务名；prompt 禁令之外的第二道闸。
        label_map = build_label_map(_grounding_artifacts(db, project))
        if output.get("interview_complete"):
            note = strip_resource_ids(str(output.get("completion_note") or ""), label_map)
            audit(db, workspace.id, user.id, "interview.next_question", "project", project.id, {"outcome": "ai_judged_complete"})
            db.commit()
            return {"status": "complete", "reason": "ai_judged", "note": note}
        question_text = strip_resource_ids(str(output["question_text"]), label_map)
        rationale = strip_resource_ids(str(output["rationale"]), label_map)
        key = _normalise_question_text(question_text)
        if key and key not in seen:
            question = InterviewQuestion(
                workspace_id=project.workspace_id,
                project_id=project.id,
                round_number=next_round,
                topic=output["topic"],
                question_text=question_text,
                rationale=rationale,
                status="pending",
                answer_text="",
                source="ai",
                ai_run_id=result["run_id"],
                created_by=user.id,
            )
            db.add(question)
            db.flush()
            audit(db, workspace.id, user.id, "interview.next_question", "project", project.id, {"outcome": "asked", "round": next_round})
            db.commit()
            return {"status": "ok", "question": model_dict(question)}
    # Batch 20: the model keeps repeating itself -- fall back to a template
    # follow-up on an unexplored report finding instead of ending the
    # interview.  Only an explicit interview_complete or the cap ends it.
    report = db.scalar(
        select(AutoAnalysisReport)
        .where(AutoAnalysisReport.project_id == project.id)
        .order_by(AutoAnalysisReport.created_at.desc())
        .limit(1)
    )
    findings = (
        report.deterministic_json.get("findings")
        if report is not None and isinstance(report.deterministic_json, dict)
        else None
    ) or []
    asked_texts = {q.question_text for q in existing}
    unexplored = next(
        (
            item
            for item in findings
            if isinstance(item, dict) and item.get("statement") not in asked_texts
        ),
        None,
    )
    statement = str((unexplored or {}).get("statement") or "报告中的关键数据发现")
    audit(db, workspace.id, user.id, "interview.next_question", "project", project.id, {"outcome": "template_fallback"})
    db.commit()
    template_question = InterviewQuestion(
        workspace_id=project.workspace_id,
        project_id=project.id,
        round_number=next_round,
        topic="数据发现追问",
        question_text=f"关于「{statement[:150]}」，实际情况与预期的差距主要来自什么？",
        rationale="AI 连续未能提出新角度，改用模板追问以继续收集信息。",
        status="pending",
        answer_text="",
        source="ai",
        ai_run_id=None,
        created_by=user.id,
    )
    db.add(template_question)
    db.commit()
    return {"status": "ok", "question": model_dict(template_question)}


async def complete_interview(
    *, db: Session, user: User, workspace: Workspace, project: Project
) -> dict[str, Any]:
    """Generate and persist the end-of-interview digest (batch 18).

    Idempotent: each call overwrites the project's single summary row.  The
    digest is only meaningful with at least one answered question -- skipped
    or pending-only interviews have nothing to distill.
    """

    answered = db.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.project_id == project.id, InterviewQuestion.status == "answered")
        .order_by(InterviewQuestion.created_at)
        .limit(_ANSWER_CONTEXT_LIMIT)
    ).all()
    if not answered:
        raise ValueError("no answered questions")

    answered_items = [
        {
            "id": q.id,
            "artifact_type": "interview_answer",
            "title": q.topic or q.question_text[:120],
            "payload_json": {"question": q.question_text, "answer": q.answer_text},
        }
        for q in answered
    ]
    context = build_ai_context(
        goal=project.goal_statement or "",
        artifacts=[*_grounding_artifacts(db, project), *answered_items],
        question="总结这次采访收集到的信息，并指出对下一步洞察蒸馏的建议",
    )
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="interview_summary",
            system_prompt=(
                "你是产品分析师。基于给定的报告聚合、数据发现与采访问答对，生成本次采访的收尾小结，"
                "全部使用简体中文：collected 列出围绕哪些数据发现收集到了哪些判断（每条一句话，可引用数字）；"
                "gaps 列出未覆盖、只能依赖数据本身回答的部分；ready_for 用 ≤3 句话给出对下一步洞察蒸馏的建议"
                "（哪些结论可以直接蒸馏、哪些还需要数据验证）。"
                "collected/gaps 各 ≤6 条、每条 ≤40 字。"
            ),
        context=context,
        flag_name="insight_suggestions_enabled",
        response_schema=SUMMARY_SCHEMA,
        output_validator=validate_interview_summary,
        empty_output={"collected": [], "gaps": [], "ready_for": ""},
    )
    if result["status"] != "succeeded":
        return {
            "status": result.get("status") or "failed",
            "error_code": result.get("error_code") or "AI_UNAVAILABLE",
            "message": "小结生成暂不可用，可稍后重试或直接进入洞察蒸馏。",
        }
    import json as _json

    summary_label_map = build_label_map(_grounding_artifacts(db, project))
    summary_payload = {
        "collected": [strip_resource_ids(str(item), summary_label_map) for item in result["output"].get("collected") or []],
        "gaps": [strip_resource_ids(str(item), summary_label_map) for item in result["output"].get("gaps") or []],
        "ready_for": strip_resource_ids(str(result["output"].get("ready_for") or ""), summary_label_map),
    }
    summary_row = db.scalar(select(InterviewSummary).where(InterviewSummary.project_id == project.id))
    if summary_row is None:
        summary_row = InterviewSummary(workspace_id=project.workspace_id, project_id=project.id, created_by=user.id)
        db.add(summary_row)
    summary_row.summary = _json.dumps(summary_payload, ensure_ascii=False)
    summary_row.ai_run_id = result["run_id"]
    audit(db, workspace.id, user.id, "interview.completed", "project", project.id, {"answered": len(answered)})
    db.commit()
    return {"status": "ok", "summary": summary_payload, "answered": len(answered)}


def create_manual_question(db: Session, user: User, project: Project, *, topic: str, question_text: str, answer_text: str) -> dict[str, Any]:
    """Persist a manual supplement (round 0).  With info supplied it counts as
    answered immediately; otherwise it stays pending like an AI question."""

    answered = bool(answer_text.strip())
    question = InterviewQuestion(
        workspace_id=project.workspace_id,
        project_id=project.id,
        round_number=0,
        topic=topic.strip()[:120],
        question_text=question_text.strip(),
        rationale="",
        status="answered" if answered else "pending",
        answer_text=answer_text,
        source="manual",
        created_by=user.id,
        answered_at=now() if answered else None,
    )
    db.add(question)
    db.flush()
    audit(db, project.workspace_id, user.id, "interview.question_added", "interview_question", question.id, {"source": "manual", "status": question.status})
    db.commit()
    return model_dict(question)


def update_interview_question(db: Session, user: User, question: InterviewQuestion, *, answer_text: str | None, status: str | None) -> dict[str, Any]:
    if answer_text is not None:
        question.answer_text = answer_text
        question.status = "answered"
        question.answered_at = now()
    if status == "answered":
        question.status = "answered"
        question.answered_at = question.answered_at or now()
    elif status == "skipped":
        question.status = "skipped"
    audit(db, question.workspace_id, user.id, "interview.question_updated", "interview_question", question.id, {"status": question.status})
    db.commit()
    return model_dict(question)


async def distill_interview(
    *, db: Session, user: User, workspace: Workspace, project: Project
) -> dict[str, Any]:
    """Distill answered questions + analysis artifacts into insight drafts.

    Batch 18: the distillation auto-persists -- every claim with evidence
    becomes a draft ``Insight`` server-side (subtractive adjudication happens
    in stage 7: reject/edit/confirm).  Idempotent on re-distill: drafts that
    this project's earlier ``interview_distill`` runs produced and that are
    still ``draft`` are replaced; confirmed/rejected insights are untouched.
    The evidence contract is enforced per claim -- no evidence, no insight.
    """

    questions = db.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.project_id == project.id, InterviewQuestion.status == "answered")
        .order_by(InterviewQuestion.created_at)
        .limit(_ANSWER_CONTEXT_LIMIT)
    ).all()
    analysis_items = _analysis_artifact_items(db, project)
    report_items = _latest_report_context(db, project)

    interview_items = [
        {
            "id": q.id,
            "artifact_type": "interview_answer",
            "title": q.topic or q.question_text[:120],
            "payload_json": {"question": q.question_text, "answer": q.answer_text},
        }
        for q in questions
    ]
    context = build_ai_context(
        goal=project.goal_statement or "",
        artifacts=[*report_items, *interview_items, *analysis_items],
        question="把采访问答与数据结论蒸馏成洞察草稿：事实、假设、建议，逐条给证据",
    )
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="interview_distill",
        system_prompt=(
            "你是产品分析助手。把采访回答（interview_answer 产物）与数据结论蒸馏成洞察草稿。"
            "facts=有数字或回答原文直接支撑的结论；hypotheses=待验证判断（写明验证方式）；"
            "recommendations=可执行下一步。"
            "条数 3-8 条、由证据决定，每条 ≤80 字，宁少勿凑；"
            "每条 evidence 恰好 1 条，id 原样取自上下文；limitations ≤3 条。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
    )
    output = _normalize_distill_evidence(result.get("output") or {}, questions, analysis_items)
    result["output"] = output

    # ---- auto-persist drafts (batch 18) ----
    created: list[Insight] = []
    discarded = 0
    if result["status"] == "succeeded":
        # Idempotent refresh: drop this project's still-draft insights that
        # earlier distillation runs produced (ai_run_id -> interview_distill
        # run).  Confirmed/rejected insights -- the user's adjudication --
        # are never touched.
        distill_run_ids = set(
            db.scalars(
                select(AIRun.id).where(
                    AIRun.workspace_id == project.workspace_id,
                    AIRun.feature_name == "interview_distill",
                )
            ).all()
        )
        stale = db.scalars(
            select(Insight).where(
                Insight.project_id == project.id,
                Insight.status == "draft",
                Insight.ai_run_id.in_(distill_run_ids),
            )
        ).all()
        for insight in stale:
            db.delete(insight)
        db.flush()

        type_by_section = {"facts": "fact", "hypotheses": "hypothesis", "recommendations": "recommendation"}
        distill_label_map = build_label_map(_grounding_artifacts(db, project))
        for section, insight_type in type_by_section.items():
            for claim in output.get(section) or []:
                evidence = claim.get("evidence") or []
                text = strip_resource_ids(str(claim.get("text") or "").strip(), distill_label_map)
                if not text or not evidence:
                    discarded += 1
                    continue
                insight = Insight(
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    title=text[:120],
                    insight_type=insight_type,
                    content=text,
                    confidence="medium",
                    evidence_json=evidence,
                    status="draft",
                    ai_run_id=result["run_id"],
                    created_by=user.id,
                )
                db.add(insight)
                created.append(insight)
        if created:
            db.flush()
    audit(
        db,
        project.workspace_id,
        user.id,
        "interview.distilled",
        "project",
        project.id,
        {"status": result["status"], "created": len(created), "discarded": discarded},
    )
    db.commit()
    return {
        **result,
        "created": [model_dict(insight) for insight in created],
        "discarded_claims": discarded,
        "interview_answer_count": len(questions),
        "analysis_artifact_count": len(analysis_items),
    }


_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)


def _normalize_distill_evidence(output: dict[str, Any], questions: list[InterviewQuestion], analysis_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Rewrite every evidence entry into a legal ``{type, id}`` reference.

    The model routinely emits bare ``{"id": ...}`` objects, wrong types, or the
    id as a plain string; the insight persistence layer rejects all of those.
    The id's membership decides the type -- a question id is an
    ``interview_question`` reference no matter what the model called it.
    Entries pointing outside this project's known ids are dropped, and the
    existing deterministic fallback only applies when everything was dropped.
    """

    # lowercase -> canonical id as stored in the DB (SQLite compares ids
    # case-sensitively, so saved references must carry the stored spelling)
    question_ids = {q.id.lower(): q.id for q in questions}
    artifact_ids = {item["id"].lower(): item["id"] for item in analysis_items}
    fallback: dict[str, Any] | None = None
    if questions:
        fallback = {"type": "interview_question", "id": questions[0].id}
    elif analysis_items:
        fallback = {"type": "analysis_artifact", "id": analysis_items[0]["id"]}

    def resolve(entry: Any) -> dict[str, Any] | None:
        candidates: list[str] = []
        if isinstance(entry, dict):
            value = entry.get("id")
            if isinstance(value, str):
                candidates.append(value)
        elif isinstance(entry, str):
            candidates.append(entry)
            candidates.extend(_UUID_RE.findall(entry))
        for candidate in candidates:
            key = candidate.lower()
            if key in question_ids:
                return {"type": "interview_question", "id": question_ids[key]}
            if key in artifact_ids:
                return {"type": "analysis_artifact", "id": artifact_ids[key]}
        return None

    for section in ("facts", "hypotheses", "recommendations"):
        for claim in output.get(section, []):
            resolved: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for entry in claim.get("evidence", []):
                reference = resolve(entry)
                if reference is None:
                    continue
                key = (reference["type"], reference["id"])
                if key in seen:
                    continue
                seen.add(key)
                resolved.append(reference)
            if not resolved and fallback is not None:
                resolved = [dict(fallback)]
            claim["evidence"] = resolved
    return output
