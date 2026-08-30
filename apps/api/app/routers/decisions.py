from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..db import get_db
from ..models import ApprovalRequest, DecisionProposal, User, WorkspaceMember, now
from ..schemas import ApprovalDecision, DecisionCreate, DecisionPatch
from ..services.access import _task_for_project, membership, project_for
from ..services.audit import audit
from ..services.evidence import _check_evidence_scope

router = APIRouter()




@router.get("/api/v1/decision-proposals")
def list_decisions(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(DecisionProposal).where(DecisionProposal.project_id == project.id).order_by(DecisionProposal.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(DecisionProposal).where(DecisionProposal.workspace_id.in_(workspace_ids)).order_by(DecisionProposal.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/decision-proposals")
def create_decision(body: DecisionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    _task_for_project(db, project, body.task_id)
    _check_evidence_scope(db, project.workspace_id, body.evidence, project.id)
    proposal = DecisionProposal(workspace_id=project.workspace_id, project_id=project.id, task_id=body.task_id, title=body.title, problem_statement=body.problem_statement, proposed_action=body.proposed_action, expected_impact=body.expected_impact, risk_summary=body.risk_summary, validation_plan=body.validation_plan, priority=body.priority, evidence_json=body.evidence, status="draft", created_by=user.id)
    db.add(proposal)
    db.flush()
    audit(db, project.workspace_id, user.id, "decision.created", "decision_proposal", proposal.id)
    db.commit()
    return ok(model_dict(proposal))


@router.get("/api/v1/decision-proposals/{proposal_id}")
def get_decision(proposal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    proposal = db.get(DecisionProposal, proposal_id)
    if proposal is None:
        raise error("NOT_FOUND", "Decision proposal not found", 404)
    membership(db, user, proposal.workspace_id)
    return ok(model_dict(proposal))


@router.patch("/api/v1/decision-proposals/{proposal_id}")
def patch_decision(proposal_id: str, body: DecisionPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    proposal = db.get(DecisionProposal, proposal_id)
    if proposal is None:
        raise error("NOT_FOUND", "Decision proposal not found", 404)
    membership(db, user, proposal.workspace_id, "editor")
    if body.version is not None and body.version != proposal.version:
        raise error("VERSION_CONFLICT", "Proposal version is stale", 409)
    if body.evidence is not None:
        _check_evidence_scope(db, proposal.workspace_id, body.evidence, proposal.project_id)
    for field, column in (("title", "title"), ("problem_statement", "problem_statement"), ("proposed_action", "proposed_action"), ("expected_impact", "expected_impact"), ("risk_summary", "risk_summary"), ("validation_plan", "validation_plan"), ("priority", "priority"), ("evidence", "evidence_json")):
        value = getattr(body, field)
        if value is not None:
            setattr(proposal, column, value)
    proposal.version += 1
    audit(db, proposal.workspace_id, user.id, "decision.updated", "decision_proposal", proposal.id, {"version": proposal.version})
    db.commit()
    return ok(model_dict(proposal))


@router.post("/api/v1/decision-proposals/{proposal_id}/submit")
def submit_decision(proposal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    proposal = db.get(DecisionProposal, proposal_id)
    if proposal is None:
        raise error("NOT_FOUND", "Decision proposal not found", 404)
    membership(db, user, proposal.workspace_id, "editor")
    if proposal.status not in {"draft", "rejected"}:
        raise error("INVALID_STATE", "Proposal cannot be submitted in current state", 409)
    proposal.status = "pending_approval"
    request = ApprovalRequest(workspace_id=proposal.workspace_id, target_type="decision_proposal", target_id=proposal.id, action_type="approve_decision", requested_by=user.id, version=proposal.version)
    db.add(request)
    audit(db, proposal.workspace_id, user.id, "decision.submitted", "decision_proposal", proposal.id)
    db.commit()
    return ok({"proposal": model_dict(proposal), "approval_request": model_dict(request)})


@router.get("/api/v1/approval-requests")
def list_approvals(user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
    rows = db.scalars(select(ApprovalRequest).where(ApprovalRequest.workspace_id.in_(workspace_ids), ApprovalRequest.status == "pending").order_by(ApprovalRequest.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


def _decide_approval(approval_id: str, body: ApprovalDecision, user: User, db: Session, decision: str) -> dict[str, Any]:
    request = db.get(ApprovalRequest, approval_id)
    if request is None:
        raise error("NOT_FOUND", "Approval request not found", 404)
    # Editors can process approvals inside a workspace; Viewer remains read-only.
    membership(db, user, request.workspace_id, "editor")
    if request.status != "pending":
        raise error("INVALID_STATE", "Approval request is already decided", 409)
    if request.version != body.version:
        raise error("VERSION_CONFLICT", "Approval version is stale", 409)
    request.status = decision
    request.decided_by = user.id
    request.decision_note = body.decision_note
    request.decided_at = now()
    proposal = db.get(DecisionProposal, request.target_id) if request.target_type == "decision_proposal" else None
    if proposal is not None and proposal.version != request.version:
        raise error("VERSION_CONFLICT", "Decision proposal changed after approval request", 409)
    if proposal:
        proposal.status = "approved" if decision == "approved" else "rejected"
    audit(db, request.workspace_id, user.id, f"approval.{decision}", request.target_type, request.target_id, {"note": body.decision_note})
    db.commit()
    return ok({"approval_request": model_dict(request), "target": model_dict(proposal) if proposal else None})


@router.post("/api/v1/approval-requests/{approval_id}/approve")
def approve(approval_id: str, body: ApprovalDecision, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _decide_approval(approval_id, body, user, db, "approved")


@router.post("/api/v1/approval-requests/{approval_id}/reject")
def reject(approval_id: str, body: ApprovalDecision, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not body.decision_note.strip():
        raise error("VALIDATION_ERROR", "Rejection reason is required", 400)
    return _decide_approval(approval_id, body, user, db, "rejected")
