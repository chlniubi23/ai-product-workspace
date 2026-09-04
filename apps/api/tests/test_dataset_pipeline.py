"""Stages 1-3: upload, parse, schema review, quality report.

The parse job is scheduled through FastAPI ``BackgroundTasks``
(app/infrastructure/jobs.py:120), which ``TestClient`` drains before returning
the response, so no polling is needed -- a version is already parsed when the
upload POST returns.
"""
from __future__ import annotations

from conftest import (
    auth,
    data_of,
    error_of,
    review_schema,
    unique,
    upload,
    version_of,
)

# --------------------------------------------------------------------------
# upload + parse (stage 1)
# --------------------------------------------------------------------------


def test_upload_parses_csv_synchronously(client, owner, project):
    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version = version_of(client, owner, uploaded["version"]["id"])

    assert version["status"] in {"ready", "confirmed", "succeeded"}, version["status"]
    assert version["row_count"] == 13, "user_events.csv has 13 data rows"
    assert version["column_count"] == 5
    assert uploaded["job"]["job_type"] == "dataset_parse"


def test_upload_infers_column_names(client, owner, project):
    uploaded = upload(client, owner, project["id"], "user_events.csv")
    schema = data_of(
        client.get(f"/api/v1/dataset-versions/{uploaded['version']['id']}/schema", headers=auth(owner))
    )
    columns = schema["columns"] if isinstance(schema, dict) else schema
    names = [column["name"] for column in columns]
    assert names == ["user_id", "event_time", "event_name", "channel", "version"]


