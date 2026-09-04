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
    INTERVIEW_QUESTIONS_SCHEMA,
    build_ai_context,
    validate_interview_questions,
)
from ..common import model_dict
from ..models import (
    AnalysisArtifact,
    AnalysisRun,
    AutoAnalysisReport,
    InterviewQuestion,
    Project,
    User,
    Workspace,
    now,
)
from .ai_stages import _run_ai_stage
from .audit import audit

_ANSWER_CONTEXT_LIMIT = 50
# Batch 13: widened from 12 so the report's finding artifacts fit the window
# alongside the raw analysis artifacts.
_ARTIFACT_CONTEXT_LIMIT = 20
_REPORT_DATASET_LIMIT = 5


def _normalise_question_text(text: str) -> str:
    """Reduce a question to its comparable core for server-side dedup."""

    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())


def _latest_report_context(db: Session, project: Project) -> list[dict[str, Any]]:
    """Dataset aggregates of the project's report -- the grounding chain.

    Batch 13: the interview and the distillation must reason from the report
    the user confirmed (or, unconfirmed, the newest one), not only from raw
    artifacts.  Shape mirrors ``documents._build_document_context`` so both
    chains present the same evidence the same way; the per-report id prefix
    avoids collisions across reports.
    """

    reports = db.scalars(
        select(AutoAnalysisReport).where(AutoAnalysisReport.project_id == project.id)
    ).all()
    if not reports:
        return []
    report = next((item for item in reports if item.confirmed_at is not None), None) or max(
        reports, key=lambda item: item.created_at
    )
    deterministic = report.deterministic_json if isinstance(report.deterministic_json, dict) else {}
    datasets = deterministic.get("datasets")
    if not isinstance(datasets, list):
        return []
    return [
        {
            "id": f"{report.id}:{dataset.get('dataset_version_id')}",
            "artifact_type": "dataset_summary",
            "title": str(dataset.get("name") or "dataset"),
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
        .limit(_ARTIFACT_CONTEXT_LIMIT)
    ).all()
    return [
        {
            "id": artifact.id,
            "artifact_type": artifact.artifact_type,
            "title": artifact.title,
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


async def generate_interview_round(
    *, db: Session, user: User, workspace: Workspace, project: Project
) -> dict[str, Any]:
    """Ask the model for the next round of questions, dedup, persist as ai rows."""

    existing = db.scalars(
        select(InterviewQuestion).where(InterviewQuestion.project_id == project.id)
    ).all()
    seen = {_normalise_question_text(q.question_text) for q in existing if q.question_text}
    next_round = max((q.round_number for q in existing if q.source == "ai"), default=0) + 1

    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name="interview_round",
        system_prompt=(
            "你是产品分析师（采访者）。基于给定的项目目标与分析结论，提出本轮采访问题（3-5 个），"
            "帮助澄清数据结论背后的用户动机、场景与业务背景。每个问题输出 topic（主题，尽量短）、"
            "question_text（问题正文）、rationale（为什么问这个，引用哪条结论）。"
            "给定的报告聚合（dataset_summary）与 findings 是本项目数据侧已确认的重点，提问应优先围绕这些重点展开。"
            "问题之间不得重复，也不要重复给定的历史问题。输出默认是 draft。"
        ),
        context=_project_context(db, project, "请提出下一轮采访问题"),
        flag_name="insight_suggestions_enabled",
        response_schema=INTERVIEW_QUESTIONS_SCHEMA,
        output_validator=validate_interview_questions,
        empty_output={"questions": []},
    )

    created: list[InterviewQuestion] = []
    duplicates_dropped = 0
    if result["status"] == "succeeded":
        for item in result["output"].get("questions", []):
            key = _normalise_question_text(item["question_text"])
            if not key or key in seen:
                duplicates_dropped += 1
                continue
            seen.add(key)
            question = InterviewQuestion(
                workspace_id=project.workspace_id,
                project_id=project.id,
                round_number=next_round,
                topic=item["topic"],
                question_text=item["question_text"],
                rationale=item["rationale"],
                status="pending",
                answer_text="",
                source="ai",
                ai_run_id=result["run_id"],
                created_by=user.id,
            )
            db.add(question)
            created.append(question)
        if created:
            db.flush()

    audit(
        db,
        project.workspace_id,
        user.id,
        "interview.round_generated",
        "project",
        project.id,
        {"status": result["status"], "created": len(created), "duplicates_dropped": duplicates_dropped},
    )
    db.commit()
    return {
        "run_id": result["run_id"],
        "status": result["status"],
        "error_code": result.get("error_code"),
        "round_number": next_round,
        "questions": [model_dict(q) for q in created],
        "duplicates_dropped": duplicates_dropped,
    }


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
    """Distill answered questions + analysis artifacts into an insight draft.

    The four-section contract applies; evidence is post-normalized so every
    claim cites a real interview question or analysis artifact of this project,
    with a deterministic fallback when the model cites nothing usable.
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
            "你是产品分析助手。把给定的采访问答（interview_answer 产物）与数据结论（分析产物）蒸馏成洞察草稿："
            "facts（有依据的事实）、hypotheses（待验证的假设）、recommendations（下一步建议）。"
            "每节最多 4 条；每条 text 不超过 80 字；每条的 evidence 只引 1 个最相关的 id；limitations 最多 3 条。"
            "每条必须带 evidence 数组，每项必须是 {\"type\": \"...\", \"id\": \"...\"} 对象，type 取 interview_question（采访问答）或 analysis_artifact（分析产物）。"
            "不要臆测未提供的信息。输出默认是 draft。"
        ),
        context=context,
        flag_name="insight_suggestions_enabled",
    )
    result["output"] = _normalize_distill_evidence(result.get("output") or {}, questions, analysis_items)
    return {
        **result,
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
