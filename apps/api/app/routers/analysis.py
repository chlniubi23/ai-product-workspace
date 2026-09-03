from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..db import get_db
from ..models import AnalysisArtifact, AnalysisRun, User, WorkspaceMember
from ..schemas import AnalysisCreate
from ..services.access import _dataset_version_for, _ensure_project_active, membership, project_for
from ..services.analysis_pipeline import (
    _analysis_config_validation,
    _analysis_request_config,
    _field_mapping_error_message,
    _prepare_analysis_run,
)
from ..services.audit import audit
from ..services.job_handlers import _job, _job_payload, job_executor

router = APIRouter()




@router.post("/api/v1/analysis-runs/validate-config")
def validate_analysis_config(body: AnalysisCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, project = _dataset_version_for(db, user, body.dataset_version_id)
    if project.id != body.project_id:
        raise error("FORBIDDEN", "Dataset and project do not match", 403)
    columns = {item.name for item in version.columns}
    config = _analysis_request_config(body)
    validation = _analysis_config_validation(version, body.analysis_type, config)
    errors = validation["errors"]
    missing = [error_message.split(": ", 1)[1] for error_message in errors if error_message.startswith("Missing column: ")]
    result: dict[str, Any] = {
        "valid": not errors,
        "analysis_type": body.analysis_type,
        "field_mapping": validation["field_mapping"],
        "available_columns": sorted(columns),
        "missing_columns": list(dict.fromkeys(missing)),
        "missing_mappings": validation["missing_mappings"],
        "invalid_mappings": validation["invalid_mappings"],
        "errors": errors,
    }
    if validation["missing_mappings"]:
        result["error"] = {
            "code": "FIELD_MAPPING_REQUIRED",
            "message": _field_mapping_error_message(body.analysis_type),
            "missing_mappings": validation["missing_mappings"],
            "available_columns": sorted(columns),
        }
    elif validation["invalid_mappings"] or validation["unknown_mapping_keys"] or validation["mapping_errors"]:
        result["error"] = {
            "code": "FIELD_MAPPING_INVALID",
            "message": "Field mapping contains columns or semantic fields that are not available",
            "invalid_mappings": validation["invalid_mappings"],
            "available_columns": sorted(columns),
        }
    return ok(result)


@router.get("/api/v1/analysis-runs")
def list_analysis_runs(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project.id).order_by(AnalysisRun.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(AnalysisRun).where(AnalysisRun.workspace_id.in_(workspace_ids)).order_by(AnalysisRun.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/analysis-runs")
def create_analysis(body: AnalysisCreate, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, project = _dataset_version_for(db, user, body.dataset_version_id, "editor")
    if project.id != body.project_id:
        raise error("FORBIDDEN", "Dataset and project do not match", 403)
    config = _analysis_request_config(body)
    # A cleaned business table often has no user/event/time columns, so the
    # requested analysis type may simply not apply to it.  That is not a user
    # error: _prepare_analysis_run falls back to a descriptive run rather than
    # blocking the pipeline.  Only missing mappings degrade -- a mapping that
    # points at a nonexistent column is a real mistake and still surfaces as 400.
    run, outcome = _prepare_analysis_run(db, project, version, body.analysis_type, config, user.id)
    if run is None:
        validation = outcome["validation"]
        analysis_type = str(outcome["analysis_type"])
        config_errors = outcome["errors"]
        if outcome["reason"] == "quality":
            raise error("DATASET_NOT_READY", "Quality report requires confirmation before analysis", 422, {"dataset_version_id": version.id, "quality_status": outcome["quality_status"]})
        if outcome["reason"] == "not_ready":
            raise error("DATASET_NOT_READY", "Dataset version is not ready for analysis", 422, {"dataset_version_id": version.id})
        if validation["missing_mappings"]:
            raise error(
                "FIELD_MAPPING_REQUIRED",
                _field_mapping_error_message(analysis_type),
                400,
                {
                    "missing_mappings": validation["missing_mappings"],
                    "available_columns": sorted(column.name for column in version.columns),
                    "errors": config_errors,
                },
            )
        if validation["invalid_mappings"] or validation["unknown_mapping_keys"] or validation["mapping_errors"]:
            raise error(
                "FIELD_MAPPING_INVALID",
                "Field mapping contains columns or semantic fields that are not available",
                400,
                {
                    "invalid_mappings": validation["invalid_mappings"],
                    "available_columns": sorted(column.name for column in version.columns),
                    "errors": config_errors,
                },
            )
        raise error("VALIDATION_ERROR", "Analysis configuration is invalid", 400, config_errors)
    analysis_type = run.analysis_type
    config = outcome["config"]
    job = _job(db, project.workspace_id, "analysis_run", {"analysis_run_id": run.id, "analysis_type": analysis_type, "dataset_version_id": version.id, "config": config, "field_mapping": outcome["field_mapping"], "_actor_id": user.id}, result_type="analysis_run", result_id=run.id)
    audit(db, project.workspace_id, user.id, "analysis.queued", "analysis_run", run.id, {"analysis_type": analysis_type, "requested_analysis_type": body.analysis_type, "degraded_from": outcome["degraded_from"], "job_id": job.id})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"analysis_run": model_dict(run), "artifacts": [], "job": _job_payload(job)})


@router.get("/api/v1/analysis-runs/{run_id}")
def get_analysis(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id)
    return ok(model_dict(run, {"artifacts": [model_dict(item) for item in run.artifacts]}))


@router.get("/api/v1/analysis-runs/{run_id}/artifacts")
def list_artifacts(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id)
    return ok([model_dict(item) for item in run.artifacts], page=1, page_size=len(run.artifacts), total=len(run.artifacts))


@router.get("/api/v1/analysis-artifacts/{artifact_id}")
def get_artifact(artifact_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    artifact = db.get(AnalysisArtifact, artifact_id)
    if artifact is None:
        raise error("NOT_FOUND", "Analysis artifact not found", 404)
    run = db.get(AnalysisRun, artifact.analysis_run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id)
    return ok(model_dict(artifact))


@router.post("/api/v1/analysis-runs/{run_id}/rerun")
def rerun_analysis(run_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Analysis run not found", 404)
    membership(db, user, run.workspace_id, "editor")
    _ensure_project_active(db, run.project_id)
    body = AnalysisCreate(project_id=run.project_id, dataset_version_id=run.dataset_version_id, analysis_type=run.analysis_type, config=run.config_json or {})
    return create_analysis(body, background_tasks, user, db)
