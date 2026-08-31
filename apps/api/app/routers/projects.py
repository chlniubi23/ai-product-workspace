from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged
from ..db import get_db
from ..models import (
    AnalysisArtifact,
    AnalysisRun,
    ApprovalRequest,
    CleaningOperation,
    DataColumn,
    DataQualityReport,
    Dataset,
    DatasetVersion,
    DecisionProposal,
    Document,
    DocumentVersion,
    Insight,
    Project,
    Task,
    TaskLink,
    User,
    WorkspaceMember,
)
from ..schemas import DatasetDeleteRequest, LinkCreate, ProjectCreate, ProjectPatch, TaskCreate, TaskPatch
from ..services.access import _check_assignee, membership, project_for, workspace_for_user
from ..services.audit import audit
from ..services.datasets import _safe_data_file
from ..services.evidence import _linked_resource_scope

router = APIRouter()




# Deleting a project is a soft delete so an accidental delete stays recoverable.
# Defined once here because the list filter, the delete handler, and the restore
# handler must agree on the sentinel.
ARCHIVED_PROJECT_STATUS = "archived"


@router.get("/api/v1/projects")
def list_projects(workspace_id: str | None = Query(default=None), include_archived: bool = Query(default=False), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    page, page_size = pagination
    # DELETE is a hard delete, so nothing lands here by being deleted.  A project
    # can still be archived by hand via PATCH status="archived" to hide finished
    # work from the list; ``include_archived`` brings those back into view.
    conditions = [] if include_archived else [Project.status != ARCHIVED_PROJECT_STATUS]
    if workspace_id:
        membership(db, user, workspace_id)
        rows = db.scalars(select(Project).where(Project.workspace_id == workspace_id, *conditions).order_by(Project.created_at.desc())).all()
    else:
        ids = [m.workspace_id for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()]
        rows = db.scalars(select(Project).where(Project.workspace_id.in_(ids), *conditions).order_by(Project.created_at.desc())).all() if ids else []
    return paged([model_dict(row) for row in rows], page, page_size, len(rows))


@router.post("/api/v1/projects")
def create_project(body: ProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, body.workspace_id)
    membership(db, user, workspace.id, "editor")
    project = Project(workspace_id=workspace.id, owner_id=user.id, name=body.name, description=body.description, status=body.status, goal_statement=body.goal_statement)
    db.add(project)
    db.flush()
    audit(db, workspace.id, user.id, "project.created", "project", project.id)
    db.commit()
    return ok(model_dict(project))


@router.get("/api/v1/projects/{project_id}")
def get_project(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return ok(model_dict(project_for(db, user, project_id)))


@router.patch("/api/v1/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    for field in ("name", "description", "status", "goal_statement"):
        value = getattr(body, field)
        if value is not None:
            setattr(project, field, value)
    audit(db, project.workspace_id, user.id, "project.updated", "project", project.id)
    db.commit()
    return ok(model_dict(project))


def _purge_project(db: Session, project: Project) -> dict[str, int]:
    """Delete a project's rows and uploaded files, returning what was removed.

    SQLite runs with ``PRAGMA foreign_keys=ON`` (db.py:21), so order matters.
    Most children declare ``ondelete="CASCADE"``, but ``AnalysisRun`` points at
    ``dataset_versions.id`` with no ``ondelete`` (models.py:278), so analysis
    rows must go before the versions they reference.  Everything is issued in
    one transaction; the caller commits.
    """

    dataset_ids = list(db.scalars(select(Dataset.id).where(Dataset.project_id == project.id)).all())
    version_rows = (
        db.execute(
            select(DatasetVersion.id, DatasetVersion.storage_path).where(DatasetVersion.dataset_id.in_(dataset_ids))
        ).all()
        if dataset_ids
        else []
    )
    version_ids = [row[0] for row in version_rows]
    counts = {"datasets": len(dataset_ids), "versions": len(version_ids)}

    if version_ids:
        counts["quality_reports"] = int(
            db.scalar(
                select(func.count())
                .select_from(DataQualityReport)
                .where(DataQualityReport.dataset_version_id.in_(version_ids))
            )
            or 0
        )
        # Analysis runs reference versions without ON DELETE, so clear them first.
        run_ids = list(db.scalars(select(AnalysisRun.id).where(AnalysisRun.dataset_version_id.in_(version_ids))).all())
        if run_ids:
            db.execute(delete(AnalysisArtifact).where(AnalysisArtifact.analysis_run_id.in_(run_ids)))
            db.execute(delete(AnalysisRun).where(AnalysisRun.id.in_(run_ids)))
        db.execute(delete(CleaningOperation).where(CleaningOperation.source_version_id.in_(version_ids)))
        db.execute(delete(CleaningOperation).where(CleaningOperation.result_version_id.in_(version_ids)))
        db.execute(delete(DataQualityReport).where(DataQualityReport.dataset_version_id.in_(version_ids)))
        db.execute(delete(DataColumn).where(DataColumn.dataset_version_id.in_(version_ids)))
        db.execute(delete(DatasetVersion).where(DatasetVersion.id.in_(version_ids)))
    else:
        counts["quality_reports"] = 0

    if dataset_ids:
        db.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))

    # Insight.task_id (models.py:367) and DecisionProposal.task_id
    # (models.py:437) reference tasks.id with no ondelete, while Project.tasks
    # carries an ORM delete-orphan cascade (models.py:128).  db.delete(project)
    # therefore deletes the Task rows while these still point at them, so they
    # must be cleared first or SQLite raises FOREIGN KEY constraint failed.
    db.execute(delete(Insight).where(Insight.project_id == project.id))
    db.execute(delete(DecisionProposal).where(DecisionProposal.project_id == project.id))

    # Approvals are addressed by (target_type, target_id) rather than a foreign
    # key, so no cascade reaches them.
    orphan_targets = {"project": [project.id], "dataset": dataset_ids, "dataset_version": version_ids}
    for target_type, ids in orphan_targets.items():
        if ids:
            db.execute(
                delete(ApprovalRequest).where(
                    ApprovalRequest.target_type == target_type, ApprovalRequest.target_id.in_(ids)
                )
            )

    db.delete(project)
    db.flush()

    removed_files = 0
    for _, storage_path in version_rows:
        if not storage_path:
            continue
        # _safe_data_file refuses any storage_path resolving outside DATA_ROOT
        # (defense against a tampered row turning delete into arbitrary unlink).
        path = _safe_data_file(str(storage_path))
        if path is None:
            continue
        try:
            path.unlink()
            removed_files += 1
        except FileNotFoundError:
            continue
        except OSError:
            # A locked or already-gone file must not abort the delete; the row is
            # gone either way and a stray file is recoverable disk noise.
            continue
    counts["files"] = removed_files
    return counts


@router.delete("/api/v1/projects/{project_id}")
def delete_project(
    project_id: str,
    body: DatasetDeleteRequest | None = Body(default=None),
    confirm: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Permanently delete a project with its datasets, reports, and versions.

    This is a hard delete: uploaded files are unlinked and no restore exists.
    It mirrors the confirmation contract already used by DELETE /datasets/{id}
    (main.py:1939) so an accidental call cannot destroy data.
    """

    project = project_for(db, user, project_id, "owner")
    workspace_id = project.workspace_id
    name = project.name

    confirmation = body.confirm if body is not None else confirm
    confirmed = confirmation is True
    if isinstance(confirmation, str):
        normalized = confirmation.strip()
        confirmed = normalized == project.id or normalized.lower() in {"true", "1", "yes"}
    if not confirmed:
        raise error(
            "CONFIRMATION_REQUIRED",
            "Project deletion is permanent and requires confirm=true or confirm=<project_id>",
            409,
            {"project_id": project_id},
        )

    counts = _purge_project(db, project)
    audit(db, workspace_id, user.id, "project.purged", "project", project_id, {"name": name, "removed": counts})
    db.commit()
    return ok({"id": project_id, "deleted": True, "removed": counts})


@router.get("/api/v1/projects/{project_id}/overview")
def project_overview(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id)
    tasks = db.scalars(select(Task).where(Task.project_id == project.id)).all()
    datasets = db.scalars(select(Dataset).where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None))).all()
    workflow = _project_workflow_status(db, project)
    return ok({"project": model_dict(project), "workflow_status": "analysis_ready" if datasets else "draft", "workflow": workflow, "task_counts": {state: sum(1 for task in tasks if task.status == state) for state in {task.status for task in tasks}}, "tasks": [model_dict(task) for task in tasks[:10]], "datasets": [model_dict(dataset, {"versions": len(dataset.versions)}) for dataset in datasets]})


# V1.1 keeps the old project overview contract but adds a real, metadata-only
# progress calculation for the five-step pipeline.  Keeping this in one helper
# also gives the dedicated workflow-status endpoint and legacy overview exactly
# the same semantics.
# Advisory only.  These roles unlock richer analysis types (trend/funnel/
# retention) when present, but a cleaned business table legitimately has none of
# them, so their absence must never block the pipeline.  This is the single
# source of truth: the frontend reads it from the workflow-status payload rather
# than keeping its own copy.
SUGGESTED_FIELD_ROLES = ("user_id", "event_time", "event_name")


def _project_workflow_status(db: Session, project: Project) -> dict[str, Any]:
    datasets = db.scalars(
        select(Dataset)
        .where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None))
        .order_by(Dataset.created_at.desc())
    ).all()
    versions: list[DatasetVersion] = []
    for dataset in datasets:
        # The latest non-failed version is the version a user sees in the
        # pipeline.  A processing/failed upload must not mark step 1 complete.
        candidates = sorted(dataset.versions, key=lambda item: item.version_number, reverse=True)
        version = next((item for item in candidates if item.status not in {"failed", "error"}), None)
        if version is not None:
            versions.append(version)

    selected_version = versions[0] if versions else None
    confirmed_roles = sorted(
        {
            str(column.mapping_role).strip()
            for column in (selected_version.columns if selected_version else [])
            if column.mapping_role
        }
    )
    # Reported for UI hinting only.  Deliberately not part of `data_complete`.
    missing_roles = [role for role in SUGGESTED_FIELD_ROLES if role not in confirmed_roles]
    # An auto-accepted schema counts as reviewed.  The parse job stamps
    # ``schema_auto_accepted_at`` (see _run_auto_analyses) so a plain upload
    # advances on its own; an explicit human review still stamps
    # ``schema_reviewed_at`` and is reported separately below.
    schema_reviewed = bool(
        selected_version
        and (selected_version.schema_reviewed_at is not None or selected_version.schema_auto_accepted_at is not None)
    )
    schema_reviewed_by_human = bool(selected_version and selected_version.schema_reviewed_at is not None)
    # `status` still gates: a failed parse never advances, auto-accepted or not.
    data_complete = bool(selected_version and selected_version.status in {"ready", "confirmed", "succeeded"} and schema_reviewed)

    quality_complete = bool(selected_version and selected_version.quality_report is not None)
    analysis_complete = db.scalar(
        select(func.count(AnalysisRun.id)).where(
            AnalysisRun.project_id == project.id,
            AnalysisRun.status == "succeeded",
        )
    ) > 0
    insight_complete = db.scalar(
        select(func.count(Insight.id)).where(
            Insight.project_id == project.id,
            Insight.status == "confirmed",
        )
    ) > 0
    delivery_complete = db.scalar(
        select(func.count(DocumentVersion.id))
        .select_from(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.project_id == project.id)
    ) > 0

    step_values = [data_complete, quality_complete, analysis_complete, insight_complete, delivery_complete]
    step_names = ("data", "quality", "analysis", "insights", "delivery")
    steps = [
        {
            "key": key,
            "complete": complete,
            "progress": 100 if complete else 0,
        }
        for key, complete in zip(step_names, step_values, strict=True)
    ]
    completed_steps = sum(step_values)
    next_step = next((key for key, complete in zip(step_names, step_values, strict=True) if not complete), None)
    return {
        "steps": steps,
        "completed_steps": completed_steps,
        "total_steps": len(steps),
        "progress_percent": int(round(completed_steps / len(steps) * 100)),
        "next_step": next_step,
        "selected_dataset_version_id": selected_version.id if selected_version else None,
        "confirmed_roles": confirmed_roles,
        "missing_roles": missing_roles,
        "suggested_roles": list(SUGGESTED_FIELD_ROLES),
        "schema_reviewed": schema_reviewed,
        "schema_reviewed_by_human": schema_reviewed_by_human,
        "schema_auto_accepted": bool(selected_version and selected_version.schema_auto_accepted_at is not None),
    }


@router.get("/api/v1/projects/{project_id}/workflow-status")
def project_workflow_status(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return truthful V1.1 pipeline progress for a project.

    This endpoint is intentionally additive.  It does not expose task rows or
    dataset contents and therefore can be used by the new five-step frontend
    without removing V1.0 routes.
    """

    project = project_for(db, user, project_id)
    return ok({"project_id": project.id, **_project_workflow_status(db, project)})


@router.get("/api/v1/projects/{project_id}/tasks")
def list_tasks(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    project = project_for(db, user, project_id)
    rows = db.scalars(select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())).all()
    return paged([model_dict(row) for row in rows], *pagination, len(rows))


@router.post("/api/v1/projects/{project_id}/tasks")
def create_task(project_id: str, body: TaskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    project = project_for(db, user, project_id, "editor")
    _check_assignee(db, project.workspace_id, body.assignee_id)
    task = Task(workspace_id=project.workspace_id, project_id=project.id, title=body.title, description=body.description, priority=body.priority, status=body.status, assignee_id=body.assignee_id, due_at=body.due_at)
    db.add(task)
    db.flush()
    audit(db, project.workspace_id, user.id, "task.created", "task", task.id)
    db.commit()
    return ok(model_dict(task))


@router.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id)
    return ok(model_dict(task, {"links": [model_dict(link) for link in task.links]}))


@router.patch("/api/v1/tasks/{task_id}")
def patch_task(task_id: str, body: TaskPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    _check_assignee(db, task.workspace_id, body.assignee_id)
    for field in ("title", "description", "priority", "status", "assignee_id", "due_at", "ai_summary"):
        value = getattr(body, field)
        if value is not None:
            setattr(task, field, value)
    audit(db, task.workspace_id, user.id, "task.updated", "task", task.id)
    db.commit()
    return ok(model_dict(task))


@router.delete("/api/v1/tasks/{task_id}")
def delete_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    task.status = "archived"
    audit(db, task.workspace_id, user.id, "task.deleted", "task", task.id)
    db.commit()
    return ok({"id": task.id, "status": task.status})


@router.post("/api/v1/tasks/{task_id}/links")
def link_task(task_id: str, body: LinkCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    target_workspace, target_project = _linked_resource_scope(db, body.link_type, body.target_id)
    if target_workspace != task.workspace_id or (target_project is not None and target_project != task.project_id):
        raise error("FORBIDDEN", "Linked object is outside the task project", 403)
    link_type = body.link_type.strip().lower()
    existing = db.scalar(select(TaskLink).where(TaskLink.task_id == task.id, TaskLink.link_type == link_type, TaskLink.target_id == body.target_id))
    if existing is not None:
        raise error("CONFLICT", "This object is already linked to the task", 409)
    link = TaskLink(task_id=task.id, link_type=link_type, target_id=body.target_id, title=body.title)
    db.add(link)
    audit(db, task.workspace_id, user.id, "task.link_created", "task", task.id, {"link_type": link_type, "target_id": body.target_id})
    db.commit()
    return ok(model_dict(link))


@router.get("/api/v1/tasks/{task_id}/links")
def list_task_links(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id)
    rows = db.scalars(select(TaskLink).where(TaskLink.task_id == task.id).order_by(TaskLink.created_at)).all()
    return ok([model_dict(row) for row in rows])


@router.delete("/api/v1/tasks/{task_id}/links/{link_id}")
def unlink_task(task_id: str, link_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if task is None:
        raise error("NOT_FOUND", "Task not found", 404)
    membership(db, user, task.workspace_id, "editor")
    link = db.scalar(select(TaskLink).where(TaskLink.id == link_id, TaskLink.task_id == task_id))
    if link is None:
        raise error("NOT_FOUND", "Task link not found", 404)
    db.delete(link)
    audit(db, task.workspace_id, user.id, "task.link_deleted", "task", task.id, {"link_id": link_id})
    db.commit()
    return ok({"id": link_id, "deleted": True})
