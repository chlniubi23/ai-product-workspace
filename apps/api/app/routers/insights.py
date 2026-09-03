from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..db import get_db
from ..models import Insight, User, WorkspaceMember
from ..schemas import InsightCreate, InsightPatch
from ..services.access import _ensure_project_active, _task_for_project, membership, project_for
from ..services.audit import audit
from ..services.evidence import _check_evidence_scope, _require_nonempty_evidence

router = APIRouter()




@router.get("/api/v1/insights")
def list_insights(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(Insight).where(Insight.project_id == project.id).order_by(Insight.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Insight).where(Insight.workspace_id.in_(workspace_ids)).order_by(Insight.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/insights")
def create_insight(body: InsightCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    _task_for_project(db, project, body.task_id)
    # Drafts may be started without evidence and completed later.  Confirmed
    # insights are the publication boundary, so direct creation at that status
    # must satisfy the same evidence contract as the PATCH lifecycle route.
    evidence = list(body.evidence or [])
    if body.status == "confirmed":
        evidence = _require_nonempty_evidence(evidence, subject="Confirmed insight")
    elif evidence:
        evidence = _require_nonempty_evidence(evidence)
    _check_evidence_scope(db, project.workspace_id, evidence, project.id)
    insight = Insight(workspace_id=project.workspace_id, project_id=project.id, task_id=body.task_id, title=body.title, insight_type=body.insight_type, content=body.content, confidence=body.confidence, evidence_json=evidence, status=body.status, created_by=user.id)
    db.add(insight)
    db.flush()
    audit(db, project.workspace_id, user.id, "insight.created", "insight", insight.id)
    db.commit()
    return ok(model_dict(insight))


@router.get("/api/v1/insights/{insight_id}")
def get_insight(insight_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise error("NOT_FOUND", "Insight not found", 404)
    membership(db, user, insight.workspace_id)
    return ok(model_dict(insight))


@router.patch("/api/v1/insights/{insight_id}")
def patch_insight(insight_id: str, body: InsightPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise error("NOT_FOUND", "Insight not found", 404)
    membership(db, user, insight.workspace_id, "editor")
    _ensure_project_active(db, insight.project_id)
    candidate_evidence = body.evidence if body.evidence is not None else insight.evidence_json
    if body.status == "confirmed":
        candidate_evidence = _require_nonempty_evidence(candidate_evidence, subject="Confirmed insight")
    elif body.evidence is not None:
        candidate_evidence = _require_nonempty_evidence(candidate_evidence)
    if body.evidence is not None or body.status == "confirmed":
        _check_evidence_scope(db, insight.workspace_id, candidate_evidence, insight.project_id)
    for field, column in (("title", "title"), ("content", "content"), ("confidence", "confidence"), ("evidence", "evidence_json"), ("status", "status")):
        value = getattr(body, field)
        if field == "evidence" and value is not None:
            value = candidate_evidence
        if value is not None:
            setattr(insight, column, value)
    audit(db, insight.workspace_id, user.id, "insight.updated", "insight", insight.id, {"status": insight.status})
    db.commit()
    return ok(model_dict(insight))
