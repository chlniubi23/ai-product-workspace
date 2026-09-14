from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..config import settings
from ..db import get_db
from ..models import DataColumn, Dataset, DatasetVersion, User, WorkspaceMember, now
from ..schemas import DatasetDeleteRequest, SchemaPatch
from ..services.access import _dataset_version_for, _ensure_project_active, membership, project_for
from ..services.audit import audit
from ..services.datasets import (
    _json_records,
    _read_dataframe,
    _reject_unsupported_upload,
    _safe_name,
    _version_payload,
)
from ..services.job_handlers import _job, _job_payload, job_executor

router = APIRouter()




@router.get("/api/v1/datasets")
def list_datasets(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(Dataset).where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None)).order_by(Dataset.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Dataset).where(Dataset.workspace_id.in_(workspace_ids), Dataset.deleted_at.is_(None)).order_by(Dataset.created_at.desc())).all() if workspace_ids else []
    # The quality report rides along so the web list page can render real
    # stats without fanning out one request per version (batch 11).
    payload = [model_dict(row, {"versions": [_version_payload(version, {"columns": None, "quality_report": model_dict(version.quality_report) if version.quality_report else None}) for version in row.versions]}) for row in rows]
    return paged(payload, *pagination, len(payload))


@router.post("/api/v1/datasets/upload")
async def upload_dataset(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    dataset_name: str | None = Form(default=None),
    worksheet_name: str | None = Form(default=None),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    # 批 32：文件名保真 —— 展示字段（Dataset.name / DatasetVersion.file_name）存
    # 客户端原始名（含扩展名），磁盘路径仍用 _safe_name 生成的安全名；两者从
    # 此分离，中文名不再被清洗成下划线。
    raw_name = file.filename or "upload.csv"
    filename = _safe_name(raw_name)
    _reject_unsupported_upload(raw_name)
    content = await file.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise error("FILE_TOO_LARGE", f"File exceeds {settings.max_upload_size_mb} MB", 413)
    upload_path = settings.data_path / "uploads" / f"{uuid4().hex}_{filename}"
    upload_path.write_bytes(content)
    # Re-uploading under an existing dataset name appends an immutable new
    # version instead of forking a parallel dataset (BUG-015).
    name = dataset_name or Path(raw_name).stem
    dataset = db.scalar(select(Dataset).where(Dataset.project_id == project.id, Dataset.name == name, Dataset.deleted_at.is_(None)))
    if dataset is None:
        dataset = Dataset(workspace_id=project.workspace_id, project_id=project.id, name=name, source_type="upload", created_by=user.id)
        db.add(dataset)
        db.flush()
        next_version_number = 1
    else:
        membership(db, user, dataset.workspace_id, "editor")
        existing = [v.version_number or 0 for v in dataset.versions]
        next_version_number = (max(existing) if existing else 0) + 1
    relative_path = str(upload_path.relative_to(settings.data_path))
    version = DatasetVersion(dataset_id=dataset.id, version_number=next_version_number, storage_path=relative_path, file_name=raw_name, file_size_bytes=len(content), row_count=0, column_count=0, schema_json={"columns": []}, status="processing", fingerprint=hashlib.sha256(content).hexdigest())
    db.add(version)
    db.flush()
    job = _job(db, project.workspace_id, "dataset_parse", {"dataset_id": dataset.id, "dataset_version_id": version.id, "file_name": raw_name, "worksheet_name": worksheet_name, "_storage_path": relative_path, "_actor_id": user.id}, result_type="dataset_version", result_id=version.id)
    audit(db, project.workspace_id, user.id, "dataset.parse_queued", "dataset", dataset.id, {"version_id": version.id, "job_id": job.id})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"dataset": model_dict(dataset), "version": _version_payload(version, {"columns": [], "quality_report": None}), "job": _job_payload(job)})


BATCH_UPLOAD_LIMIT = 10


