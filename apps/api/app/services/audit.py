from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, WorkspaceMember


def audit(
    db: Session,
    workspace_id: str,
    actor_id: str | None,
    action: str,
    target_type: str = "",
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    actor_type: str = "user",
) -> None:
    db.add(AuditLog(workspace_id=workspace_id, actor_type=actor_type, actor_id=actor_id, action=action, target_type=target_type, target_id=target_id, detail_json=detail or {}))


def audit_user_workspaces(
    db: Session,
    user_id: str,
    action: str,
    target_type: str = "user",
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    actor_type: str = "user",
) -> None:
    """Write an authentication event to every workspace the user belongs to.

    ``audit_logs`` is intentionally workspace-scoped, so a known user's auth
    event is copied to each of their workspaces. An unknown email has no safe
    workspace to associate with and is therefore not persisted; the caller
    still returns the same generic authentication error in either case.
    """

    workspace_ids = db.scalars(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id)).all()
    for workspace_id in workspace_ids:
        actor_id = user_id if actor_type == "user" else None
        audit(db, workspace_id, actor_id, action, target_type, target_id or user_id, detail, actor_type)
