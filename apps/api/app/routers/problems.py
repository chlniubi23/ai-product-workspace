from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..db import get_db
from ..models import ProductProblem, Project, SolutionOption, User, WorkspaceMember
from ..schemas import ProblemCreate, ProblemPatch, SolutionCreate, SolutionPatch, SolutionSelect
from ..services.access import _problem_for, membership, project_for
from ..services.audit import audit
from ..services.evidence import _validate_source_insights

router = APIRouter()




@router.get("/api/v1/problems")
def list_problems(project_id: str | None = Query(default=None), status: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    stmt = select(ProductProblem)
    if project_id:
        project = project_for(db, user, project_id)
        stmt = stmt.where(ProductProblem.project_id == project.id)
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        if not workspace_ids:
            return paged([], *pagination, 0)
        stmt = stmt.where(ProductProblem.workspace_id.in_(workspace_ids))
    if status:
        stmt = stmt.where(ProductProblem.status == status)
    rows = db.scalars(stmt.order_by(ProductProblem.created_at.desc())).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/problems")
def create_problem(body: ProblemCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    insight_ids = _validate_source_insights(db, project, body.source_insight_ids)
    if body.status == "confirmed" and not insight_ids:
        raise error("VALIDATION_ERROR", "A confirmed problem requires at least one source insight", 422)
    problem = ProductProblem(
        workspace_id=project.workspace_id,
        project_id=project.id,
        title=body.title,
        statement=body.statement,
        impact_scope=body.impact_scope,
        source_insight_ids=insight_ids,
        status=body.status,
        priority=body.priority,
        created_by=user.id,
    )
    db.add(problem)
    db.flush()
    audit(db, project.workspace_id, user.id, "problem.created", "product_problem", problem.id, {"status": problem.status})
    db.commit()
    return ok(model_dict(problem))


@router.get("/api/v1/problems/{problem_id}")
def get_problem(problem_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id)
    return ok(model_dict(problem, {"solutions": [model_dict(option) for option in problem.solutions]}))


@router.patch("/api/v1/problems/{problem_id}")
def patch_problem(problem_id: str, body: ProblemPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id, "editor")
    project = db.get(Project, problem.project_id)
    if project is None:
        raise error("NOT_FOUND", "Project not found", 404)
    candidate_ids = list(problem.source_insight_ids or [])
    if body.source_insight_ids is not None:
        candidate_ids = _validate_source_insights(db, project, body.source_insight_ids)
        problem.source_insight_ids = candidate_ids
    if body.status == "confirmed" and not candidate_ids:
        raise error("VALIDATION_ERROR", "A confirmed problem requires at least one source insight", 422)
    for field in ("title", "statement", "impact_scope", "priority", "status"):
        value = getattr(body, field)
        if value is not None:
            setattr(problem, field, value)
    audit(db, problem.workspace_id, user.id, "problem.updated", "product_problem", problem.id, {"status": problem.status})
    db.commit()
    return ok(model_dict(problem))


@router.get("/api/v1/problems/{problem_id}/solutions")
def list_solutions(problem_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id)
    rows = db.scalars(select(SolutionOption).where(SolutionOption.problem_id == problem.id).order_by(SolutionOption.created_at)).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/problems/{problem_id}/solutions")
def create_solution(problem_id: str, body: SolutionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    problem = _problem_for(db, user, problem_id, "editor")
    option = SolutionOption(
        workspace_id=problem.workspace_id,
        problem_id=problem.id,
        title=body.title,
        approach=body.approach,
        pros=list(body.pros),
        cons=list(body.cons),
        effort=body.effort,
        created_by=user.id,
    )
    db.add(option)
    db.flush()
    audit(db, problem.workspace_id, user.id, "solution.created", "solution_option", option.id)
    db.commit()
    return ok(model_dict(option))


@router.patch("/api/v1/solutions/{solution_id}")
def patch_solution(solution_id: str, body: SolutionPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    option = db.get(SolutionOption, solution_id)
    if option is None:
        raise error("NOT_FOUND", "Solution option not found", 404)
    membership(db, user, option.workspace_id, "editor")
    if body.status == "selected":
        raise error("VALIDATION_ERROR", "Use POST /api/v1/solutions/{id}/select to choose an option", 422)
    if body.status == "rejected":
        reason = body.reject_reason if body.reject_reason is not None else option.reject_reason
        if not (reason or "").strip():
            raise error("VALIDATION_ERROR", "A rejected option requires reject_reason", 422)
    for field in ("title", "approach", "pros", "cons", "effort", "status", "reject_reason"):
        value = getattr(body, field)
        if value is not None:
            setattr(option, field, value)
    audit(db, option.workspace_id, user.id, "solution.updated", "solution_option", option.id, {"status": option.status})
    db.commit()
    return ok(model_dict(option))


@router.post("/api/v1/solutions/{solution_id}/select")
def select_solution(solution_id: str, body: SolutionSelect, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Choose one option and reject the rest, recording why each lost.

    The losing rationale is mandatory: a decision record that keeps only the
    winner cannot explain itself later.
    """

    option = db.get(SolutionOption, solution_id)
    if option is None:
        raise error("NOT_FOUND", "Solution option not found", 404)
    membership(db, user, option.workspace_id, "editor")
    siblings = db.scalars(select(SolutionOption).where(SolutionOption.problem_id == option.problem_id, SolutionOption.id != option.id)).all()
    missing = [sibling.id for sibling in siblings if not (body.reject_reasons.get(sibling.id) or sibling.reject_reason or "").strip()]
    if missing:
        raise error("VALIDATION_ERROR", "Every non-selected option requires a reject reason", 422, {"missing_reject_reasons": missing})
    option.status = "selected"
    option.reject_reason = ""
    for sibling in siblings:
        sibling.status = "rejected"
        supplied = (body.reject_reasons.get(sibling.id) or "").strip()
        if supplied:
            sibling.reject_reason = supplied
    audit(db, option.workspace_id, user.id, "solution.selected", "solution_option", option.id, {"problem_id": option.problem_id, "rejected": [s.id for s in siblings]})
    db.commit()
    return ok(model_dict(option, {"rejected": [model_dict(s) for s in siblings]}))


@router.get("/api/v1/solutions")
def list_all_solutions(project_id: str | None = Query(default=None), status: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    """Flat solution list across problems, for the stage-10 gate.

    The nested route under a problem stays the canonical way to read one
    problem's options; this exists so the workflow header can tell whether any
    option has been selected without walking every problem.
    """

    stmt = select(SolutionOption)
    if project_id:
        project = project_for(db, user, project_id)
        problem_ids = db.scalars(select(ProductProblem.id).where(ProductProblem.project_id == project.id)).all()
        if not problem_ids:
            return paged([], *pagination, 0)
        stmt = stmt.where(SolutionOption.problem_id.in_(problem_ids))
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        if not workspace_ids:
            return paged([], *pagination, 0)
        stmt = stmt.where(SolutionOption.workspace_id.in_(workspace_ids))
    if status:
        stmt = stmt.where(SolutionOption.status == status)
    rows = db.scalars(stmt.order_by(SolutionOption.created_at.desc())).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))
