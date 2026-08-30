"""Isolated fixtures for the v1.2 API suite (12-stage pipeline).

Test-only environment variables are set before ``app`` is imported so the suite
never touches the development database or data root.  ``DEEPSEEK_API_KEY`` is
deliberately empty: every AI endpoint must degrade to a ``not_configured``
result, which is what makes the AI boundary testable without a live provider.

Jobs are not polled.  ``job_executor.schedule`` (app/infrastructure/jobs.py:120)
uses FastAPI ``BackgroundTasks``, and ``TestClient`` runs those synchronously
before returning the response, so results are ready as soon as the POST returns.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = REPO_ROOT / "output" / "test-runtime"
TEST_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = TEST_ROOT / "api-test.db"
if DB_PATH.exists():
    DB_PATH.unlink()

os.environ.update(
    {
        "APP_ENV": "test",
        "APP_SECRET_KEY": "test-only-secret-not-a-credential",
        "DATABASE_URL": f"sqlite:///{DB_PATH.as_posix()}",
        "DATA_ROOT": str(TEST_ROOT / "data"),
        "API_CORS_ORIGINS": "http://localhost:3000,http://127.0.0.1:3000",
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_BASE_URL": "http://127.0.0.1:9",
        "DEEPSEEK_MAX_RETRIES": "0",
        "MAX_UPLOAD_SIZE_MB": "50",
        "MAX_ROWS_PER_DATASET": "100000",
        "MAX_COLUMNS_PER_DATASET": "50",
    }
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db as database
from app.db import Base
from app.main import app
from app.models import AuditLog

PASSWORD = "test-password-123"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------
# database / client
# --------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    yield
    Base.metadata.drop_all(bind=database.engine)


@pytest.fixture
def client(isolated_database):
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def auth(user: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def data_of(response) -> dict:
    """Unwrap the ``{"data": ...}`` envelope, asserting a 200 first."""

    assert response.status_code == 200, response.text
    return response.json()["data"]


def error_of(response) -> dict:
    """Return the error body of a failed response without asserting a code."""

    payload = response.json()
    return payload.get("error", payload)


def register(client: TestClient, prefix: str = "owner") -> dict:
    email = f"{unique(prefix)}@example.test"
    payload = data_of(
        client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "name": prefix,
                "password": PASSWORD,
                "workspace_name": unique(f"workspace-{prefix}"),
            },
        )
    )
    return {
        "email": email,
        "password": PASSWORD,
        "token": payload["access_token"],
        "user": payload["user"],
        "workspace": payload["workspace"],
    }


def create_member(client: TestClient, owner: dict, role: str, prefix: str | None = None) -> dict:
    """Register a fresh user and add them to the owner's workspace as ``role``."""

    member = register(client, prefix or role)
    data_of(
        client.post(
            f"/api/v1/workspaces/{owner['workspace']['id']}/members",
            headers=auth(owner),
            json={"email": member["email"], "role": role},
        )
    )
    member["workspace"] = owner["workspace"]
    return member


def audit_rows(workspace_id: str) -> list[AuditLog]:
    with database.SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.workspace_id == workspace_id)).all())


def audit_actions(workspace_id: str) -> list[str]:
    return [row.action for row in audit_rows(workspace_id)]


def row_count(model) -> int:
    """Count rows of a domain model -- used to prove AI endpoints never write."""

    with database.SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


# --------------------------------------------------------------------------
# actors
# --------------------------------------------------------------------------


@pytest.fixture
def owner(client):
    return register(client, "owner")


@pytest.fixture
def editor(client, owner):
    return create_member(client, owner, "editor")


@pytest.fixture
def viewer(client, owner):
    return create_member(client, owner, "viewer")


@pytest.fixture
def outsider(client):
    """A user with their own workspace and no membership in the owner's."""

    return register(client, "outsider")


@pytest.fixture
def project(client, owner):
    return data_of(
        client.post(
            "/api/v1/projects",
            headers=auth(owner),
            json={"workspace_id": owner["workspace"]["id"], "name": unique("project")},
        )
    )


# --------------------------------------------------------------------------
# datasets
# --------------------------------------------------------------------------


def upload(client: TestClient, user: dict, project_id: str, fixture: str = "user_events.csv", dataset_name: str | None = None) -> dict:
    """Upload a fixture CSV.  The parse job completes before this returns."""

    path = FIXTURES / fixture if "/" not in fixture else FIXTURES / fixture
    form = {"project_id": project_id}
    if dataset_name:
        form["dataset_name"] = dataset_name
    with path.open("rb") as handle:
        return data_of(
            client.post(
                "/api/v1/datasets/upload",
                headers=auth(user),
                data=form,
                files={"file": (path.name, handle, "text/csv")},
            )
        )


