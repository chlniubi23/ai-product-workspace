"""Batch 13/15: the grounding chain -- report as the interview's foundation.

Covers the identifier-column noise filter, digest artifacts landing on real
analysis runs (idempotent), the unique-report semantics (compute replaces),
and the interview/distillation grounding (latest report context + window 20).
"""

from __future__ import annotations

from io import BytesIO, StringIO

import pandas as pd
from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.analytics.digest import build_findings_digest
from app.config import settings
from app.models import AnalysisArtifact, AnalysisRun, AutoAnalysisReport, Project, User
from app.schemas import DocumentGenerate
from app.services.auto_report import _compute_report_aggregates
from app.services.documents import _build_document_context
from app.services.interview import (
    _ARTIFACT_CONTEXT_LIMIT,
    _grounding_artifacts,
)

# region: 3 unique / 12 rows (qualified category); plan: 75% concentration;
# revenue: 2/12 missing; report_id: 12/12 unique -> identifier noise.
CSV_GROUNDING = (
    "region,plan,revenue,report_id\n"
    "华东,a,100,r-001\n"
    "华东,a,120,r-002\n"
    "华东,a,,r-003\n"
    "华东,b,90,r-004\n"
    "华北,a,80,r-005\n"
    "华北,a,,r-006\n"
    "华北,b,60,r-007\n"
    "华北,a,70,r-008\n"
    "华南,b,50,r-009\n"
    "华南,a,110,r-010\n"
    "华南,b,95,r-011\n"
    "华南,a,105,r-012\n"
)


def _upload_grounding(client, owner, project):
    return data_of(
        client.post(
            "/api/v1/datasets/upload-batch",
            headers=auth(owner),
            data={"project_id": project["id"]},
            files=[("files", ("grounding.csv", BytesIO(CSV_GROUNDING.encode()), "text/csv"))],
        )
    )


def test_identifier_columns_drop_top_value_noise():
    frame = pd.read_csv(StringIO(CSV_GROUNDING))
    uploads = settings.data_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "grounding-unit.csv").write_bytes(CSV_GROUNDING.encode())
    snapshot = {
        "version_id": "v-unit",
        "dataset_name": "grounding",
        "version_number": 1,
        "storage_path": "uploads/grounding-unit.csv",
        "file_name": "grounding-unit.csv",
        "row_count": len(frame),
        "column_count": len(frame.columns),
        "columns": [
            {"name": "region", "type": "string"},
            {"name": "plan", "type": "string"},
            {"name": "revenue", "type": "float"},
            {"name": "report_id", "type": "string"},
        ],
    }
    aggregates = _compute_report_aggregates(snapshot)
    metrics = {item["name"]: item for item in aggregates["metrics"]}
    assert metrics["report_id"].get("identifier") is True
    assert "categories" not in metrics["report_id"]
    assert "categories" in metrics["region"], "ordinary category columns keep their breakdown"
    findings = build_findings_digest([aggregates])
    for item in findings:
        assert "report_id" not in (item.get("columns") or []), "identifier columns must not produce digest noise"


def test_compute_lands_finding_artifacts_idempotently_and_replaces(client, owner, project):
    _upload_grounding(client, owner, project)
    data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))

    def landed_findings():
        with database.SessionLocal() as db:
            rows = db.scalars(
                select(AnalysisArtifact)
                .join(AnalysisRun, AnalysisRun.id == AnalysisArtifact.analysis_run_id)
                .where(
                    AnalysisArtifact.artifact_type == "finding",
                    AnalysisRun.project_id == project["id"],
                )
            ).all()
            attached = []
            for row in rows:
                run = db.get(AnalysisRun, row.analysis_run_id)
                attached.append((run.project_id == project["id"], run.status, row.title))
            return attached

    first = landed_findings()
    assert first, "findings must land as real artifacts on succeeded runs"
    assert all(ok and status == "succeeded" and title for ok, status, title in first)

    data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))
    second = landed_findings()
    assert len(second) == len(first), "recompute must not stack duplicate finding artifacts"

    with database.SessionLocal() as db:
        reports = db.scalars(
            select(AutoAnalysisReport)
            .where(AutoAnalysisReport.project_id == project["id"])
            .order_by(AutoAnalysisReport.created_at)
        ).all()
        assert len(reports) == 1, "batch 15: compute replaces -- exactly one live report remains"
        assert reports[0].confirmed_at is None, "the surviving report is the fresh draft"


