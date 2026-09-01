"""Stage-6 AI interview routes: rounds, answers/skips, manual supplements, and
the stage-7 distillation entry."""

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
    create_manual_question,
    distill_interview,
    generate_interview_round,
    update_interview_question,
)

router = APIRouter()


@router.post("/api/v1/projects/{project_id}/interview/rounds")
async def generate_round(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """One AI interview round: 3-5 grounded questions, deduped server-side.

    Degrades to ``status="not_configured"`` with an empty question list when no
    provider key is configured -- the interview continues with manual rows.
    """

    project = project_for(db, user, project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    result = await generate_interview_round(db=db, user=user, workspace=workspace, project=project)
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
    a four-section insight draft.  Nothing is persisted to ``insights`` until
    the user saves a claim back through POST /api/v1/insights."""

    project = project_for(db, user, body.project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    result = await distill_interview(db=db, user=user, workspace=workspace, project=project)
    return ok({"provider": "deepseek", "draft": True, **result})
