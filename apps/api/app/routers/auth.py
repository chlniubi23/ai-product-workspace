from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import create_access_token, get_current_user, hash_password, password_needs_rehash, verify_password
from ..common import error, model_dict, ok
from ..db import get_db
from ..models import User, Workspace, WorkspaceMember
from ..schemas import LoginRequest, RegisterRequest
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
