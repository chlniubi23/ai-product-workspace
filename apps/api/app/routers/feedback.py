from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import build_ai_context
from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged, serialize
from ..config import settings
from ..db import get_db
from ..models import (
    DatasetVersion,
    FeedbackCluster,
    FeedbackItem,
    FeedbackNote,
    Job,
    Project,
    Task,
    User,
    WorkspaceMember,
)
from ..schemas import (
    AIClusterFeedbackRequest,
    FeedbackClusterPatch,
    FeedbackCreate,
    FeedbackNoteCreate,
    FeedbackNotePatch,
    FeedbackPatch,
)
from ..services.access import _dataset_version_for, _ensure_project_active, membership, project_for
from ..services.audit import audit
from ..services.datasets import _reject_unsupported_upload, _safe_name
from ..services.job_handlers import _feedback_payload, _job, _job_payload, job_executor

router = APIRouter()




@router.get("/api/v1/feedback-items")
def list_feedback(project_id: str | None = Query(default=None), status: str | None = Query(default=None), channel: str | None = Query(default=None), label: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(FeedbackItem).where(FeedbackItem.project_id == project.id).order_by(FeedbackItem.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(FeedbackItem).where(FeedbackItem.workspace_id.in_(workspace_ids)).order_by(FeedbackItem.created_at.desc())).all() if workspace_ids else []
    if status:
        rows = [row for row in rows if row.status == status]
    if channel:
        rows = [row for row in rows if row.channel == channel]
    if label:
        rows = [row for row in rows if label in (row.labels_json or [])]
    return paged([_feedback_payload(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/feedback-items")
def create_feedback(body: FeedbackCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    item = FeedbackItem(workspace_id=project.workspace_id, project_id=project.id, content=body.content, external_ref=body.external_ref, user_ref=body.user_ref, channel=body.channel, rating=body.rating, feedback_at=body.feedback_at, labels_json=body.labels, status=body.status)
    db.add(item)
    db.flush()
    db.add(FeedbackNote(project_id=project.id, content=body.content, source=body.channel or "manual", label=(body.labels or [""])[0][:120], sentiment="unknown"))
    audit(db, project.workspace_id, user.id, "feedback.created", "feedback_item", item.id)
    db.commit()
    return ok(_feedback_payload(item))


@router.post("/api/v1/feedback-items/import")
async def import_feedback(background_tasks: BackgroundTasks, project_id: str = Form(...), file: UploadFile = File(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    filename = _safe_name(file.filename or "feedback.csv")
    _reject_unsupported_upload(filename)
    content = await file.read()
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise error("FILE_TOO_LARGE", "Feedback file is too large", 413)
    path = settings.data_path / "uploads" / f"{uuid4().hex}_{filename}"
    path.write_bytes(content)
    relative_path = str(path.relative_to(settings.data_path))
    job = _job(db, project.workspace_id, "feedback_import", {"project_id": project.id, "file_name": filename, "rows": 0, "_storage_path": relative_path, "_actor_id": user.id}, result_type="feedback_items")
    audit(db, project.workspace_id, user.id, "feedback.import_queued", "job", job.id, {"project_id": project.id, "file_name": filename})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"imported": 0, "queued": True, "job": _job_payload(job), "items": []})


@router.get("/api/v1/feedback-imports")
def list_feedback_imports(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    project = project_for(db, user, project_id)
    jobs = db.scalars(
        select(Job)
        .where(Job.workspace_id == project.workspace_id, Job.job_type == "feedback_import")
        .order_by(Job.created_at.desc())
    ).all()
    rows = []
    for job in jobs:
        job_input = job.input_json if isinstance(job.input_json, dict) else {}
        if job_input.get("project_id") != project.id:
            continue
        rows.append({
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "progress": job.progress,
            "current_step": job.current_step,
            "input_json": {"project_id": project.id, "rows": job_input.get("rows", 0)},
            "result_type": job.result_type,
            "attempt_count": job.attempt_count,
            "created_at": serialize(job.created_at),
            "completed_at": serialize(job.completed_at),
        })
    return paged(rows, *pagination, len(rows))


@router.patch("/api/v1/feedback-items/{feedback_id}")
def patch_feedback(feedback_id: str, body: FeedbackPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.get(FeedbackItem, feedback_id)
    if item is None:
        raise error("NOT_FOUND", "Feedback item not found", 404)
    membership(db, user, item.workspace_id, "editor")
    _ensure_project_active(db, item.project_id)
    if body.labels is not None:
        item.labels_json = body.labels
    if body.status is not None:
        item.status = body.status
    if body.project_id is not None:
        project = project_for(db, user, body.project_id, "editor")
        if project.workspace_id != item.workspace_id:
            raise error("FORBIDDEN", "Feedback cannot be moved to another workspace", 403)
        item.project_id = project.id
    audit(db, item.workspace_id, user.id, "feedback.updated", "feedback_item", item.id)
    db.commit()
    return ok(_feedback_payload(item))


@router.get("/api/v1/feedback-clusters")
def list_feedback_clusters(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(FeedbackCluster).where(FeedbackCluster.project_id == project.id).order_by(FeedbackCluster.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(FeedbackCluster).where(FeedbackCluster.workspace_id.in_(workspace_ids)).order_by(FeedbackCluster.created_at.desc())).all() if workspace_ids else []
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/feedback-clusters/generate")
def generate_feedback_clusters(project_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    job = _job(db, project.workspace_id, "feedback_cluster_generation", {"project_id": project.id, "_actor_id": user.id}, result_type="feedback_clusters")
    audit(db, project.workspace_id, user.id, "feedback.clusters_queued", "job", job.id, {"project_id": project.id})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"clusters": [], "queued": True, "job": _job_payload(job)})


@router.patch("/api/v1/feedback-clusters/{cluster_id}")
def patch_feedback_cluster(cluster_id: str, body: FeedbackClusterPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    cluster = db.get(FeedbackCluster, cluster_id)
    if cluster is None:
        raise error("NOT_FOUND", "Feedback cluster not found", 404)
    membership(db, user, cluster.workspace_id, "editor")
    if body.name is not None:
        cluster.name = body.name
    if body.summary is not None:
        cluster.summary = body.summary
    if body.status is not None:
        cluster.status = body.status
    db.commit()
    return ok(model_dict(cluster))


@router.post("/api/v1/feedback-clusters/{cluster_id}/link-task")
def link_cluster_task(cluster_id: str, task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    cluster = db.get(FeedbackCluster, cluster_id)
    task = db.get(Task, task_id)
    if cluster is None or task is None or cluster.workspace_id != task.workspace_id or cluster.project_id != task.project_id:
        raise error("NOT_FOUND", "Feedback cluster or task not found", 404)
    membership(db, user, cluster.workspace_id, "editor")
    evidence = list(cluster.evidence_json or [])
    evidence.append({"type": "task", "id": task.id, "title": task.title})
    cluster.evidence_json = evidence
    db.commit()
    return ok(model_dict(cluster))


def _feedback_note_payload(note: FeedbackNote) -> dict[str, Any]:
    """Return the stable V1.1 feedback contract without legacy fields."""

    return {
        "id": note.id,
        "project_id": note.project_id,
        "dataset_version_id": note.dataset_version_id,
        "content": note.content,
        "source": note.source,
        "label": note.label,
        "sentiment": note.sentiment,
        "cluster_name": note.cluster_name,
        "created_at": serialize(note.created_at),
    }


@router.get("/api/v1/feedback-notes")
def list_feedback_notes(
    project_id: str | None = Query(default=None),
    dataset_version_id: str | None = Query(default=None),
    label: str | None = Query(default=None),
    sentiment: str | None = Query(default=None),
    cluster_name: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pagination: tuple[int, int] = Depends(page_params),
) -> dict[str, Any]:
    """List the normalized feedback notes used by V1.1 context builders."""

    query = select(FeedbackNote).order_by(FeedbackNote.created_at.desc())
    if project_id:
        project = project_for(db, user, project_id)
        query = query.where(FeedbackNote.project_id == project.id)
    else:
        # A note without a project is allowed for staging, but it is only
        # visible to an authenticated user when explicitly addressed by id.
        workspace_ids = list(db.scalars(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)).all())
        project_ids = list(db.scalars(select(Project.id).where(Project.workspace_id.in_(workspace_ids))).all()) if workspace_ids else []
        query = query.where(FeedbackNote.project_id.in_(project_ids)) if project_ids else query.where(False)
    if dataset_version_id:
        query = query.where(FeedbackNote.dataset_version_id == dataset_version_id)
    if label:
        query = query.where(FeedbackNote.label == label)
    if sentiment:
        query = query.where(FeedbackNote.sentiment == sentiment)
    if cluster_name:
        query = query.where(FeedbackNote.cluster_name == cluster_name)
    rows = db.scalars(query).all()
    return paged([_feedback_note_payload(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/feedback-notes")
def create_feedback_note(body: FeedbackNoteCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor") if body.project_id else None
    if body.dataset_version_id:
        version, _dataset, version_project = _dataset_version_for(db, user, body.dataset_version_id, "editor")
        if project is not None and version_project.id != project.id:
            raise error("FORBIDDEN", "Dataset version is outside the selected project", 403)
        if project is None:
            project = version_project
    note = FeedbackNote(project_id=project.id if project else None, dataset_version_id=body.dataset_version_id, content=body.content.strip(), source=body.source.strip(), label=body.label.strip(), sentiment=body.sentiment.strip() or "unknown", cluster_name=body.cluster_name.strip() if body.cluster_name else None)
    db.add(note)
    if project:
        audit(db, project.workspace_id, user.id, "feedback_note.created", "feedback_note", note.id)
    db.commit()
    return ok(_feedback_note_payload(note))


@router.patch("/api/v1/feedback-notes/{note_id}")
def patch_feedback_note(note_id: str, body: FeedbackNotePatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    note = db.get(FeedbackNote, note_id)
    if note is None:
        raise error("NOT_FOUND", "Feedback note not found", 404)
    project = project_for(db, user, note.project_id, "editor") if note.project_id else None
    if body.project_id is not None:
        target = project_for(db, user, body.project_id, "editor")
        note.project_id = target.id
        project = target
    if body.label is not None:
        note.label = body.label.strip()
    if body.sentiment is not None:
        note.sentiment = body.sentiment.strip() or "unknown"
    if body.cluster_name is not None:
        note.cluster_name = body.cluster_name.strip() or None
    if project:
        audit(db, project.workspace_id, user.id, "feedback_note.updated", "feedback_note", note.id)
    db.commit()
    return ok(_feedback_note_payload(note))




@router.post("/api/v1/ai/cluster-feedback")
def ai_cluster_feedback(body: AIClusterFeedbackRequest, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue deterministic feedback grouping; every resulting cluster stays draft."""

    project = project_for(db, user, body.project_id, "editor")
    version: DatasetVersion | None = None
    if body.dataset_version_id:
        version, _dataset, version_project = _dataset_version_for(db, user, body.dataset_version_id, "viewer")
        if version_project.id != project.id:
            raise error("FORBIDDEN", "Dataset version is outside the selected project", 403)
    # Validate and discard caller context so raw feedback text cannot be copied
    # into a job or an AI run. Clustering itself uses the existing deterministic
    # worker and writes status='draft'.
    _ = build_ai_context(
        body.context,
        project=project,
        schema=[model_dict(column) for column in version.columns] if version is not None else None,
        quality=version.quality_report if version is not None else None,
        question="反馈主题聚类",
    )
    return generate_feedback_clusters(body.project_id, background_tasks, user, db)
