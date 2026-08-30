from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, ok
from ..db import get_db
from ..models import Job, User
from ..services.access import membership
from ..services.audit import audit
from ..services.job_handlers import _job_payload, job_executor

router = APIRouter()




@router.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise error("NOT_FOUND", "Job not found", 404)
    membership(db, user, job.workspace_id)
    return ok(_job_payload(job))


@router.post("/api/v1/jobs/{job_id}/retry")
def retry_job(job_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise error("NOT_FOUND", "Job not found", 404)
    membership(db, user, job.workspace_id, "editor")
    if job.status not in {"failed", "cancelled"}:
        raise error("INVALID_STATE", "Only failed or cancelled jobs can be retried", 409)
    source = job.input_json if isinstance(job.input_json, dict) else {}
    if not bool(source.get("_retryable", True)) or not job_executor.has_handler(job.job_type):
        raise error("INVALID_STATE", "This job cannot be retried safely", 409)
    job.status = "queued"
    job.progress = 0
    job.current_step = "queued_for_retry"
    job.error_code = None
    job.error_message = None
    job.started_at = None
    job.completed_at = None
    audit(db, job.workspace_id, user.id, "job.retry_queued", "job", job.id, {"job_type": job.job_type})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok(_job_payload(job))


@router.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise error("NOT_FOUND", "Job not found", 404)
    membership(db, user, job.workspace_id, "editor")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise error("INVALID_STATE", "Job is already completed", 409)
    job_executor.mark_cancelled(db, job)
    audit(db, job.workspace_id, user.id, "job.cancelled", "job", job.id)
    db.commit()
    return ok(_job_payload(job))