def test_upload_rejects_unsupported_extension(client, owner, project):
    response = client.post(
        "/api/v1/datasets/upload",
        headers=auth(owner),
        data={"project_id": project["id"]},
        files={"file": ("payload.exe", b"MZ\x90\x00binary", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_upload_rejects_missing_project(client, owner):
    response = client.post(
        "/api/v1/datasets/upload",
        headers=auth(owner),
        data={"project_id": "does-not-exist"},
        files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert response.status_code in {403, 404}


def test_reupload_same_name_appends_version(client, owner, project):
    """BUG-015: a re-upload must version, not fork a parallel dataset."""

    name = unique("versioned")
    first = upload(client, owner, project["id"], "user_events.csv", dataset_name=name)
    second = upload(client, owner, project["id"], "user_events.csv", dataset_name=name)

    assert first["dataset"]["id"] == second["dataset"]["id"]
    assert second["version"]["version_number"] == first["version"]["version_number"] + 1

    versions = data_of(
        client.get(f"/api/v1/datasets/{first['dataset']['id']}/versions", headers=auth(owner))
    )
    items = versions["items"] if isinstance(versions, dict) else versions
    assert len(items) == 2


def test_upload_records_audit_entry(client, owner, project):
    from conftest import audit_actions

    upload(client, owner, project["id"], "metrics.csv")
    assert "dataset.parse_queued" in audit_actions(owner["workspace"]["id"])


def test_header_only_csv_is_rejected_by_parse_job(client, owner, project):
    """empty.csv has headers and no data rows.

    The upload itself succeeds (the file is valid CSV), but the parse job
    rejects it at app/main.py:2180 and the version is left in a failed state.
    """

    uploaded = upload(client, owner, project["id"], "empty.csv")
    version = version_of(client, owner, uploaded["version"]["id"])
    assert version["status"] in {"failed", "error"}, version["status"]

    job = data_of(client.get(f"/api/v1/jobs/{uploaded['job']['id']}", headers=auth(owner)))
    assert job["status"] == "failed"


def test_failed_version_does_not_complete_stage_one(client, owner, project):
    """A failed upload must never be treated as usable data."""

    upload(client, owner, project["id"], "empty.csv")
    status = data_of(
        client.get(f"/api/v1/projects/{project['id']}/workflow-status", headers=auth(owner))
    )
    assert status["steps"][0]["complete"] is False
    assert status["selected_dataset_version_id"] is None


# --------------------------------------------------------------------------
# schema review (the stage 2 gate)
# --------------------------------------------------------------------------


def test_upload_auto_accepts_schema_and_completes_stage_one(client, owner, project):
    """A plain upload advances on its own -- no manual review click.

    Inverted from the old manual-gate contract: the parse job stamps
    ``schema_auto_accepted_at`` (app/main.py:_run_auto_analyses), which counts
    as reviewed for ``data_complete`` at app/main.py:1651.
    """

    upload(client, owner, project["id"], "user_events.csv")
    status = data_of(
        client.get(f"/api/v1/projects/{project['id']}/workflow-status", headers=auth(owner))
    )
    assert status["schema_reviewed"] is True
    assert status["schema_auto_accepted"] is True
    assert status["schema_reviewed_by_human"] is False, "nobody clicked review"
    assert status["steps"][0]["complete"] is True


def test_explicit_schema_review_is_reported_separately(client, owner, project):
    """An explicit review must remain distinguishable from an auto-accept."""

    uploaded = upload(client, owner, project["id"], "user_events.csv")
    review_schema(client, owner, uploaded["version"]["id"])

    status = data_of(
        client.get(f"/api/v1/projects/{project['id']}/workflow-status", headers=auth(owner))
    )
    assert status["schema_reviewed"] is True
    assert status["schema_reviewed_by_human"] is True
    assert status["steps"][0]["complete"] is True


def test_schema_patch_sets_mapping_role(client, owner, project):
    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version_id = uploaded["version"]["id"]
    response = client.patch(
        f"/api/v1/dataset-versions/{version_id}/schema",
        headers=auth(owner),
        json={"columns": [{"name": "user_id", "mapping_role": "user_id"}]},
    )
    assert response.status_code == 200, response.text

    status = data_of(
        client.get(f"/api/v1/projects/{project['id']}/workflow-status", headers=auth(owner))
    )
    assert "user_id" in status["confirmed_roles"]
    assert "user_id" not in status["missing_roles"]


def test_missing_roles_are_advisory_only(client, ready_dataset, owner, project):
    """Absent suggested roles must never block the pipeline."""

    status = data_of(
        client.get(f"/api/v1/projects/{project['id']}/workflow-status", headers=auth(owner))
    )
    assert status["steps"][0]["complete"] is True
    assert set(status["suggested_roles"]) == {"user_id", "event_time", "event_name"}


def test_viewer_cannot_review_schema(client, viewer, ready_dataset):
    response = client.post(
        f"/api/v1/dataset-versions/{ready_dataset['version_id']}/schema-review",
        headers=auth(viewer),
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# quality report (stage 3)
# --------------------------------------------------------------------------


def test_quality_report_is_available_after_parse(client, owner, ready_dataset):
    report = data_of(
        client.get(
            f"/api/v1/dataset-versions/{ready_dataset['version_id']}/quality-report",
            headers=auth(owner),
        )
    )
    assert report is not None
    assert isinstance(report, dict)


def test_quality_step_completes_with_report(client, owner, project, ready_dataset):
    status = data_of(
        client.get(f"/api/v1/projects/{project['id']}/workflow-status", headers=auth(owner))
    )
    quality = next(step for step in status["steps"] if step["key"] == "quality")
    assert quality["complete"] is True


def test_duplicate_rows_are_detected(client, owner, project):
    """anomalies.csv contains an exact duplicate row on purpose."""

    uploaded = upload(client, owner, project["id"], "anomalies.csv")
    report = data_of(
        client.get(
            f"/api/v1/dataset-versions/{uploaded['version']['id']}/quality-report",
            headers=auth(owner),
        )
    )
    serialized = str(report)
    assert "duplicate" in serialized.lower(), "quality report should mention duplicates"


def test_preview_returns_rows(client, owner, ready_dataset):
    preview = data_of(
        client.get(
            f"/api/v1/dataset-versions/{ready_dataset['version_id']}/preview",
            headers=auth(owner),
        )
    )
    assert preview is not None


def test_dataset_delete_requires_explicit_confirmation(client, owner, ready_dataset):
    """Destructive deletes must not happen on an unconfirmed call."""

    dataset_id = ready_dataset["dataset"]["id"]
    response = client.delete(f"/api/v1/datasets/{dataset_id}", headers=auth(owner))
    assert response.status_code == 409
    assert error_of(response)["code"] == "CONFIRMATION_REQUIRED"

    payload = data_of(client.get("/api/v1/datasets", headers=auth(owner)))
    items = payload["items"] if isinstance(payload, dict) else payload
    assert dataset_id in [item["id"] for item in items], "unconfirmed delete removed data"


def test_confirmed_dataset_delete_hides_it_from_list(client, owner, ready_dataset):
    dataset_id = ready_dataset["dataset"]["id"]
    response = client.delete(
        f"/api/v1/datasets/{dataset_id}",
        headers=auth(owner),
        params={"confirm": "true"},
    )
    assert response.status_code == 200, response.text

    payload = data_of(client.get("/api/v1/datasets", headers=auth(owner)))
    items = payload["items"] if isinstance(payload, dict) else payload
    assert dataset_id not in [item["id"] for item in items]


# --------------------------------------------------------------------------
# automatic analysis (the upload -> report path)
# --------------------------------------------------------------------------


def test_upload_creates_succeeded_analysis_runs(client, owner, project):
    """The whole point: upload alone produces finished analysis."""

    upload(client, owner, project["id"], "user_events.csv")
    runs = data_of(client.get("/api/v1/analysis-runs", headers=auth(owner), params={"project_id": project["id"]}))
    items = runs["items"] if isinstance(runs, dict) else runs

    assert items, "upload produced no analysis runs"
    assert all(run["status"] == "succeeded" for run in items), [run["status"] for run in items]


def test_auto_analysis_completes_the_analysis_step(client, owner, project):
    upload(client, owner, project["id"], "user_events.csv")
    status = data_of(
        client.get(f"/api/v1/projects/{project['id']}/workflow-status", headers=auth(owner))
    )
    analysis = next(step for step in status["steps"] if step["key"] == "analysis")
    assert analysis["complete"] is True


def test_auto_analysis_reports_its_plan_and_reasons(client, owner, project):
    """The selection must be inspectable, not a black box."""

    uploaded = upload(client, owner, project["id"], "user_events.csv")
    job = data_of(client.get(f"/api/v1/jobs/{uploaded['job']['id']}", headers=auth(owner)))
    plan = job["input_json"]["auto_analysis_plan"]

    assert [entry["analysis_type"] for entry in plan] == ["eda", "retention"]
    assert all(entry["reason"] for entry in plan), "every selection needs a stated reason"


def test_event_data_selects_retention_not_trend(client, owner, project):
    """user_events.csv has user_id + event_time roles -> retention wins."""

    uploaded = upload(client, owner, project["id"], "user_events.csv")
    job = data_of(client.get(f"/api/v1/jobs/{uploaded['job']['id']}", headers=auth(owner)))
    types = [entry["analysis_type"] for entry in job["input_json"]["auto_analysis_plan"]]

    assert "retention" in types
    assert "trend" not in types


def test_metric_data_selects_trend_and_anomaly(client, owner, project):
    """metrics.csv is a datetime + numeric time series, with no user_id."""

    uploaded = upload(client, owner, project["id"], "metrics.csv")
    job = data_of(client.get(f"/api/v1/jobs/{uploaded['job']['id']}", headers=auth(owner)))
    types = [entry["analysis_type"] for entry in job["input_json"]["auto_analysis_plan"]]

    assert "trend" in types
    assert "retention" not in types


def test_funnel_is_never_auto_selected(client, owner, project):
    """A funnel needs an ordered step list that cannot be inferred.

    Guessing it would yield a plausible but wrong funnel -- worse than none.
    """

    for fixture in ("user_events.csv", "metrics.csv"):
        uploaded = upload(client, owner, project["id"], fixture)
        job = data_of(client.get(f"/api/v1/jobs/{uploaded['job']['id']}", headers=auth(owner)))
        types = [entry["analysis_type"] for entry in job["input_json"]["auto_analysis_plan"]]
        assert "funnel" not in types, fixture


def test_auto_analysis_caps_at_four_runs(client, owner, project):
    uploaded = upload(client, owner, project["id"], "metrics.csv")
    job = data_of(client.get(f"/api/v1/jobs/{uploaded['job']['id']}", headers=auth(owner)))
    assert len(job["input_json"]["auto_analysis_plan"]) <= 4


def test_failed_parse_creates_no_analysis_runs(client, owner, project):
    """empty.csv fails to parse; it must not leave phantom analysis behind."""

    upload(client, owner, project["id"], "empty.csv")
    runs = data_of(client.get("/api/v1/analysis-runs", headers=auth(owner), params={"project_id": project["id"]}))
    items = runs["items"] if isinstance(runs, dict) else runs
    assert items == []


def test_auto_analysis_is_attributed_to_the_uploader(client, owner, project):
    """Auto-created runs are still owned by a real person, for audit."""

    upload(client, owner, project["id"], "user_events.csv")
    runs = data_of(client.get("/api/v1/analysis-runs", headers=auth(owner), params={"project_id": project["id"]}))
    items = runs["items"] if isinstance(runs, dict) else runs
    assert all(run["requested_by"] == owner["user"]["id"] for run in items)
