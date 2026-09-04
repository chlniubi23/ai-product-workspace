"""Unit tests for the rule-based findings digest (batch 12).

``build_findings_digest`` is a pure function over report aggregates: every rule
must fire on a constructed case, a clean dataset yields no findings, the output
is severity-ordered and capped, and statements carry the real numbers.
"""

from __future__ import annotations

from app.analytics.digest import DIGEST_MAX_FINDINGS, build_findings_digest


def _dataset(**overrides) -> dict:
    base = {
        "dataset_version_id": "v-1",
        "name": "业务表",
        "row_count": 100,
        "column_count": 3,
        "duplicate_rows": 0,
        "metrics": [],
    }
    base.update(overrides)
    return base


def test_high_missing_rate_fires_with_severity_and_number():
    aggregates = [
        _dataset(
            metrics=[
                {"name": "note", "type": "string", "missing_rate": 0.42, "unique_count": 5},
            ]
        )
    ]
    findings = build_findings_digest(aggregates)
    assert len(findings) == 1
    item = findings[0]
    assert item["kind"] == "missing"
    assert item["severity"] == 3  # >= 30% escalates
    assert "42.0%" in item["statement"]
    assert item["columns"] == ["note"]


def test_low_missing_rate_does_not_fire():
    aggregates = [_dataset(metrics=[{"name": "note", "type": "string", "missing_rate": 0.05, "unique_count": 5}])]
    assert build_findings_digest(aggregates) == []


def test_strong_correlation_fires_with_direction():
    aggregates = [_dataset(correlation_pairs={"price ~ revenue": 0.83, "a ~ b": -0.71, "c ~ d": 0.2})]
    findings = build_findings_digest(aggregates)
    assert [item["kind"] for item in findings] == ["correlation", "correlation"]
    assert findings[0]["value"] == 0.83
    assert "正相关" in findings[0]["statement"]
    assert "负相关" in findings[1]["statement"]
    assert findings[0]["columns"] == ["price ", " revenue"] or findings[0]["columns"] == ["price", "revenue"]


def test_correlation_keeps_only_strongest_two_pairs():
    aggregates = [_dataset(correlation_pairs={f"x{i} ~ y{i}": 0.9 - i * 0.02 for i in range(5)})]
    assert len(build_findings_digest(aggregates)) == 2


def test_extreme_trend_shift_fires():
    aggregates = [
        _dataset(
            trend={
                "metric_column": "dau",
                "first_value": 1000,
                "last_value": 500,
                "last_period_change": -0.5,
            }
        )
    ]
    findings = build_findings_digest(aggregates)
    assert len(findings) == 1
    assert findings[0]["kind"] == "trend_shift"
    assert findings[0]["severity"] == 3
    assert "下降" in findings[0]["statement"]
    assert "50.0%" in findings[0]["statement"]
    assert "1000" in findings[0]["statement"] and "500" in findings[0]["statement"]


def test_small_trend_shift_does_not_fire():
    aggregates = [_dataset(trend={"metric_column": "dau", "last_period_change": 0.12})]
    assert build_findings_digest(aggregates) == []


def test_category_concentration_fires():
    aggregates = [
        _dataset(
            metrics=[
                {
                    "name": "plan",
                    "type": "string",
                    "missing_rate": 0,
                    "unique_count": 3,
                    "categories": [{"value": "free", "count": 70, "rate": 0.7}],
                }
            ]
        )
    ]
    findings = build_findings_digest(aggregates)
    assert len(findings) == 1
    assert findings[0]["kind"] == "concentration"
    assert "free" in findings[0]["statement"]
    assert "70.0%" in findings[0]["statement"]


def test_high_duplicate_rate_fires():
    aggregates = [_dataset(row_count=200, duplicate_rows=24)]
    findings = build_findings_digest(aggregates)
    assert len(findings) == 1
    assert findings[0]["kind"] == "duplicate"
    assert "24 行" in findings[0]["statement"]
    assert "12.0%" in findings[0]["statement"]


def test_clean_dataset_yields_empty_digest():
    aggregates = [
        _dataset(
            metrics=[
                {"name": "plan", "type": "string", "missing_rate": 0.0, "unique_count": 2,
                 "categories": [{"value": "a", "count": 55, "rate": 0.55}]},
                {"name": "dau", "type": "integer", "missing_rate": 0.0, "unique_count": 90,
                 "statistics": {"count": 100, "mean": 10.0}},
            ],
            correlation_pairs={"a ~ b": 0.3},
            trend={"metric_column": "dau", "last_period_change": 0.05},
        )
    ]
    assert build_findings_digest(aggregates) == []


def test_digest_is_sorted_by_severity_then_value_and_capped():
    aggregates = [
        # many moderate concentration findings to exceed the cap
        {
            "dataset_version_id": "v-2",
            "name": f"表{i}",
            "row_count": 100,
            "column_count": 1,
            "duplicate_rows": 0,
            "metrics": [
                {"name": "plan", "type": "string", "missing_rate": 0.11, "unique_count": 2,
                 "categories": [{"value": "x", "count": 65, "rate": 0.65}]}
            ],
        }
        for i in range(15)
    ]
    findings = build_findings_digest(aggregates)
    assert len(findings) == DIGEST_MAX_FINDINGS
    severities = [item["severity"] for item in findings]
    assert severities == sorted(severities, reverse=True)
    for previous, current in zip(findings, findings[1:], strict=False):
        if previous["severity"] == current["severity"] and previous["kind"] == current["kind"]:
            assert abs(previous["value"]) >= abs(current["value"])
    # each dataset fires both rules; the cap keeps only the severity-2
    # concentration findings (65.0%), not the severity-1 missing ones (11.0%)
    assert all(item["kind"] == "concentration" for item in findings)
    assert all("65.0%" in item["statement"] for item in findings)
