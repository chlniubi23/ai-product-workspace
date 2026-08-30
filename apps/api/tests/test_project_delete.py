"""DELETE /projects/{id} is a permanent purge.

These tests assert against the database and the filesystem rather than the
list endpoint, because the whole point of the change is that the rows are
gone -- a soft delete would also disappear from the list and pass a
list-only check.
"""
from __future__ import annotations

from pathlib import Path

from conftest import audit_actions, auth, data_of, error_of, upload
from sqlalchemy import select

from app import db as database
from app.config import settings
from app.models import (
    AnalysisRun,
    DataColumn,
    Dataset,
    DatasetVersion,
    DataQualityReport,
    Insight,
    Project,
    Task,
)


def _rows(model, **filters) -> list:
    with database.SessionLocal() as db:
        statement = select(model)
        for field, value in filters.items():
            statement = statement.where(getattr(model, field) == value)
        return list(db.scalars(statement).all())


def _version_files(dataset_id: str) -> list[Path]:
    with database.SessionLocal() as db:
        versions = db.scalars(
            select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)
        ).all()
        return [settings.data_path / version.storage_path for version in versions]


def _delete(client, user, project_id, **kwargs):
    """``TestClient.delete`` has no ``json=`` parameter, so a body needs ``request``."""

    body = kwargs.pop("json", None)
    if body is not None:
        return client.request(
            "DELETE", f"/api/v1/projects/{project_id}", headers=auth(user), json=body, **kwargs
        )
    return client.delete(f"/api/v1/projects/{project_id}", headers=auth(user), **kwargs)


# --------------------------------------------------------------------------
# confirmation gate
# --------------------------------------------------------------------------


def test_unconfirmed_delete_is_refused(client, owner, project):
    response = _delete(client, owner, project["id"])
    assert response.status_code == 409
    assert error_of(response)["code"] == "CONFIRMATION_REQUIRED"
    assert _rows(Project, id=project["id"]), "unconfirmed delete destroyed the project"


def test_delete_accepts_project_id_as_confirmation(client, owner, project):
    response = _delete(client, owner, project["id"], json={"confirm": project["id"]})
    assert response.status_code == 200, response.text
    assert not _rows(Project, id=project["id"])


def test_delete_accepts_confirm_true_query(client, owner, project):
    response = _delete(client, owner, project["id"], params={"confirm": "true"})
    assert response.status_code == 200, response.text
    assert not _rows(Project, id=project["id"])


# --------------------------------------------------------------------------
# the cascade
# --------------------------------------------------------------------------


def test_delete_removes_datasets_versions_and_quality_reports(client, owner, project, ready_dataset):
    dataset_id = ready_dataset["dataset"]["id"]
    version_id = ready_dataset["version_id"]

    assert _rows(DatasetVersion, id=version_id)
    assert _rows(DataQualityReport, dataset_version_id=version_id)
    assert _rows(DataColumn, dataset_version_id=version_id)

    assert _delete(client, owner, project["id"], params={"confirm": "true"}).status_code == 200

    assert not _rows(Dataset, id=dataset_id), "dataset survived the purge"
    assert not _rows(DatasetVersion, id=version_id), "version survived the purge"
    assert not _rows(DataQualityReport, dataset_version_id=version_id), "quality report survived"
    assert not _rows(DataColumn, dataset_version_id=version_id), "schema columns survived"


def test_delete_unlinks_uploaded_files(client, owner, project, ready_dataset):
    """The row carries the only pointer to the file, so it must go first."""

    paths = _version_files(ready_dataset["dataset"]["id"])
    assert paths and all(path.is_file() for path in paths), "fixture upload left no file"

    assert _delete(client, owner, project["id"], params={"confirm": "true"}).status_code == 200

    assert not any(path.exists() for path in paths), "uploaded files leaked"


