"""Role enforcement and cross-workspace isolation.

``membership`` (app/main.py:338) is the single gate: it raises ``FORBIDDEN`` 403
both when the user is not a member at all and when their role outranks the
required minimum (viewer=1, editor=2, owner=3).  These tests pin both branches
because a regression in either one is a tenant data leak, not a UI bug.
"""
from __future__ import annotations

import pytest
from conftest import auth, data_of, error_of, unique, upload

# --------------------------------------------------------------------------
# unauthenticated access
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/me"),
        ("get", "/api/v1/projects"),
        ("get", "/api/v1/workspaces"),
        ("get", "/api/v1/datasets"),
        ("get", "/api/v1/settings"),
        ("get", "/api/v1/audit-logs"),
        ("get", "/api/v1/analysis-runs"),
        ("get", "/api/v1/insights"),
        ("get", "/api/v1/problems"),
        ("get", "/api/v1/documents"),
        ("get", "/api/v1/decision-proposals"),
        ("get", "/api/v1/ai/usage"),
    ],
)
def test_protected_routes_reject_anonymous(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code in {401, 403}, f"{path} -> {response.status_code}"


def test_garbage_bearer_token_is_rejected(client):
    response = client.get("/api/v1/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
    assert error_of(response)["code"] == "UNAUTHENTICATED"


def test_me_returns_workspace_membership(client, owner):
    payload = data_of(client.get("/api/v1/me", headers=auth(owner)))
    assert payload["user"]["email"] == owner["email"]
    workspace_ids = [item["id"] for item in payload["workspaces"]]
    assert owner["workspace"]["id"] in workspace_ids


# --------------------------------------------------------------------------
# viewer is read-only
# --------------------------------------------------------------------------


def test_viewer_cannot_create_project(client, viewer):
    response = client.post(
        "/api/v1/projects",
        headers=auth(viewer),
        json={"workspace_id": viewer["workspace"]["id"], "name": unique("nope")},
    )
    assert response.status_code == 403
    assert error_of(response)["code"] == "FORBIDDEN"


def test_viewer_cannot_upload_dataset(client, viewer, project):
    with pytest.raises(AssertionError):
        upload(client, viewer, project["id"])


def test_viewer_can_read_project(client, viewer, project):
    payload = data_of(client.get(f"/api/v1/projects/{project['id']}", headers=auth(viewer)))
    assert payload["id"] == project["id"]


def test_viewer_cannot_add_members(client, viewer, owner):
    response = client.post(
        f"/api/v1/workspaces/{owner['workspace']['id']}/members",
        headers=auth(viewer),
        json={"email": "someone@example.test", "role": "viewer"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# editor can write, but not administer
# --------------------------------------------------------------------------


def test_editor_can_create_project(client, editor):
    payload = data_of(
        client.post(
            "/api/v1/projects",
            headers=auth(editor),
            json={"workspace_id": editor["workspace"]["id"], "name": unique("editor-project")},
        )
    )
    assert payload["name"].startswith("editor-project")


def test_editor_cannot_add_members(client, editor, owner):
    """Membership administration is owner-only."""

    response = client.post(
        f"/api/v1/workspaces/{owner['workspace']['id']}/members",
        headers=auth(editor),
        json={"email": "someone-else@example.test", "role": "viewer"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# cross-workspace isolation
# --------------------------------------------------------------------------


def test_outsider_cannot_read_foreign_project(client, outsider, project):
    response = client.get(f"/api/v1/projects/{project['id']}", headers=auth(outsider))
    assert response.status_code in {403, 404}, response.text


def test_outsider_cannot_modify_foreign_project(client, outsider, project):
    response = client.patch(
        f"/api/v1/projects/{project['id']}",
        headers=auth(outsider),
        json={"name": "hijacked"},
    )
    assert response.status_code in {403, 404}


def test_outsider_cannot_delete_foreign_project(client, outsider, project):
    response = client.delete(f"/api/v1/projects/{project['id']}", headers=auth(outsider))
    assert response.status_code in {403, 404}


def test_outsider_cannot_read_foreign_dataset_version(client, outsider, ready_dataset):
    response = client.get(
        f"/api/v1/dataset-versions/{ready_dataset['version_id']}",
        headers=auth(outsider),
    )
    assert response.status_code in {403, 404}


def test_outsider_cannot_read_foreign_quality_report(client, outsider, ready_dataset):
    response = client.get(
        f"/api/v1/dataset-versions/{ready_dataset['version_id']}/quality-report",
        headers=auth(outsider),
    )
    assert response.status_code in {403, 404}


def test_project_list_is_scoped_to_own_workspace(client, outsider, project):
    """The outsider's own list must not contain the owner's project."""

    payload = data_of(client.get("/api/v1/projects", headers=auth(outsider)))
    items = payload["items"] if isinstance(payload, dict) else payload
    assert project["id"] not in [item["id"] for item in items]


def test_dataset_list_is_scoped_to_own_workspace(client, outsider, ready_dataset):
    payload = data_of(client.get("/api/v1/datasets", headers=auth(outsider)))
    items = payload["items"] if isinstance(payload, dict) else payload
    assert ready_dataset["dataset"]["id"] not in [item["id"] for item in items]


def test_outsider_cannot_read_foreign_audit_log(client, outsider, owner):
    """Audit logs carry actor emails and action history -- strictly scoped."""

    response = client.get(
        "/api/v1/audit-logs",
        headers=auth(outsider),
        params={"workspace_id": owner["workspace"]["id"]},
    )
    if response.status_code == 200:
        items = response.json()["data"]
        items = items["items"] if isinstance(items, dict) else items
        assert items == [], "audit rows leaked across workspaces"
    else:
        assert response.status_code in {403, 404}
