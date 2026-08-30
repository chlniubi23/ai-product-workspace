from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..db import get_db
from ..models import Document, DocumentVersion, Project, User, Workspace, WorkspaceMember
from ..schemas import DocumentCreate, DocumentGenerate, DocumentVersionCreate
from ..services.access import membership, project_for
from ..services.audit import audit
from ..services.documents import _document_payload, _render_document_markdown
from ..services.evidence import _require_confirmed_insight_refs
from ..services.job_handlers import _job, _job_payload, job_executor
from ..services.workspace_settings import _workspace_settings

router = APIRouter()




@router.get("/api/v1/documents")
def list_documents(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    if project_id:
        project = project_for(db, user, project_id)
        rows = db.scalars(select(Document).where(Document.project_id == project.id).order_by(Document.created_at.desc())).all()
    else:
        workspace_ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Document).where(Document.workspace_id.in_(workspace_ids)).order_by(Document.created_at.desc())).all() if workspace_ids else []
    return paged([_document_payload(row, db) for row in rows], *pagination, len(rows))


@router.post("/api/v1/documents")
def create_document(body: DocumentCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    _require_confirmed_insight_refs(db, project.workspace_id, body.evidence, project.id)
    document = Document(workspace_id=project.workspace_id, project_id=project.id, document_type=body.document_type, title=body.title, status="draft", created_by=user.id)
    db.add(document)
    db.flush()
    version = DocumentVersion(document_id=document.id, version_number=1, content_markdown=body.content_markdown or f"# {body.title}\n\nDraft pending review.", evidence_json=body.evidence, created_by=user.id)
    db.add(version)
    db.flush()
    document.current_version_id = version.id
    audit(db, project.workspace_id, user.id, "document.created", "document", document.id)
    db.commit()
    return ok(_document_payload(document, db))


@router.post("/api/v1/documents/generate")
def generate_document(body: DocumentGenerate, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, body.project_id, "editor")
    workspace = db.get(Workspace, project.workspace_id)
    flags = _workspace_settings(workspace).get("feature_flags", {}) if workspace else {}
    if not bool(flags.get("document_generation_enabled", True)):
        audit(db, project.workspace_id, user.id, "document.feature_disabled", "project", project.id, {"feature": "document_generation"})
        db.commit()
        raise error("AI_FEATURE_DISABLED", "Document generation is disabled for this workspace", 403)
    _require_confirmed_insight_refs(db, project.workspace_id, body.source_refs, project.id)
    markdown, evidence = _render_document_markdown(body, db, user)
    document = Document(workspace_id=project.workspace_id, project_id=project.id, document_type=body.document_type, title=body.title, status="draft", created_by=user.id)
    db.add(document)
    db.flush()
    version = DocumentVersion(document_id=document.id, version_number=1, content_markdown=markdown, evidence_json=evidence, created_by=user.id)
    db.add(version)
    db.flush()
    document.current_version_id = version.id
    job = _job(
        db,
        project.workspace_id,
        "document_generation",
        {
            "document_id": document.id,
            "source_refs": evidence,
            "template_options": body.template_options,
            "document_type": body.document_type,
            "_actor_id": user.id,
        },
        result_type="document",
        result_id=document.id,
    )
    audit(db, project.workspace_id, user.id, "document.generated", "document", document.id, {"source_count": len(evidence)})
    db.commit()
    job_executor.schedule(background_tasks, job.id)
    return ok({"document": _document_payload(document, db), "job": _job_payload(job)})


@router.get("/api/v1/documents/{document_id}")
def get_document(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id)
    return ok(_document_payload(document, db))


@router.get("/api/v1/documents/{document_id}/versions")
def list_document_versions(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id)
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number.desc())).all()
    return ok([model_dict(item) for item in versions], page=1, page_size=len(versions), total=len(versions))


@router.post("/api/v1/documents/{document_id}/versions")
def create_document_version(document_id: str, body: DocumentVersionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id, "editor")
    document_project = db.get(Project, document.project_id)
    _require_confirmed_insight_refs(db, document.workspace_id, body.evidence, document_project.id if document_project else document.project_id)
    latest = db.scalar(select(func.max(DocumentVersion.version_number)).where(DocumentVersion.document_id == document.id)) or 0
    if body.version is not None and body.version != latest + 1:
        raise error("VERSION_CONFLICT", "Document version does not follow latest version", 409)
    version = DocumentVersion(document_id=document.id, version_number=latest + 1, content_markdown=body.content_markdown, evidence_json=body.evidence, created_by=user.id)
    db.add(version)
    db.flush()
    document.current_version_id = version.id
    document.status = "draft"
    audit(db, document.workspace_id, user.id, "document.version_created", "document", document.id, {"version": version.version_number})
    db.commit()
    return ok(model_dict(version))


@router.post("/api/v1/documents/{document_id}/submit")
def submit_document(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id, "editor")
    document.status = "in_review"
    audit(db, document.workspace_id, user.id, "document.submitted", "document", document.id)
    db.commit()
    return ok(_document_payload(document, db))


@router.get("/api/v1/documents/{document_id}/export")
def export_document(document_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    document = db.get(Document, document_id)
    if document is None:
        raise error("NOT_FOUND", "Document not found", 404)
    membership(db, user, document.workspace_id)
    version = db.get(DocumentVersion, document.current_version_id) if document.current_version_id else db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number.desc()))
    if version is None:
        raise error("NOT_FOUND", "Document has no versions", 404)
    audit(db, document.workspace_id, user.id, "document.exported", "document", document.id)
    db.commit()
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", document.title)[:120] or "document"
    return Response(content=version.content_markdown, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}.md"'})


@router.post("/api/v1/ai/draft-document")
def ai_draft_document(body: DocumentGenerate, background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """V1.1 name for the existing draft document generation workflow."""

    return generate_document(body, background_tasks, user, db)