def test_delete_reports_what_it_removed(client, owner, project, ready_dataset):
    payload = data_of(_delete(client, owner, project["id"], params={"confirm": "true"}))
    removed = payload["removed"]
    assert payload["deleted"] is True
    assert removed["datasets"] == 1
    assert removed["versions"] == 1
    assert removed["files"] == 1


def test_delete_removes_analysis_runs(client, owner, project, ready_dataset):
    """analysis_runs.dataset_version_id has no ondelete -- ordering matters."""

    run = data_of(
        client.post(
            "/api/v1/analysis-runs",
            headers=auth(owner),
            json={
                "project_id": project["id"],
                "dataset_version_id": ready_dataset["version_id"],
                "analysis_type": "eda",
                "parameters": {},
            },
        )
    )
    run_id = run["analysis_run"]["id"]
    assert _rows(AnalysisRun, id=run_id)

    assert _delete(client, owner, project["id"], params={"confirm": "true"}).status_code == 200
    assert not _rows(AnalysisRun, id=run_id), "analysis run survived the purge"


def test_delete_removes_task_linked_insight(client, owner, project):
    """Regression: Insight.task_id -> tasks.id declares no ondelete.

    Project.tasks carries an ORM delete-orphan cascade (models.py:128), so
    db.delete(project) removes the Task rows.  An Insight still pointing at one
    used to abort the whole purge with FOREIGN KEY constraint failed.
    """

    task = data_of(
        client.post(f"/api/v1/projects/{project['id']}/tasks", headers=auth(owner), json={"title": "t"})
    )
    insight = data_of(
        client.post(
            "/api/v1/insights",
            headers=auth(owner),
            json={
                "project_id": project["id"],
                "task_id": task["id"],
                "title": "linked",
                "insight_type": "fact",
                "content": "c",
                "confidence": "medium",
            },
        )
    )
    assert insight["task_id"] == task["id"]

    response = _delete(client, owner, project["id"], params={"confirm": "true"})
    assert response.status_code == 200, response.text
    assert not _rows(Insight, id=insight["id"])
    assert not _rows(Task, id=task["id"])


def test_delete_leaves_other_projects_untouched(client, owner, project, ready_dataset, project_factory):
    other = project_factory(owner)
    other_upload = upload(client, owner, other["id"], "user_events.csv")
    other_dataset_id = other_upload["dataset"]["id"]
    other_paths = _version_files(other_dataset_id)

    assert _delete(client, owner, project["id"], params={"confirm": "true"}).status_code == 200

    assert _rows(Project, id=other["id"])
    assert _rows(Dataset, id=other_dataset_id), "purge crossed a project boundary"
    assert all(path.is_file() for path in other_paths), "purge deleted another project's files"


# --------------------------------------------------------------------------
# access and audit
# --------------------------------------------------------------------------


def test_outsider_cannot_delete_foreign_project(client, outsider, project):
    response = _delete(client, outsider, project["id"], params={"confirm": "true"})
    assert response.status_code in {403, 404}
    assert _rows(Project, id=project["id"]), "foreign delete succeeded"


def test_deleted_project_is_gone_from_the_list(client, owner, project):
    assert _delete(client, owner, project["id"], params={"confirm": "true"}).status_code == 200

    payload = data_of(client.get("/api/v1/projects", headers=auth(owner)))
    items = payload["items"] if isinstance(payload, dict) else payload
    assert project["id"] not in [item["id"] for item in items]


def test_deleted_project_returns_404(client, owner, project):
    assert _delete(client, owner, project["id"], params={"confirm": "true"}).status_code == 200
    assert client.get(f"/api/v1/projects/{project['id']}", headers=auth(owner)).status_code in {403, 404}


def test_purge_is_audited(client, owner, project):
    workspace_id = owner["workspace"]["id"]
    assert _delete(client, owner, project["id"], params={"confirm": "true"}).status_code == 200
    assert "project.purged" in audit_actions(workspace_id)