@router.post("/api/v1/datasets/upload-batch")
async def upload_dataset_batch(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Upload several files at once; each becomes (or versions) its own dataset.

    Semantics match the single-file endpoint exactly: the file stem is the
    dataset name, re-uploading under an existing name appends an immutable
    version, and every file gets its own parse job.  A failure on one file does
    not roll the others back -- the response reports per-file outcomes so the
    client can show what landed and what did not.
    """

    project = project_for(db, user, project_id, "editor")
    if not files:
        raise error("VALIDATION_ERROR", "At least one file is required", 400)
    if len(files) > BATCH_UPLOAD_LIMIT:
        raise error("VALIDATION_ERROR", f"At most {BATCH_UPLOAD_LIMIT} files per batch", 400)

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    accepted: list[tuple[bytes, str, str, Path]] = []
    failures: list[dict[str, Any]] = []
    for item in files:
        # 批 32：同单文件端点 —— raw_name 入库展示，safe 名只用于磁盘路径。
        raw_name = item.filename or "upload.csv"
        filename = _safe_name(raw_name)
        try:
            _reject_unsupported_upload(raw_name)
            content = await item.read()
            if len(content) > max_bytes:
                raise error("FILE_TOO_LARGE", f"{raw_name} exceeds {settings.max_upload_size_mb} MB", 413)
            if not content:
                raise error("VALIDATION_ERROR", f"{raw_name} is empty", 400)
            upload_path = settings.data_path / "uploads" / f"{uuid4().hex}_{filename}"
            accepted.append((content, raw_name, filename, upload_path))
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
            failures.append({"file_name": raw_name, "code": detail.get("code"), "message": detail.get("message")})

    results: list[dict[str, Any]] = []
    for content, raw_name, _storage_name, upload_path in accepted:
        upload_path.write_bytes(content)
        name = Path(raw_name).stem
        dataset = db.scalar(select(Dataset).where(Dataset.project_id == project.id, Dataset.name == name, Dataset.deleted_at.is_(None)))
        if dataset is None:
            dataset = Dataset(workspace_id=project.workspace_id, project_id=project.id, name=name, source_type="upload", created_by=user.id)
            db.add(dataset)
            db.flush()
        # Compute the next version number from the database rather than the
        # relationship collection: two same-named files in one batch would
        # otherwise both read the stale collection and collide on
        # (dataset_id, version_number) at commit -- a 500 with the whole batch
        # rolled back.
        next_version_number = (
            db.scalar(select(func.max(DatasetVersion.version_number)).where(DatasetVersion.dataset_id == dataset.id)) or 0
        ) + 1
        relative_path = str(upload_path.relative_to(settings.data_path))
        version = DatasetVersion(dataset_id=dataset.id, version_number=next_version_number, storage_path=relative_path, file_name=raw_name, file_size_bytes=len(content), row_count=0, column_count=0, schema_json={"columns": []}, status="processing", fingerprint=hashlib.sha256(content).hexdigest())
        db.add(version)
        db.flush()
        job = _job(db, project.workspace_id, "dataset_parse", {"dataset_id": dataset.id, "dataset_version_id": version.id, "file_name": raw_name, "_storage_path": relative_path, "_actor_id": user.id}, result_type="dataset_version", result_id=version.id)
        # Flush so job.id is assigned before the payload below serializes it;
        # the single-file endpoint reads it after commit instead.
        db.flush()
        results.append({"file_name": raw_name, "dataset": model_dict(dataset), "version": _version_payload(version, {"columns": [], "quality_report": None}), "job": _job_payload(job)})

    audit(db, project.workspace_id, user.id, "dataset.batch_upload_queued", "project", project.id, {"accepted": len(results), "rejected": len(failures), "file_names": [row["file_name"] for row in results]})
    db.commit()
    for row in results:
        job_executor.schedule(background_tasks, row["job"]["id"])
    return ok({"project_id": project.id, "uploads": results, "failures": failures})



@router.get("/api/v1/datasets/{dataset_id}")
def get_dataset(dataset_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    project_for(db, user, dataset.project_id)
    return ok(model_dict(dataset, {"versions": [_version_payload(version, {"quality_report": model_dict(version.quality_report) if version.quality_report else None, "columns": [model_dict(column) for column in version.columns]}) for version in dataset.versions]}))


@router.get("/api/v1/datasets/{dataset_id}/versions")
def list_dataset_versions(dataset_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    project_for(db, user, dataset.project_id)
    versions = sorted(dataset.versions, key=lambda item: item.version_number or 0)
    payload = [_version_payload(version, {"columns": None}) for version in versions]
    return ok(payload, page=1, page_size=len(payload), total=len(payload))


@router.get("/api/v1/dataset-versions/{version_id}")
def get_dataset_version(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, _ = _dataset_version_for(db, user, version_id)
    return ok(_version_payload(version, {"columns": [model_dict(column) for column in version.columns], "quality_report": model_dict(version.quality_report) if version.quality_report else None}))


def _dataset_schema_payload(version: DatasetVersion, dataset: Dataset) -> dict[str, Any]:
    columns = [model_dict(column) for column in version.columns]
    return {
        "dataset_id": dataset.id,
        "dataset_version_id": version.id,
        "status": version.status,
        "columns": columns,
        "available_columns": [str(column["name"]) for column in columns],
    }


@router.get("/api/v1/dataset-versions/{version_id}/schema")
def get_dataset_version_schema(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, dataset, _ = _dataset_version_for(db, user, version_id)
    return ok(_dataset_schema_payload(version, dataset))


@router.get("/api/v1/datasets/{dataset_id}/versions/{version_id}/schema")
def get_dataset_schema(dataset_id: str, version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, dataset, _ = _dataset_version_for(db, user, version_id)
    if dataset.id != dataset_id:
        raise error("NOT_FOUND", "Dataset version does not belong to this dataset", 404)
    return ok(_dataset_schema_payload(version, dataset))


@router.get("/api/v1/dataset-versions/{version_id}/preview")
def preview_dataset(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    version, _, ds_project = _dataset_version_for(db, user, version_id)
    _ensure_project_active(db, ds_project.id)
    path = settings.data_path / version.storage_path
    try:
        frame = _read_dataframe(path, version.file_name)
    except Exception as exc:
        raise error("VALIDATION_ERROR", f"Could not load dataset: {exc}", 400) from exc
    start = (page - 1) * page_size
    return ok({"columns": [model_dict(column) for column in version.columns], "rows": _json_records(frame.iloc[start : start + page_size]), "total": len(frame), "page": page, "page_size": page_size, "dataset_version_id": version.id})


CONFIRMABLE_COLUMN_TYPES = {"string", "integer", "float", "boolean", "datetime", "category"}


@router.patch("/api/v1/dataset-versions/{version_id}/schema")
def patch_schema(version_id: str, body: SchemaPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, project = _dataset_version_for(db, user, version_id, "editor")
    _ensure_project_active(db, project.id)
    columns_by_name = {column.name: column for column in version.columns}
    for item in body.columns:
        name = item.get("name")
        column_id = item.get("id")
        if column_id:
            column = db.get(DataColumn, column_id)
            if column is not None and column.dataset_version_id != version.id:
                raise error("FORBIDDEN", "Column is outside the selected dataset version", 403)
        else:
            column = columns_by_name.get(name)
        if column is None:
            raise error("VALIDATION_ERROR", f"Column '{name or column_id}' does not exist in this dataset version", 400)
        confirmed = item.get("confirmed_type")
        if confirmed is not None and str(confirmed) not in CONFIRMABLE_COLUMN_TYPES:
            raise error("VALIDATION_ERROR", f"confirmed_type must be one of {sorted(CONFIRMABLE_COLUMN_TYPES)}", 400)
        for field in ("display_name", "confirmed_type", "mapping_role", "nullable"):
            if field in item:
                setattr(column, field, item[field])
    version.schema_json = {"columns": [model_dict(column) for column in version.columns]}
    # Editing the schema implies the user looked at the inferred roles, which is
    # what stage 2 actually gates on.
    if version.schema_reviewed_at is None:
        version.schema_reviewed_at = datetime.now(UTC).replace(tzinfo=None)
    audit(db, project.workspace_id, user.id, "dataset.schema_confirmed", "dataset_version", version.id)
    db.commit()
    return ok(_version_payload(version, {"columns": [model_dict(column) for column in version.columns]}))


@router.post("/api/v1/dataset-versions/{version_id}/schema-review")
def mark_schema_reviewed(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Record that a user reviewed the inferred field roles without changing them.

    This is the "I looked, nothing to map" path.  A cleaned business table often
    has no event columns, so requiring a role edit to advance would deadlock the
    pipeline.  Idempotent: reviewing twice keeps the first timestamp.
    """

    version, _, project = _dataset_version_for(db, user, version_id, "editor")
    _ensure_project_active(db, project.id)
    if version.schema_reviewed_at is None:
        version.schema_reviewed_at = datetime.now(UTC).replace(tzinfo=None)
        audit(db, project.workspace_id, user.id, "dataset.schema_reviewed", "dataset_version", version.id)
    db.commit()
    return ok(_version_payload(version, {"columns": [model_dict(column) for column in version.columns]}))


@router.get("/api/v1/dataset-versions/{version_id}/quality-report")
def quality_report(version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version, _, _ = _dataset_version_for(db, user, version_id)
    if version.quality_report is None:
        raise error("NOT_FOUND", "Quality report not found", 404)
    return ok(model_dict(version.quality_report))


@router.delete("/api/v1/datasets/{dataset_id}")
def delete_dataset(
    dataset_id: str,
    body: DatasetDeleteRequest | None = Body(default=None),
    confirm: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    # owner-level hard delete may target an archived project, so use the raw
    # membership check instead of project_for here.
    membership(db, user, dataset.workspace_id, "owner")

    confirmation = body.confirm if body is not None else confirm
    confirmed = confirmation is True
    if isinstance(confirmation, str):
        normalized = confirmation.strip()
        confirmed = normalized == dataset.id or normalized.lower() in {"true", "1", "yes"}
    if not confirmed:
        raise error(
            "CONFIRMATION_REQUIRED",
            "Dataset deletion requires confirm=true or confirm=<dataset_id>",
            409,
            {"dataset_id": dataset_id},
        )
    dataset.deleted_at = now()
    audit(db, dataset.workspace_id, user.id, "dataset.deleted", "dataset", dataset.id, {"confirmed": True})
    db.commit()
    return ok({"id": dataset.id, "deleted": True})