def version_of(client: TestClient, user: dict, version_id: str) -> dict:
    return data_of(client.get(f"/api/v1/dataset-versions/{version_id}", headers=auth(user)))


def review_schema(client: TestClient, user: dict, version_id: str) -> dict:
    """Mark the inferred field roles as reviewed (the stage 2 gate)."""

    return data_of(client.post(f"/api/v1/dataset-versions/{version_id}/schema-review", headers=auth(user)))


@pytest.fixture
def ready_dataset(client, owner, project):
    """An event dataset parsed and schema-reviewed, ready for analysis."""

    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version_id = uploaded["version"]["id"]
    review_schema(client, owner, version_id)
    return {
        "dataset": uploaded["dataset"],
        "version": version_of(client, owner, version_id),
        "version_id": version_id,
        "project": project,
    }


@pytest.fixture
def metrics_dataset(client, owner, project):
    """A daily-metrics table (date, dau, new_users, ...) for trend/group runs."""

    uploaded = upload(client, owner, project["id"], "metrics.csv")
    version_id = uploaded["version"]["id"]
    review_schema(client, owner, version_id)
    return {
        "dataset": uploaded["dataset"],
        "version": version_of(client, owner, version_id),
        "version_id": version_id,
        "project": project,
    }


@pytest.fixture
def project_factory(client):
    """Create extra projects in a user's own workspace."""

    def make(user: dict, name: str | None = None) -> dict:
        return data_of(
            client.post(
                "/api/v1/projects",
                headers=auth(user),
                json={
                    "workspace_id": user["workspace"]["id"],
                    "name": name or unique("project"),
                },
            )
        )

    return make


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------


def start_analysis(
    client: TestClient,
    user: dict,
    ready: dict,
    analysis_type: str = "eda",
    config: dict | None = None,
    field_mapping: dict | None = None,
):
    """POST an analysis run and return the raw response (for error assertions)."""

    body = {
        "project_id": ready["project"]["id"],
        "dataset_version_id": ready["version_id"],
        "analysis_type": analysis_type,
        "config": config or {},
    }
    if field_mapping:
        body["field_mapping"] = field_mapping
    return client.post("/api/v1/analysis-runs", headers=auth(user), json=body)


def run_analysis(
    client: TestClient,
    user: dict,
    ready: dict,
    analysis_type: str = "eda",
    config: dict | None = None,
    field_mapping: dict | None = None,
) -> dict:
    """Start an analysis run and re-read it once the inline job has finished.

    The job executor runs synchronously under TestClient, so the run has
    already reached a terminal status by the time the POST returns.
    """

    created = data_of(start_analysis(client, user, ready, analysis_type, config, field_mapping))
    run_id = created["analysis_run"]["id"]
    return data_of(client.get(f"/api/v1/analysis-runs/{run_id}", headers=auth(user)))


def validate_analysis_config(
    client: TestClient,
    user: dict,
    ready: dict,
    analysis_type: str,
    config: dict | None = None,
    field_mapping: dict | None = None,
) -> dict:
    body = {
        "project_id": ready["project"]["id"],
        "dataset_version_id": ready["version_id"],
        "analysis_type": analysis_type,
        "config": config or {},
    }
    if field_mapping:
        body["field_mapping"] = field_mapping
    return data_of(client.post("/api/v1/analysis-runs/validate-config", headers=auth(user), json=body))


# --------------------------------------------------------------------------
# decision chain (stages 6-11)
# --------------------------------------------------------------------------


@pytest.fixture
def evidence(ready_dataset):
    """A structured reference to a real dataset version.

    `_check_evidence_scope` (app/main.py:587) rejects fabricated ids, so any
    test that stores a confirmed claim must cite an artifact that exists.
    """

    return [{"type": "dataset_version", "id": ready_dataset["version_id"]}]


@pytest.fixture
def problem(client, owner, project):
    """A named product problem, the stage 9 entry point for solutions."""

    return data_of(
        client.post(
            "/api/v1/problems",
            headers=auth(owner),
            json={
                "project_id": project["id"],
                "title": "Mobile checkout is losing users",
                "statement": "Users abandon the mobile checkout at the payment step.",
                "priority": "P1",
            },
        )
    )
