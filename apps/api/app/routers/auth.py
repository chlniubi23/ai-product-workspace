from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import create_access_token, get_current_user, hash_password, password_needs_rehash, verify_password
from ..common import error, model_dict, ok
from ..db import get_db
from ..models import User, Workspace, WorkspaceMember
from ..schemas import LoginRequest, MeUpdate, RegisterRequest
from ..services.audit import audit, audit_user_workspaces
from ..services.workspace_settings import _workspace_payload

router = APIRouter()




@router.post("/api/v1/auth/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    email = str(body.email).lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise error("VALIDATION_ERROR", "Email is already registered", 400)
    user = User(email=email, name=body.name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    workspace = Workspace(name=body.workspace_name, owner_id=user.id)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    audit(db, workspace.id, user.id, "auth.registered", "user", user.id)
    db.commit()
    token = create_access_token(user.id)
    return ok({"access_token": token, "token_type": "bearer", "user": model_dict(user), "workspace": _workspace_payload(workspace, "owner")})


@router.post("/api/v1/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.email == str(body.email).lower()))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        if user is not None:
            audit_user_workspaces(db, user.id, "auth.login_failed", "user", user.id, actor_type="anonymous")
            db.commit()
        raise error("UNAUTHENTICATED", "Incorrect email or password", 401)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    audit_user_workspaces(db, user.id, "auth.login_succeeded", "user", user.id)
    db.commit()
    return ok({"access_token": create_access_token(user.id), "token_type": "bearer", "user": model_dict(user)})


@router.post("/api/v1/auth/refresh")
def refresh(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return ok({"access_token": create_access_token(user.id), "token_type": "bearer"})


@router.get("/api/v1/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    workspaces = [db.get(Workspace, row.workspace_id) for row in rows]
    return ok(
        {
            "user": model_dict(user),
            "workspaces": [
                _workspace_payload(ws, row.role)
                for ws, row in zip(workspaces, rows, strict=True)
                if ws is not None
            ],
        }
    )


@router.patch("/api/v1/me")
def update_me(body: MeUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Batch 26 user center: rename and/or password change (any role, self only).

    Password change verifies the current password first; existing tokens are
    deliberately NOT revoked -- the project has no server-side revocation
    system (JWTs live until expiry), so old sessions simply keep working.
    """
    if (body.current_password is None) != (body.new_password is None):
        raise error("VALIDATION_ERROR", "current_password and new_password must be provided together", 400)
    changed_name = False
    changed_password = False
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise error("VALIDATION_ERROR", "name cannot be empty", 400)
        if name != user.name:
            user.name = name
            changed_name = True
    if body.new_password is not None:
        if not verify_password(body.current_password, user.password_hash):
            raise error("VALIDATION_ERROR", "当前密码不正确", 400)
        try:
            user.password_hash = hash_password(body.new_password)
        except ValueError as exc:
            raise error("VALIDATION_ERROR", str(exc), 400) from exc
        changed_password = True
    if not (changed_name or changed_password):
        # Nothing to persist (same name, no password fields): return the
        # current profile unchanged rather than writing audit noise.
        return update_me_response(user, db)
    workspace_id = db.scalar(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id).limit(1))
    if changed_name and workspace_id:
        audit(db, workspace_id, user.id, "user.renamed", "user", user.id)
    if changed_password and workspace_id:
        audit(db, workspace_id, user.id, "user.password_changed", "user", user.id)
    db.commit()
    return update_me_response(user, db)


def update_me_response(user: User, db: Session) -> dict[str, Any]:
    rows = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    workspaces = [db.get(Workspace, row.workspace_id) for row in rows]
    return ok(
        {
            "user": model_dict(user),
            "workspaces": [
                _workspace_payload(ws, row.role)
                for ws, row in zip(workspaces, rows, strict=True)
                if ws is not None
            ],
        }
    )
