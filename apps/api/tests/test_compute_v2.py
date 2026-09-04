"""Batch 14 pipeline integration on the committed synthetic fixture.

The fixture (business_table_text_metrics.csv: 30 rows x 8 columns) covers ISO
and Chinese dates, thousands/currency amounts, a percent column, category,
identifier and constant columns, and a free-text metrics column.  These tests
pin the whole v2 chain: parse -> derived data_columns (source=extracted) ->
schema_json extraction report -> auto plan picks trend -> report aggregates
carry derived statistics and distribution depth -> digest v2 rules.
"""

from __future__ import annotations

from io import BytesIO

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.analytics.digest import build_findings_digest
from app.models import DataColumn

FIXTURE = "business_table_text_metrics.csv"


def _upload_fixture(client, owner, project):
    path = __import__("pathlib").Path(__file__).parent / "fixtures" / "samples" / FIXTURE
    response = client.post(
        "/api/v1/datasets/upload-batch",
        headers=auth(owner),
        data={"project_id": project["id"]},
        files=[("files", (FIXTURE, BytesIO(path.read_bytes()), "text/csv"))],
    )
    payload = data_of(response)
    dataset_id = payload["uploads"][0]["dataset"]["id"]
    return data_of(client.get(f"/api/v1/datasets/{dataset_id}", headers=auth(owner)))


def test_parse_lands_derived_columns_and_extraction_report(client, owner, project):
    dataset = _upload_fixture(client, owner, project)
    version = dataset["versions"][-1]
    assert version["schema_json"]["text_metric_extraction"], "extraction report persisted"
    report_rows = version["schema_json"]["text_metric_extraction"]
    derived_columns = {row["derived_column"]: row for row in report_rows}
    assert "metrics_summary__DAU" in derived_columns
    assert derived_columns["metrics_summary__DAU"]["coverage"] > 0.8
    assert derived_columns["metrics_summary__DAU"]["unit_note"] == "k"

    with database.SessionLocal() as db:
        columns = db.scalars(
            select(DataColumn).where(DataColumn.dataset_version_id == version["id"])
        ).all()
    by_name = {column.name: column for column in columns}
    assert by_name["metrics_summary__DAU"].source == "extracted"
    assert by_name["metrics_summary__DAU"].inferred_type == "float"
    assert by_name["region"].source == "original"
    # original file shape is untouched: 8 uploaded columns, derived are extra
    assert len(dataset["versions"][-1]["columns"]) > 8

    runs = data_of(
        client.get(f"/api/v1/analysis-runs?project_id={project['id']}&page_size=50", headers=auth(owner))
    )
    types = {run["analysis_type"] for run in runs if run["status"] == "succeeded"}
    assert "trend" in types, "derived numeric columns unlock the trend analysis"
    assert "group_comparison" in types


def test_report_aggregates_carry_derived_stats_and_depth(client, owner, project):
    _upload_fixture(client, owner, project)
    report = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )["report"]
    deterministic = report["deterministic_json"]
    first = deterministic["datasets"][0]
    metrics = {item["name"]: item for item in first["metrics"]}

    dau = metrics["metrics_summary__DAU"]
    assert dau["source"] == "extracted"
    assert dau["statistics"]["mean"] > 100000  # magnitude suffix actually parsed
    assert "outliers" in dau and "skewness" in dau
    assert dau["counts"] and dau["bins"]
    assert metrics["region"]["source"] == "original"
    assert metrics["cost_center"]["constant"] is True

    # trend exists on the fixture (ISO week_start + numeric columns) and the
    # deterministic overview labels derived metrics
    assert first["trend"]["metric_column"] in {"amount", "metrics_summary__DAU", "growth"}
    assert "（抽取）" in report["content_markdown"] or any(
        "（抽取）" in section.get("content", "")
        for section in report["sections_json"]
    )


def test_digest_v2_rules_fire_on_the_fixture(client, owner, project):
    _upload_fixture(client, owner, project)
    report = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )["report"]
    findings = report["deterministic_json"]["findings"]
    kinds = {item["kind"] for item in findings}
    assert "constant" in kinds, "cost_center is constant -> one merged finding"
    constant = next(item for item in findings if item["kind"] == "constant")
    assert constant["columns"] == ["cost_center"]
    assert "1 个字段" in constant["statement"]

    gaps = [item for item in findings if item["kind"] == "calendar_gap"]
    # the fixture has a continuous calendar; if no gap exists the rule must
    # simply not fire -- either way it must never crash the digest
    assert all(item["value"] >= 1 for item in gaps)


def test_digest_v2_unit_rules():
    dataset = {
        "dataset_version_id": "v-1",
        "name": "周报",
        "row_count": 100,
        "column_count": 3,
        "duplicate_rows": 0,
        "metrics": [
            {"name": "cost_center", "type": "string", "missing_rate": 0, "unique_count": 1, "constant": True},
            {
                "name": "orders",
                "type": "float",
                "missing_rate": 0,
                "unique_count": 90,
                "source": "extracted",
                "statistics": {"count": 100, "mean": 50.0},
                "outliers": 18,
            },
        ],
        "trend": {
            "metric_column": "metrics_summary__DAU",
            "first_value": 100000,
            "last_value": 60000,
            "last_period_change": -0.4,
            "gaps": 2,
        },
    }
    findings = build_findings_digest([dataset])
    kinds = {item["kind"]: item for item in findings}
    assert kinds["constant"]["columns"] == ["cost_center"]
    assert kinds["outlier"]["severity"] == 3  # 18% >= severe threshold
    assert "（抽取指标）" in kinds["trend_shift"]["statement"]
    assert kinds["calendar_gap"]["value"] == 2.0
    assert "2 个缺失期" in kinds["calendar_gap"]["statement"]
