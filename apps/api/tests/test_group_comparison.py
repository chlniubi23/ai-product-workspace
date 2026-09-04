"""Batch 12: group-comparison analysis and the findings digest wiring.

Covers the engine math (pure DataFrame), the auto-analysis plan selection
(business tables gain group_comparison, event/metrics tables unchanged, cap 4),
the report chain (digest persisted in ``deterministic_json`` and injected as
``finding`` artifacts into the narration context) and the document chain
(finding artifacts from the latest report).
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest
from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.analytics.engine import AnalysisEngine
from app.models import AIRun, AnalysisRun, AutoAnalysisReport, User
from app.schemas import DocumentGenerate
from app.services.documents import _build_document_context

# A business table: low-cardinality category (4/12 ≈ 0.33 ≤ 0.4) + numeric
# column, no event roles, plus a hole-y note column so the digest has a
# missing-rate finding to flag (25%).
CSV_BUSINESS = (
    "region,revenue,note\n"
    "华东,100,\n"
    "华东,150,\n"
    "华东,200,\n"
    "华北,40,\n"
    "华北,60,\n"
    "华北,80,\n"
    "华南,200,\n"
    "华南,120,\n"
    "西南,90,\n"
    "西南,110,\n"
    "西南,,\n"
    "西南,,\n"
)


def _batch(client, user, project_id: str, name: str, content: bytes):
    return client.post(
        "/api/v1/datasets/upload-batch",
        headers=auth(user),
        data={"project_id": project_id},
        files=[("files", (name, BytesIO(content), "text/csv"))],
    )


def _upload_business(client, owner, project):
    response = _batch(client, owner, project["id"], "business.csv", CSV_BUSINESS.encode())
    payload = data_of(response)
    return data_of(client.get(f"/api/v1/datasets/{payload['uploads'][0]['dataset']['id']}", headers=auth(owner)))

# --------------------------------------------------------------------------
# engine: pure DataFrame math
# --------------------------------------------------------------------------


def test_group_comparison_aggregates_and_shares():
    frame = pd.DataFrame(
        {
            "region": ["华东", "华东", "华北", "华南"],
            "revenue": [100.0, 300.0, 200.0, 300.0],
        }
    )
    artifact = AnalysisEngine("v-1").run_group_comparison(
        frame, group_column="region", value_column="revenue", aggregation="mean"
    )
    payload = artifact.payload
    rows = payload["categories"]
    assert payload["total_groups"] == 3
    assert [row["group"] for row in rows] == ["华东", "华南", "华北"]  # share desc
    assert rows[0] == {"group": "华东", "count": 2, "mean": 200.0, "sum": 400.0, "min": 100.0, "max": 300.0, "share": 0.444444}
    # shares are rounded to 6 decimals, so allow a rounding hair under 1.0
    assert abs(sum(row["share"] for row in rows) - 1.0) < 1e-5
    south = next(row for row in rows if row["group"] == "华南")
    assert south["count"] == 1 and south["mean"] == 300.0 and south["share"] == 0.333333
    assert artifact.payload["chart"]["type"] == "bar"
    assert artifact.fingerprint


def test_group_comparison_truncates_to_top_n():
    frame = pd.DataFrame(
        {"region": [f"r{i}" for i in range(6)], "revenue": [10.0 * (i + 1) for i in range(6)]}
    )
    payload = AnalysisEngine("v-1").run_group_comparison(
        frame, group_column="region", value_column="revenue", top_n=3
    ).payload
    assert len(payload["categories"]) == 3
    assert payload["categories"][0]["group"] == "r5"


def test_group_comparison_rejects_missing_columns_and_bad_input():
    frame = pd.DataFrame({"region": ["a"], "revenue": [1.0]})
    with pytest.raises(ValueError, match="required columns"):
        AnalysisEngine("v-1").run_group_comparison(frame, group_column="nope", value_column="revenue")
    with pytest.raises(ValueError, match="aggregation"):
        AnalysisEngine("v-1").run_group_comparison(frame, group_column="region", value_column="revenue", aggregation="median")
    with pytest.raises(ValueError, match="numeric value"):
        AnalysisEngine("v-1").run_group_comparison(
            pd.DataFrame({"region": ["a", None], "revenue": [None, None]}),
            group_column="region",
            value_column="revenue",
        )


# --------------------------------------------------------------------------
# auto plan: business tables gain group_comparison, cap raised to 4
# --------------------------------------------------------------------------


def _schema_column(name, inferred_type, unique_ratio, ordinal=0, role=None):
    return {"name": name, "inferred_type": inferred_type, "unique_ratio": unique_ratio, "nullable": False, "ordinal": ordinal, "mapping_role": role}


def test_plan_adds_group_comparison_for_business_tables():
    schema = [
        _schema_column("region", "string", 0.2, 0),
        _schema_column("revenue", "float", 0.9, 1),
    ]
    plan = __import__("app.services.analysis_pipeline", fromlist=["_auto_analysis_plan"])._auto_analysis_plan(schema)
    kinds = [entry["analysis_type"] for entry in plan]
    assert kinds == ["eda", "group", "anomaly", "group_comparison"]  # cap 4 in effect
    comparison = plan[-1]
    assert comparison["config"]["group_column"] == "region"
    assert comparison["config"]["value_column"] == "revenue"
    assert "pareto" in comparison["reason"]


def test_plan_keeps_event_tables_unchanged():
    schema = [
        _schema_column("user_id", "string", 0.9, 0, role="user_id"),
        _schema_column("event_time", "datetime", 0.9, 1, role="event_time"),
        _schema_column("event_name", "string", 0.05, 2, role="event_name"),
    ]
    plan = __import__("app.services.analysis_pipeline", fromlist=["_auto_analysis_plan"])._auto_analysis_plan(schema)
    kinds = [entry["analysis_type"] for entry in plan]
    assert kinds == ["eda", "retention"]  # no numeric column -> nothing else changes


def test_plan_caps_at_four_even_with_datetime_and_categories():
    schema = [
        _schema_column("day", "datetime", 0.9, 0),
        _schema_column("region", "string", 0.2, 1),
        _schema_column("dau", "integer", 0.9, 2),
    ]
    plan = __import__("app.services.analysis_pipeline", fromlist=["_auto_analysis_plan"])._auto_analysis_plan(schema)
    kinds = [entry["analysis_type"] for entry in plan]
    assert len(kinds) == 4
    assert kinds[0] == "eda" and kinds[1] == "trend" and kinds[2] == "anomaly"
    assert kinds[3] == "group_comparison"


# --------------------------------------------------------------------------
# HTTP: business upload -> persisted group_comparison artifact -> report digest
# --------------------------------------------------------------------------


def test_business_upload_persists_group_comparison_artifact(client, owner, project):
    _upload_business(client, owner, project)
    runs = data_of(
        client.get(f"/api/v1/analysis-runs?project_id={project['id']}&page_size=50", headers=auth(owner))
    )
    comparison_runs = [run for run in runs if run["analysis_type"] == "group_comparison"]
    assert comparison_runs, "group_comparison must be auto-selected for a business table"
    run = data_of(client.get(f"/api/v1/analysis-runs/{comparison_runs[0]['id']}", headers=auth(owner)))
    assert run["status"] == "succeeded"
    artifacts = run["artifacts"]
    assert artifacts, "group_comparison must persist a real artifact"
    payload = artifacts[0]["payload_json"]
    assert artifacts[0]["artifact_type"] == "chart"
    groups = payload["categories"]
    assert {row["group"] for row in groups} == {"华东", "华北", "华南", "西南"}
    assert abs(sum(row["share"] for row in groups) - 1.0) < 1e-6
    assert payload["chartType"] == "bar"


def test_report_persists_digest_and_narration_context_carries_findings(client, owner, project):
    _upload_business(client, owner, project)
    computed = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )["report"]

    deterministic = computed["deterministic_json"]
    findings = deterministic["findings"]
    assert isinstance(findings, list) and findings, "business table with a fully empty note column must fire"
    assert any(item["kind"] == "missing" for item in findings)
    # key findings section comes from the digest statements
    assert computed["key_findings"] == [item["statement"] for item in findings][: len(findings)]
    # note is 100% empty (severity 3) and revenue is 2/12 missing (severity 1)
    note_finding = next(item for item in findings if item["columns"] == ["note"])
    assert note_finding["severity"] == 3 and "100.0%" in note_finding["statement"]
    assert any("16.7%" in item["statement"] for item in findings if item["columns"] == ["revenue"])

    # breakdown rides in the first dataset's aggregates under an allowlisted key
    first = deterministic["datasets"][0]
    assert first["breakdown_column"] == "region ~ revenue"
    assert {row["group"] for row in first["breakdown"]} >= {"华东", "华南"}

    # narrate (no provider) still assembles the context: finding artifacts ride
    # the artifacts channel and pass the firewall assertion inside.
    data_of(client.post(f"/api/v1/auto-reports/{computed['id']}/narrate", headers=auth(owner)))
    with database.SessionLocal() as db:
        run = db.scalar(
            select(AIRun)
            .where(AIRun.feature_name == "auto_report_narration")
            .order_by(AIRun.created_at.desc())
            .limit(1)
        )
        assert run is not None
        context = run.input_summary_json["context"]
        finding_artifacts = [item for item in context["artifacts"] if item["artifact_type"] == "finding"]
        assert len(finding_artifacts) == len(findings)
        assert finding_artifacts[0]["id"] == "finding-1"
        assert finding_artifacts[0]["payload"]["metrics"]


def test_document_context_includes_findings_from_latest_report(client, owner, project):
    _upload_business(client, owner, project)
    report = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )["report"]
    findings = report["deterministic_json"]["findings"]

    with database.SessionLocal() as db:
        user = db.get(User, owner["user"]["id"])
        body = DocumentGenerate(project_id=project["id"], document_type="prd", title="第十二批验收", source_refs=[])
        context = _build_document_context(body, db, user)
    finding_artifacts = [item for item in context["safe_context"]["artifacts"] if item["artifact_type"] == "finding"]
    assert len(finding_artifacts) == len(findings)
    assert finding_artifacts[0]["title"] == findings[0]["statement"]
    with database.SessionLocal() as db:
        stored = db.get(AutoAnalysisReport, report["id"])
        assert stored is not None and stored.project_id == project["id"]
        runs = db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project["id"])).all()
    assert runs  # sanity: the business upload really produced analysis runs
