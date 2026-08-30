"""Health probes, registration/login/refresh, and the identity endpoint."""
from __future__ import annotations

import pytest
from conftest import PASSWORD, audit_actions, auth, data_of, error_of, register, unique

# --------------------------------------------------------------------------
# health -- note these are mounted at the root, not under /api/v1
# --------------------------------------------------------------------------


def test_health_reports_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_ready_checks_database_and_data_root(client):
    """An explicit test DATABASE_URL is not a fallback, so this must be ready."""

    response = client.get("/health/ready")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["database"] is True
    assert payload["checks"]["data_root"] is True
    assert payload["checks"]["database_fallback"] is False


def test_health_ai_reports_not_configured_without_key(client):
    """The suite runs with an empty DEEPSEEK_API_KEY on purpose."""

    response = client.get("/health/ai")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "not_configured"
    assert payload["provider"] == "deepseek"


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def test_register_creates_owner_and_workspace(client):
    user = register(client, "founder")
    assert user["token"]
    assert user["user"]["email"] == user["email"]
    assert user["workspace"]["id"]


def test_register_rejects_duplicate_email(client, owner):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": owner["email"],
            "name": "duplicate",
            "password": PASSWORD,
            "workspace_name": unique("workspace"),
        },
    )
    assert response.status_code == 400, response.text
    assert error_of(response)["code"] == "VALIDATION_ERROR"


@pytest.mark.xfail(
    reason="BUG: schemas.py:12 declares `email: str` instead of `EmailStr`, so any "
    "string registers. email-validator 2.3.0 is installed and declared in "
    "pyproject.toml, so EmailStr was the intent.",
    strict=True,
)
def test_register_rejects_malformed_email(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "name": "x", "password": PASSWORD, "workspace_name": unique("w")},
    )
    assert response.status_code == 422, response.text


def test_register_rejects_short_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": f"{unique('short')}@example.test", "name": "x", "password": "abc", "workspace_name": unique("w")},
    )
    assert response.status_code in {400, 422}, response.text


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


def test_login_returns_token(client, owner):
    payload = data_of(client.post("/api/v1/auth/login", json={"email": owner["email"], "password": PASSWORD}))
    assert payload["access_token"]


def test_login_failure_does_not_leak_user_existence(client, owner):
    """An unknown email and a wrong password must be indistinguishable."""

    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": f"{unique('ghost')}@example.test", "password": PASSWORD},
    )
    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"email": owner["email"], "password": "definitely-not-the-password"},
    )
    assert unknown.status_code == wrong_password.status_code == 401
    assert error_of(unknown)["code"] == error_of(wrong_password)["code"]
    assert error_of(unknown)["message"] == error_of(wrong_password)["message"]


# --------------------------------------------------------------------------
# tokens and identity
# --------------------------------------------------------------------------


def test_refresh_issues_usable_token(client, owner):
    refreshed = data_of(client.post("/api/v1/auth/refresh", headers=auth(owner), json={}))
    token = refreshed.get("access_token")
    assert token
    me = data_of(client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}))
    assert me["user"]["email"] == owner["email"]


def test_me_returns_workspace_and_role(client, owner):
    payload = data_of(client.get("/api/v1/me", headers=auth(owner)))
    assert payload["user"]["email"] == owner["email"]
    workspaces = payload.get("workspaces") or []
    assert any(item.get("role") == "owner" for item in workspaces)


def test_me_requires_authentication(client):
    assert client.get("/api/v1/me").status_code == 401


def test_garbage_token_is_rejected(client):
    response = client.get("/api/v1/me", headers={"Authorization": "Bearer not.a.real.jwt"})
    assert response.status_code == 401


def test_registration_is_audited(client):
    user = register(client, "audited")
    actions = audit_actions(user["workspace"]["id"])
    assert actions, "registration should leave an audit trail"
