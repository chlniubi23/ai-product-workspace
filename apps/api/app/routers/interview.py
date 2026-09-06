"""Stage-6 AI interview routes: adaptive next-question, completion summary,
answers/skips, manual supplements, and the stage-7 distillation entry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..db import get_db
from ..models import InterviewQuestion, User, Workspace
from ..schemas import AIDistillInterviewRequest, InterviewQuestionCreate, InterviewQuestionPatch
from ..services.access import project_for
from ..services.interview import (
    complete_interview,
    create_manual_question,
    distill_interview,
    generate_next_question,
    update_interview_question,
)

router = APIRouter()


@router.post("/api/v1/projects/{project_id}/interview/next-question")
async def interview_next_question(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Adaptive interview: one question per call, or an AI-judged completion
    (batch 18).  Degrades to ``status="not_configured"`` without a provider --
    the interview continues with manual rows."""

    project = project_for(db, user, project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    result = await generate_next_question(db=db, user=user, workspace=workspace, project=project)
    return ok(result)


@router.post("/api/v1/projects/{project_id}/interview/complete")
async def interview_complete(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Generate (or regenerate, idempotently) the end-of-interview digest.

    Used both when the AI declares the interview complete and when the user
    ends it manually.  Requires at least one answered question."""

    project = project_for(db, user, project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    try:
        result = await complete_interview(db=db, user=user, workspace=workspace, project=project)
    except ValueError as exc:
        raise error("VALIDATION_ERROR", "还没有已回答的采访问题，先回答或跳过至少一问再生成小结", 400) from exc
    return ok(result)


@router.get("/api/v1/interview-questions")
def list_interview_questions(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    stmt = select(InterviewQuestion)
    if project_id:
        project = project_for(db, user, project_id)
        stmt = stmt.where(InterviewQuestion.project_id == project.id)
    else:
        from ..models import WorkspaceMember

        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        if not workspace_ids:
            return paged([], *pagination, 0)
        stmt = stmt.where(InterviewQuestion.workspace_id.in_(workspace_ids))
    rows = db.scalars(stmt.order_by(InterviewQuestion.created_at)).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/interview-questions")
def add_manual_question(body: InterviewQuestionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    question = create_manual_question(db, user, project, topic=body.topic, question_text=body.question_text, answer_text=body.answer_text)
    return ok(question)


@router.patch("/api/v1/interview-questions/{question_id}")
def patch_interview_question(question_id: str, body: InterviewQuestionPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    question = db.get(InterviewQuestion, question_id)
    if question is None:
        raise error("NOT_FOUND", "Interview question not found", 404)
    project_for(db, user, question.project_id, "editor")
    if body.status == "answered" and body.answer_text is None and not (question.answer_text or "").strip():
        raise error("VALIDATION_ERROR", "Answering requires answer_text", 422)
    updated = update_interview_question(db, user, question, answer_text=body.answer_text, status=body.status)
    return ok(updated)


@router.post("/api/v1/ai/distill-interview")
async def ai_distill_interview(body: AIDistillInterviewRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stage 7: distill answered interview questions + analysis artifacts into
    draft insights, persisted server-side (batch 18).  The user adjudicates
    the drafts -- reject/edit/confirm -- through the regular insight routes."""

    project = project_for(db, user, body.project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    result = await distill_interview(db=db, user=user, workspace=workspace, project=project)
    return ok({"provider": "deepseek", "draft": True, **result})
