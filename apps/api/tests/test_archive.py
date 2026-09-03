"""Project archive (batch 9): one project = one workflow.

Archiving flips a finished workflow into read-only history: editor+ writes
through project_for (and the per-route guards that close the bypass paths)
return 409 PROJECT_ARCHIVED, while viewer reads keep working.  Restore flips
it back.  The archive/unarchive endpoints are idempotent and bypass their own
guard on purpose.
"""

from __future__ import annotations

from conftest import auth, data_of, error_of, review_schema, upload


def make_ready(client, owner, project) -> dict:
    uploaded = upload(client, owner, project["id"], "user_events.csv")
    review_schema(client, owner, uploaded["version"]["id"])
    return {"project": project, "version_id": uploaded["version"]["id"], "version": uploaded["version"]}


def archive(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/archive", headers=auth(user))


def unarchive(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/unarchive", headers=auth(user))


def test_archive_by_editor_sets_status_and_timestamp(client, owner, project):
    response = archive(client, owner, project["id"])
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["status"] == "archived"
    assert payload["archived_at"]

    # idempotent: a second archive keeps the state and returns 200
    again = archive(client, owner, project["id"])
    assert again.status_code == 200
    assert data_of(again)["status"] == "archived"


def test_unarchive_restores_active_state_and_is_idempotent(client, owner, project):
    assert archive(client, owner, project["id"]).status_code == 200
    response = unarchive(client, owner, project["id"])
    assert response.status_code == 200
    payload = data_of(response)
    assert payload["status"] == "active"
    assert payload["archived_at"] is None
    # idempotent on an already-active project
    assert unarchive(client, owner, project["id"]).status_code == 200
    assert data_of(unarchive(client, owner, project["id"]))["status"] == "active"


def test_archive_requires_editor(client, owner, viewer, project):
    assert archive(client, viewer, project["id"]).status_code == 403
    assert unarchive(client, viewer, project["id"]).status_code == 403


def test_archive_is_invisible_to_outsiders(client, outsider, project):
    assert archive(client, outsider, project["id"]).status_code in {403, 404}


def test_archived_project_blocks_editor_writes_but_allows_reads(client, owner, project):
    ready = make_ready(client, owner, project)
    assert archive(client, owner, project["id"]).status_code == 200

    # editor+ writes through project_for -> 409 PROJECT_ARCHIVED
    insight = client.post(
        "/api/v1/insights",
        headers=auth(owner),
        json={
            "project_id": project["id"],
            "title": "归档后写入",
            "insight_type": "fact",
            "content": "应当被拒绝。",
        },
    )
    assert insight.status_code == 409
    assert error_of(insight)["code"] == "PROJECT_ARCHIVED"

    # reads keep working: history replay is built on them
    listed = client.get(f"/api/v1/insights?project_id={project['id']}", headers=auth(owner))
    assert listed.status_code == 200
    assert listed.json()["data"] == []

    datasets = client.get(f"/api/v1/datasets?project_id={project['id']}", headers=auth(owner))
    assert datasets.status_code == 200
    assert len(datasets.json()["data"]) == 1

    # version writes on existing artifacts are blocked too
    blocked = client.post(
        f"/api/v1/dataset-versions/{ready['version_id']}/schema-review",
        headers=auth(owner),
    )
    assert blocked.status_code == 409
    assert error_of(blocked)["code"] == "PROJECT_ARCHIVED"


def test_unarchive_restores_write_access(client, owner, project):
    make_ready(client, owner, project)
    archive(client, owner, project["id"])
    unarchive(client, owner, project["id"])

    insight = client.post(
        "/api/v1/insights",
        headers=auth(owner),
        json={
            "project_id": project["id"],
            "title": "恢复后写入",
            "insight_type": "fact",
            "content": "恢复后可继续编辑。",
        },
    )
    assert insight.status_code == 200, insight.text


def test_upload_raw_to_archived_project_returns_409(client, owner, project):
    from pathlib import Path

    archive(client, owner, project["id"])
    path = Path(__file__).resolve().parent / "fixtures" / "user_events.csv"
    with path.open("rb") as handle:
        response = client.post(
            "/api/v1/datasets/upload",
            headers=auth(owner),
            data={"project_id": project["id"]},
            files={"file": (path.name, handle, "text/csv")},
        )
    assert response.status_code == 409
    assert error_of(response)["code"] == "PROJECT_ARCHIVED"


def test_delete_of_archived_project_still_allowed_for_owner(client, owner, project):
    archive(client, owner, project["id"])
    response = client.delete(f"/api/v1/projects/{project['id']}?confirm={project['id']}", headers=auth(owner))
    assert response.status_code == 200


def test_list_projects_hides_archived_unless_requested(client, owner, project):
    archive(client, owner, project["id"])

    default_list = data_of(client.get("/api/v1/projects", headers=auth(owner)))
    assert all(item["id"] != project["id"] for item in default_list)

    with_archived = data_of(client.get("/api/v1/projects?include_archived=true", headers=auth(owner)))
    assert any(item["id"] == project["id"] and item["status"] == "archived" for item in with_archived)
