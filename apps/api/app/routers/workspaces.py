from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params
from ..db import get_db
from ..models import AuditLog, MetricDefinition, User, Workspace, WorkspaceMember, now
from ..schemas import (
    MemberCreate,
    MemberPatch,
    MetricDefinitionCreate,
    MetricDefinitionPatch,
    WorkspacePatch,
    WorkspaceSettingsPatch,
)
from ..services.access import membership, workspace_for_user
from ..services.audit import audit
from ..services.workspace_settings import (
    _merge_workspace_settings,
    _metric_payload,
    _migrate_legacy_metric_dictionary,
    _workspace_payload,
    _workspace_settings,
)

router = APIRouter()




@router.get("/api/v1/workspaces")
def list_workspaces(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    members = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    rows = []
    for member in members:
        workspace = db.get(Workspace, member.workspace_id)
        if workspace:
            rows.append(_workspace_payload(workspace, member.role))
    return ok(rows, page=1, page_size=len(rows), total=len(rows))


@router.get("/api/v1/audit-logs")
def list_audit_logs(
    workspace_id: str = Query(..., min_length=1, max_length=36),
    target_type: str | None = Query(default=None, min_length=1, max_length=80),
    target_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pagination: tuple[int, int] = Depends(page_params),
) -> dict[str, Any]:
    """List immutable audit events within an authorized workspace boundary."""

    membership(db, user, workspace_id)
    filters = [AuditLog.workspace_id == workspace_id]
    if target_type is not None:
        filters.append(AuditLog.target_type == target_type)
    if target_id is not None:
        filters.append(AuditLog.target_id == target_id)

    page, page_size = pagination
    total = db.scalar(select(func.count()).select_from(AuditLog).where(*filters)) or 0
    rows = db.scalars(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ok([model_dict(row) for row in rows], page=page, page_size=page_size, total=total)


@router.patch("/api/v1/workspaces/{workspace_id}")
def patch_workspace(workspace_id: str, body: WorkspacePatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    member = membership(db, user, workspace_id, "owner")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    changed_fields: list[str] = []
    if body.name is not None:
        workspace_name = body.name.strip()
        if not workspace_name:
            raise error("VALIDATION_ERROR", "Workspace name cannot be blank", 400)
        workspace.name = workspace_name
        changed_fields.append("name")
    if body.settings is not None:
        _, setting_fields = _merge_workspace_settings(workspace, body.settings)
        changed_fields.extend(f"settings.{field}" for field in setting_fields)
    audit(db, workspace_id, user.id, "workspace.updated", "workspace", workspace_id, {"changed_fields": changed_fields})
    db.commit()
    return ok(_workspace_payload(workspace, member.role))


@router.get("/api/v1/workspaces/{workspace_id}/settings")
def get_workspace_settings(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    member = membership(db, user, workspace_id)
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    current = _workspace_settings(workspace)
    # Settings values are exposed both nested and flattened (BUG-022): API
    # clients read the documented top-level fields, the web client reads
    # ``settings``.
    return ok(
        {
            **current,
            "workspace_id": workspace.id,
            "name": workspace.name,
            "role": member.role,
            "can_edit": member.role == "owner",
            "settings": current,
        }
    )


@router.patch("/api/v1/workspaces/{workspace_id}/settings")
def patch_workspace_settings(workspace_id: str, body: WorkspaceSettingsPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    member = membership(db, user, workspace_id, "owner")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    merged, changed_fields = _merge_workspace_settings(workspace, body)
    audit(db, workspace_id, user.id, "workspace.settings_updated", "workspace", workspace_id, {"changed_fields": changed_fields})
    db.commit()
    return ok(
        {
            **merged,
            "workspace_id": workspace.id,
            "name": workspace.name,
            "role": member.role,
            "can_edit": True,
            "settings": merged,
        }
    )


@router.get("/api/v1/workspaces/{workspace_id}/metrics")
def list_metric_definitions(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id)
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    _migrate_legacy_metric_dictionary(db, workspace)
    rows = db.scalars(
        select(MetricDefinition)
        .where(MetricDefinition.workspace_id == workspace_id, MetricDefinition.deleted_at.is_(None))
        .order_by(MetricDefinition.name, MetricDefinition.created_at)
    ).all()
    return ok([_metric_payload(row) for row in rows], page=1, page_size=len(rows), total=len(rows))


@router.post("/api/v1/workspaces/{workspace_id}/metrics")
def create_metric_definition(workspace_id: str, body: MetricDefinitionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    name = body.name.strip()
    definition = body.definition.strip()
    if not name or not definition:
        raise error("VALIDATION_ERROR", "Metric name and definition cannot be blank", 400)
    duplicate = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.workspace_id == workspace_id,
            MetricDefinition.deleted_at.is_(None),
            func.lower(MetricDefinition.name) == name.lower(),
        )
    )
    if duplicate is not None:
        raise error("VALIDATION_ERROR", "A metric with this name already exists", 400)
    metric = MetricDefinition(
        workspace_id=workspace_id,
        name=name,
        definition=definition,
        category=body.category,
        numerator=body.numerator.strip(),
        denominator=body.denominator.strip(),
        unit=body.unit.strip(),
        aggregation_period=body.aggregation_period,
        field_mapping_json=body.field_mapping,
        display_format=body.display_format,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(metric)
    db.flush()
    audit(db, workspace_id, user.id, "metric_definition.created", "metric_definition", metric.id, {"name": metric.name, "category": metric.category})
    db.commit()
    return ok(_metric_payload(metric))


@router.patch("/api/v1/workspaces/{workspace_id}/metrics/{metric_id}")
def patch_metric_definition(workspace_id: str, metric_id: str, body: MetricDefinitionPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            MetricDefinition.workspace_id == workspace_id,
            MetricDefinition.deleted_at.is_(None),
        )
    )
    if metric is None:
        raise error("NOT_FOUND", "Metric definition not found", 404)
    updates = body.model_dump(exclude_unset=True)
    if "name" in updates:
        name = str(updates["name"]).strip()
        if not name:
            raise error("VALIDATION_ERROR", "Metric name cannot be blank", 400)
        duplicate = db.scalar(
            select(MetricDefinition).where(
                MetricDefinition.workspace_id == workspace_id,
                MetricDefinition.id != metric.id,
                MetricDefinition.deleted_at.is_(None),
                func.lower(MetricDefinition.name) == name.lower(),
            )
        )
        if duplicate is not None:
            raise error("VALIDATION_ERROR", "A metric with this name already exists", 400)
        updates["name"] = name
    for text_field in ("definition", "numerator", "denominator", "unit"):
        if text_field in updates:
            updates[text_field] = str(updates[text_field]).strip()
    if "definition" in updates and not updates["definition"]:
        raise error("VALIDATION_ERROR", "Metric definition cannot be blank", 400)
    if "field_mapping" in updates:
        updates["field_mapping_json"] = updates.pop("field_mapping")
    for field, value in updates.items():
        setattr(metric, field, value)
    metric.updated_by = user.id
    metric.updated_at = now()
    audit(db, workspace_id, user.id, "metric_definition.updated", "metric_definition", metric.id, {"changed_fields": sorted(updates)})
    db.commit()
    return ok(_metric_payload(metric))


@router.delete("/api/v1/workspaces/{workspace_id}/metrics/{metric_id}")
def delete_metric_definition(workspace_id: str, metric_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    membership(db, user, workspace_id, "owner")
    metric = db.scalar(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            MetricDefinition.workspace_id == workspace_id,
            MetricDefinition.deleted_at.is_(None),
        )
    )
    if metric is None:
        raise error("NOT_FOUND", "Metric definition not found", 404)
    metric.deleted_at = now()
    metric.updated_at = now()
    metric.updated_by = user.id
    audit(db, workspace_id, user.id, "metric_definition.deleted", "metric_definition", metric.id, {"name": metric.name})
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# V1.1 exposes context resources at the top level.  Keep the workspace-scoped
# routes above as compatibility endpoints, while these aliases infer the
# caller's first workspace when ``workspace_id`` is omitted.  An explicit
# query parameter remains available for users who belong to multiple
# workspaces.
@router.get("/api/v1/settings")
def get_settings_alias(
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return get_workspace_settings(workspace.id, user, db)


@router.patch("/api/v1/settings")
def patch_settings_alias(
    body: WorkspaceSettingsPatch,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return patch_workspace_settings(workspace.id, body, user, db)


@router.get("/api/v1/metrics")
def list_metrics_alias(
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pagination: tuple[int, int] = Depends(page_params),
) -> dict[str, Any]:
    # ``list_metric_definitions`` currently returns all rows for the selected
    # workspace.  Preserve that response shape for the new top-level route;
    # pagination is accepted for contract compatibility with other list APIs.
    workspace = workspace_for_user(db, user, workspace_id)
    return list_metric_definitions(workspace.id, user, db)


@router.post("/api/v1/metrics")
def create_metric_alias(
    body: MetricDefinitionCreate,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return create_metric_definition(workspace.id, body, user, db)


@router.patch("/api/v1/metrics/{metric_id}")
def patch_metric_alias(
    metric_id: str,
    body: MetricDefinitionPatch,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = workspace_for_user(db, user, workspace_id)
    return patch_metric_definition(workspace.id, metric_id, body, user, db)


@router.delete("/api/v1/metrics/{metric_id}")
def delete_metric_alias(
    metric_id: str,
    workspace_id: str | None = Query(default=None, min_length=1, max_length=36),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    workspace = workspace_for_user(db, user, workspace_id)
    return delete_metric_definition(workspace.id, metric_id, user, db)


@router.get("/api/v1/workspaces/{workspace_id}/members")
def list_members(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id)
    members = db.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)).all()
    rows = []
    for member in members:
        member_user = db.get(User, member.user_id)
        rows.append({**model_dict(member), "user": model_dict(member_user) if member_user else None})
    return ok(rows, page=1, page_size=len(rows), total=len(rows))


@router.post("/api/v1/workspaces/{workspace_id}/members")
def add_member(workspace_id: str, body: MemberCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    target = db.scalar(select(User).where(User.email == str(body.email).lower()))
    if target is None:
        raise error("NOT_FOUND", "User with this email is not registered", 404)
    if db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == target.id)):
        raise error("VALIDATION_ERROR", "User is already a member", 400)
    member = WorkspaceMember(workspace_id=workspace_id, user_id=target.id, role=body.role)
    db.add(member)
    audit(db, workspace_id, user.id, "workspace.member_added", "user", target.id)
    db.commit()
    return ok({**model_dict(member), "user": model_dict(target)})


@router.patch("/api/v1/workspaces/{workspace_id}/members/{member_id}")
def patch_member(workspace_id: str, member_id: str, body: MemberPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, workspace_id, "owner")
    member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.id == member_id, WorkspaceMember.workspace_id == workspace_id))
    if member is None:
        raise error("NOT_FOUND", "Member not found", 404)
    member.role = body.role
    audit(db, workspace_id, user.id, "workspace.member_role_changed", "user", member.user_id, {"role": body.role})
    db.commit()
    return ok(model_dict(member))
