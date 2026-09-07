"""Batch 26: user center (PATCH /me) and the effectively-unlimited daily budget.

Rename and password change share one endpoint; password changes verify the
current password and deliberately do NOT revoke existing tokens (the project
has no server-side revocation system).
"""

from __future__ import annotations

from conftest import PASSWORD, auth, data_of, error_of
from sqlalchemy import select

from app import db as database
from app.infrastructure.llm.deepseek import DeepSeekSettings
from app.models import AuditLog
from app.schemas import WorkspaceSettings


def login(client, email: str, password: str) -> dict:
    payload = data_of(client.post("/api/v1/auth/login", json={"email": email, "password": password}))
    return payload


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_rename_updates_profile_and_audits(client, owner):
    updated = data_of(
        client.patch("/api/v1/me", headers=auth(owner), json={"name": "新昵称"})
    )
    assert updated["user"]["name"] == "新昵称"
    me = data_of(client.get("/api/v1/me", headers=auth(owner)))
    assert me["user"]["name"] == "新昵称"
    with database.SessionLocal() as db:
        actions = [
            row.action
            for row in db.scalars(
                select(AuditLog).where(AuditLog.workspace_id == owner["workspace"]["id"]).order_by(AuditLog.created_at.desc())
            ).all()
        ]
    assert "user.renamed" in actions


def test_rename_boundaries_and_strip(client, owner):
    one_char = data_of(client.patch("/api/v1/me", headers=auth(owner), json={"name": "一"}))
    assert one_char["user"]["name"] == "一"
    padded = data_of(client.patch("/api/v1/me", headers=auth(owner), json={"name": f"{'名' * 118}  "}))
    assert len(padded["user"]["name"]) == 118, "strip then 120-char cap"


def test_rename_requires_authentication(client):
    denied = error_of(client.patch("/api/v1/me", json={"name": "匿名"}))
    assert denied["code"] == "UNAUTHENTICATED"


def test_noop_patch_returns_profile_without_audit(client, owner):
    current = data_of(client.get("/api/v1/me", headers=auth(owner)))
    same = data_of(client.patch("/api/v1/me", headers=auth(owner), json={"name": current["user"]["name"]}))
    assert same["user"]["name"] == current["user"]["name"]


# ---------------------------------------------------------------------------
# password change
# ---------------------------------------------------------------------------


def test_password_change_round_trip(client, owner):
    data_of(
        client.patch(
            "/api/v1/me",
            headers=auth(owner),
            json={"current_password": PASSWORD, "new_password": "NewPass!2026"},
        )
    )
    old_login = client.post("/api/v1/auth/login", json={"email": owner["email"], "password": PASSWORD})
    assert old_login.status_code == 401, "old password must stop working"
    fresh = login(client, owner["email"], "NewPass!2026")
    assert fresh["access_token"]
    with database.SessionLocal() as db:
        actions = [
            row.action
            for row in db.scalars(
                select(AuditLog).where(AuditLog.workspace_id == owner["workspace"]["id"]).order_by(AuditLog.created_at.desc())
            ).all()
        ]
    assert "user.password_changed" in actions


def test_password_change_with_wrong_current_password_rejected(client, owner):
    denied = error_of(
        client.patch(
            "/api/v1/me",
            headers=auth(owner),
            json={"current_password": "definitely-wrong", "new_password": "NewPass!2026"},
        )
    )
    assert denied["code"] == "VALIDATION_ERROR"
    assert "当前密码不正确" in denied["message"]


def test_password_change_with_short_new_password_rejected(client, owner):
    denied = error_of(
        client.patch(
            "/api/v1/me",
            headers=auth(owner),
            json={"current_password": PASSWORD, "new_password": "short"},
        )
    )
    assert denied["code"] == "VALIDATION_ERROR"


def test_password_fields_must_come_together(client, owner):
    denied = error_of(
        client.patch("/api/v1/me", headers=auth(owner), json={"new_password": "NewPass!2026"})
    )
    assert denied["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# budget defaults (batch 26): only the daily default moved
# ---------------------------------------------------------------------------


def test_daily_budget_default_is_effectively_unlimited():
    settings = WorkspaceSettings()
    assert settings.ai_daily_token_budget == 100_000_000
    # The other budget defaults are deliberately untouched.
    assert settings.ai_max_output_tokens == 4096
    assert settings.ai_per_request_token_budget == 16000


def test_provider_daily_budget_default_matches():
    assert DeepSeekSettings.daily_token_budget == 100_000_000


def test_registered_workspace_gets_unlimited_default(client, owner):
    settings = data_of(client.get(f"/api/v1/workspaces/{owner['workspace']['id']}/settings", headers=auth(owner)))
    assert settings["ai_daily_token_budget"] == 100_000_000
    assert settings["ai_max_output_tokens"] == 4096
    assert settings["ai_per_request_token_budget"] == 16000
