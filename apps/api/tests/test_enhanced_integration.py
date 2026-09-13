"""Phase 1 integration tests for the enhanced analytics pipeline.

These tests keep deterministic calculation, artifact shape and AI context
firewall contracts in one place. They use a plain DataFrame plus a lightweight
version object because the service layer builds these artifacts without a live
HTTP request.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from app.ai_context import assert_safe_ai_context, build_ai_context
from app.analytics.dag import build_lineage_map
from app.analytics.engine import AnalysisEngine
from app.analytics.quality import assess_quality
from app.services.ai_stages import _reduce_artifact_payload_for_ai
from app.services.analysis_pipeline import _analysis_artifacts, _analysis_config_validation
from app.services.datasets import _quality_summary

FIXTURE = "tests/fixtures/samples/weekly_report.csv"


def _version(frame: pd.DataFrame, extracted: set[str] | None = None):
    extracted = extracted or set()
    return SimpleNamespace(
        id="version-phase1-test",
        schema_json={
            "columns": [
                {"name": str(column), "source": "extracted" if str(column) in extracted else "original"}
                for column in frame.columns
            ]
        },
        columns=[],
    )


def _frame() -> pd.DataFrame:
    return pd.read_csv(FIXTURE)


def test_eda_uses_enhanced_correlation_and_preserves_legacy_shape():
    frame = _frame()
    lineage = build_lineage_map(list(frame.columns), {"渗透率(% 占周活)"})
    payload = AnalysisEngine().run_eda(frame, lineage_map=lineage).to_dict()["payload_json"]

    assert payload["correlations"]
    assert set(payload["correlations"][0]) == {"left", "right", "correlation"}
    assert payload["correlation_pairs_detail"]
    assert "pearson_p" in payload["correlation_pairs_detail"][0]
    assert "robust_pearson" in payload["correlation_pairs_detail"][0]
    assert payload["excluded_correlation_pairs"] > 0
    assert all("row_index" not in pair for pair in payload["correlation_pairs_detail"])

    excluded_names = {
        frozenset(("周活跃用户", "渗透率(% 占周活)")),
        frozenset(("渗透率(% 占周活)", "较上周活跃变化(%)")),
    }
    assert not any(frozenset((item["left"], item["right"])) in excluded_names for item in payload["correlations"])


def test_enhanced_analysis_types_build_safe_artifacts():
    frame = _frame()
    version = _version(frame, {"渗透率(% 占周活)"})
    expected_carriers = {
        "correlation_analysis": "pairs",
        "type_profile": "metrics",
        "outlier_objects": "metrics",
    }

    for kind, carrier in expected_carriers.items():
        validation = _analysis_config_validation(version, kind, {})
        assert validation["errors"] == []
        artifacts = _analysis_artifacts(frame, version, kind, {})
        assert len(artifacts) == 1
        payload = artifacts[0]["payload_json"]
        assert isinstance(payload[carrier], list)
        assert payload["chartType"]
        assert payload["option"]

        reduced = _reduce_artifact_payload_for_ai(payload)
        context = build_ai_context(artifacts=[{"id": kind, "title": kind, "payload": reduced}])
        safe = assert_safe_ai_context(context)
        context_json = json.dumps(safe, ensure_ascii=False, allow_nan=False)
        assert "row_index" not in context_json
        assert "min_row_index" not in context_json
        assert "max_row_index" not in context_json
        assert safe["artifacts"][0]["payload"].get(carrier)


def test_type_profile_keeps_ordinal_distribution_and_ratio_extremes_without_row_ids():
    frame = _frame()
    payload = _analysis_artifacts(frame, _version(frame), "type_profile", {})[0]["payload_json"]
    by_name = {item["name"]: item for item in payload["metrics"]}

    ordinal = by_name["满意度(1-5)"]
    assert ordinal["inferred_type"] == "ordinal"
    assert ordinal["distribution"]
    assert ordinal["mode"] is not None

    ratio = by_name["渗透率(% 占周活)"]
    assert ratio["inferred_type"] == "ratio"
    assert ratio["quantiles"]
    assert "min_value" in ratio and "max_value" in ratio
    assert "min_row_index" not in ratio and "max_row_index" not in ratio


def test_quality_gate_score_and_status_are_unchanged_while_dual_quality_is_added():
    frame = _frame()
    before = assess_quality(frame)
    score, status, summary = _quality_summary(frame)

    assert score == before.overall_score
    assert status == before.status
    assert set(summary["dual_quality"]) == {"summary", "recommendation", "data_quality", "analysis_quality"}
    assert summary["dual_quality"]["data_quality"]["score"] >= 0
    assert summary["dual_quality"]["analysis_quality"]["score"] >= 0


def test_ai_context_pairs_are_allowlisted_but_row_lists_remain_blocked():
    context = build_ai_context(
        artifacts=[
            {
                "id": "firewall-test",
                "title": "Firewall",
                "payload": {
                    "pairs": [{"var1": "a", "var2": "b", "pearson_p": 0.01}],
                    "metrics": [{"name": "a", "count": 2}],
                    "rows": [{"row_index": 3, "value": 99}],
                },
            }
        ]
    )
    safe = assert_safe_ai_context(context)
    payload = safe["artifacts"][0]["payload"]
    assert payload["pairs"] == [{"var1": "a", "var2": "b", "pearson_p": 0.01}]
    assert payload["metrics"] == [{"name": "a", "count": 2}]
    assert "rows" not in payload
    assert "row_index" not in json.dumps(safe)