def test_dataset_without_succeeded_run_keeps_digest_without_artifacts(client, owner, project):
    _upload_grounding(client, owner, project)
    with database.SessionLocal() as db:
        # Simulate a dataset whose analyses never succeeded: drop the runs.
        for run in db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project["id"])).all():
            db.delete(run)
        db.commit()
    data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))
    with database.SessionLocal() as db:
        artifacts = db.scalars(
            select(AnalysisArtifact)
            .join(AnalysisRun, AnalysisRun.id == AnalysisArtifact.analysis_run_id)
            .where(
                AnalysisArtifact.artifact_type == "finding",
                AnalysisRun.project_id == project["id"],
            )
        ).all()
        assert artifacts == [], "no succeeded run means no landed finding artifacts"
        report = db.scalar(
            select(AutoAnalysisReport).where(AutoAnalysisReport.project_id == project["id"])
        )
    assert report.deterministic_json["findings"], "the report digest itself survives"


def _orm_project(db, project: dict):
    return db.get(Project, project["id"])


def test_latest_report_context_follows_the_single_live_report(client, owner, project):
    _upload_grounding(client, owner, project)
    data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))
    first_id = data_of(
        client.get(f"/api/v1/projects/{project['id']}/auto-reports?page_size=5", headers=auth(owner))
    )[0]["id"]
    data_of(client.post(f"/api/v1/auto-reports/{first_id}/confirm", headers=auth(owner)))
    # Recompute: the confirmed predecessor is deleted, the fresh draft becomes
    # the sole grounding.
    data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))

    with database.SessionLocal() as db:
        user = db.get(User, owner["user"]["id"])
        context = _build_document_context(
            DocumentGenerate(project_id=project["id"], document_type="prd", title="地基验收", source_refs=[]),
            db,
            user,
        )
        grounding = _grounding_artifacts(db, _orm_project(db, project))
    summaries = [item for item in grounding if item["artifact_type"] == "dataset_summary"]
    assert summaries, "the live report still feeds the grounding chain"
    assert all(not item["id"].startswith(f"{first_id}:") for item in summaries), "the confirmed predecessor is gone"
    finding_items = [item for item in context["safe_context"]["artifacts"] if item["artifact_type"] == "finding"]
    assert finding_items, "documents read the landed finding artifacts"
    assert all(len(item["id"]) > 20 for item in finding_items), "persisted findings carry real ids"


def test_narration_job_fails_gracefully_when_report_was_replaced(client, owner, project, monkeypatch):
    """compute 删除旧报告后，仍在途的 narrate job 必须以 REPORT_MISSING 诚实失败，
    不崩溃、不重试、不再触发任何 provider 调用。"""

    _upload_grounding(client, owner, project)
    report = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )["report"]
    stale_report_id = report["id"]

    class _FailSpy:
        configured = True

        def __init__(self):
            self.calls = 0

        async def complete(self, *, messages, response_schema, request_metadata):
            self.calls += 1
            raise AssertionError("provider must not be called for a missing report")

    import app.services.ai_stages as ai_stages
    from app.infrastructure.jobs import JobExecutionError

    spy = _FailSpy()
    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: spy)

    # Simulate compute replacing the report while the narrate job is queued:
    # compute once more, then run a job payload pointing at the deleted id.
    data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))

    with database.SessionLocal() as db:
        from app.models import Job

        job = Job(
            workspace_id=owner["workspace"]["id"],
            job_type="auto_report_narration",
            status="queued",
            progress=0,
            current_step="queued",
            input_json={"report_id": stale_report_id, "_actor_id": owner["user"]["id"]},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with database.SessionLocal() as db:
        context = __import__("app.services.job_handlers", fromlist=["JobContext"]).JobContext(db, job_id)
        registered = __import__("app.services.job_handlers", fromlist=["job_executor"]).job_executor
        handler = registered._handlers["auto_report_narration"].run
        try:
            handler(context)
        except JobExecutionError as exc:
            assert exc.code == "REPORT_MISSING"
            assert "取代" in str(exc)
        else:
            raise AssertionError("handler must raise REPORT_MISSING")
    assert spy.calls == 0


def test_artifact_window_caps_at_twenty(client, owner, project):
    _upload_grounding(client, owner, project)
    data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))
    with database.SessionLocal() as db:
        run = db.scalars(
            select(AnalysisRun).where(
                AnalysisRun.project_id == project["id"], AnalysisRun.status == "succeeded"
            )
        ).first()
        # Pad well past the window, then verify the reader truncates.
        for index in range(30):
            db.add(
                AnalysisArtifact(
                    analysis_run_id=run.id,
                    artifact_type="table",
                    title=f"pad {index}",
                    payload_json={"pad": index},
                )
            )
        db.commit()
        items = _grounding_artifacts(db, _orm_project(db, project))
    raw_items = [item for item in items if item["artifact_type"] != "dataset_summary"]
    assert len(raw_items) <= _ARTIFACT_CONTEXT_LIMIT
