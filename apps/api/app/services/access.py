from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common import error
from ..models import Dataset, DatasetVersion, ProductProblem, Project, Task, User, Workspace, WorkspaceMember

# One project = one workflow.  An archived project is read-only history: the
# status lives on the Project row (batch 9) and every editor+ mutation path
# must refuse it.  Defined here (not in the router) because project_for, the
# problem locator and the per-route guards below all share it.
ARCHIVED_PROJECT_STATUS = "archived"


def _ensure_project_active(db: Session, project_id: str | None) -> None:
    """Refuse editor+ mutations against an archived project (409).

    Callers that resolve their resource by db.get + membership (instead of
    project_for) use this to close the archive bypass; reads are unaffected.
    """

    if not project_id:
        return
    project = db.get(Project, project_id)
    if project is not None and project.status == ARCHIVED_PROJECT_STATUS:
        raise error("PROJECT_ARCHIVED", "项目已归档，先恢复后再编辑", 409)


def membership(db: Session, user: User, workspace_id: str, minimum: str = "viewer") -> WorkspaceMember:
    member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id))
    if member is None:
        raise error("FORBIDDEN", "Workspace access denied", 403)
    ranks = {"viewer": 1, "editor": 2, "owner": 3}
    if ranks.get(member.role, 0) < ranks.get(minimum, 1):
        raise error("FORBIDDEN", f"{minimum} role required", 403)
    return member


def project_for(db: Session, user: User, project_id: str, minimum: str = "viewer") -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise error("NOT_FOUND", "Project not found", 404)
    membership(db, user, project.workspace_id, minimum)
    # Archived projects are read-only history: viewer reads pass through (the
    # history page is built on them), editor+ mutations are refused.  Routes
    # that must mutate an archived project anyway (archive/unarchive, delete)
    # bypass project_for with db.get + membership.
    if minimum in {"editor", "owner"} and project.status == ARCHIVED_PROJECT_STATUS:
        raise error("PROJECT_ARCHIVED", "项目已归档，先恢复后再编辑", 409)
    return project


def _task_for_project(db: Session, project: Project, task_id: str | None) -> Task | None:
    if not task_id:
        return None
    task = db.get(Task, task_id)
    if task is None or task.workspace_id != project.workspace_id or task.project_id != project.id:
        raise error("FORBIDDEN", "Task is outside the selected project", 403)
    return task


def _check_assignee(db: Session, workspace_id: str, assignee_id: str | None) -> None:
    if assignee_id and db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == assignee_id)) is None:
        raise error("VALIDATION_ERROR", "Assignee is not a member of this workspace", 400)


def workspace_for_user(db: Session, user: User, workspace_id: str | None = None) -> Workspace:
    if workspace_id:
        membership(db, user, workspace_id)
        workspace = db.get(Workspace, workspace_id)
    else:
        workspace = db.scalar(select(Workspace).join(WorkspaceMember).where(WorkspaceMember.user_id == user.id).order_by(Workspace.created_at).limit(1))
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    return workspace


def _dataset_version_for(db: Session, user: User, version_id: str, minimum: str = "viewer") -> tuple[DatasetVersion, Dataset, Project]:
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise error("NOT_FOUND", "Dataset version not found", 404)
    dataset = db.get(Dataset, version.dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise error("NOT_FOUND", "Dataset not found", 404)
    project = project_for(db, user, dataset.project_id, minimum)
    return version, dataset, project


def _problem_for(db: Session, user: User, problem_id: str, minimum: str = "viewer") -> ProductProblem:
    problem = db.get(ProductProblem, problem_id)
    if problem is None:
        raise error("NOT_FOUND", "Product problem not found", 404)
    membership(db, user, problem.workspace_id, minimum)
    if minimum in {"editor", "owner"}:
        _ensure_project_active(db, problem.project_id)
    return problem
