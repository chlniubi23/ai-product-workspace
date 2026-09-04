"""Rule-based findings digest over precomputed report aggregates.

``build_findings_digest`` reads the per-dataset aggregate dicts produced by
``_compute_report_aggregates`` (same shape as ``deterministic_json["datasets"]``)
and distills the most important observations into a short, severity-ordered
list.  Every statement is Chinese, contains the real numbers it is derived
from, and never invents a metric that is not present in the input.  The digest
feeds the deterministic report's key-findings section and the AI narration /
document contexts as ``finding`` artifacts.
"""

from __future__ import annotations

from typing import Any

# Rule thresholds are fixed for this iteration -- deliberately module constants
# rather than settings so "what counts as a finding" cannot drift quietly.
DIGEST_MAX_FINDINGS = 12
MISSING_RATE_THRESHOLD = 0.10
CORRELATION_THRESHOLD = 0.60
CORRELATION_TOP_PAIRS = 2
TREND_SHIFT_THRESHOLD = 0.30
CONCENTRATION_THRESHOLD = 0.60
DUPLICATE_RATE_THRESHOLD = 0.05

_KIND_ORDER = {
    "missing": 0,
    "correlation": 1,
    "trend_shift": 2,
    "concentration": 3,
    "duplicate": 4,
}


def _pct(rate: float) -> str:
    return f"{round(float(rate) * 100, 1)}%"


def _missing_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    for column in dataset.get("metrics") or []:
        if not isinstance(column, dict):
            continue
        rate = column.get("missing_rate")
        if not isinstance(rate, (int, float)) or rate < MISSING_RATE_THRESHOLD:
            continue
        severity = 3 if rate >= 0.30 else 2 if rate >= 0.20 else 1
        findings.append(
            {
                "kind": "missing",
                "dataset": str(dataset.get("name") or ""),
                "statement": f"「{dataset.get('name')}」字段 {column.get('name')} 缺失率高达 {_pct(rate)}，分析结论受其完整性影响。",
                "severity": severity,
                "columns": [str(column.get("name"))],
                "value": round(float(rate), 4),
            }
        )


def _correlation_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    pairs = dataset.get("correlation_pairs")
    if not isinstance(pairs, dict):
        return
    candidates: list[tuple[str, float]] = []
    for label, value in pairs.items():
        if not isinstance(value, (int, float)) or abs(float(value)) < CORRELATION_THRESHOLD:
            continue
        candidates.append((str(label), float(value)))
    candidates.sort(key=lambda item: abs(item[1]), reverse=True)
    for label, value in candidates[:CORRELATION_TOP_PAIRS]:
        direction = "正" if value > 0 else "负"
        severity = 3 if abs(value) >= 0.80 else 2
        findings.append(
            {
                "kind": "correlation",
                "dataset": str(dataset.get("name") or ""),
                "statement": f"「{dataset.get('name')}」中 {label} 呈{direction}相关（r={round(value, 2)}）；相关不代表因果。",
                "severity": severity,
                "columns": [part.strip() for part in label.split("~")],
                "value": round(value, 4),
            }
        )


def _trend_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    trend = dataset.get("trend")
    if not isinstance(trend, dict):
        return
    change = trend.get("last_period_change")
    if not isinstance(change, (int, float)) or abs(float(change)) < TREND_SHIFT_THRESHOLD:
        return
    direction = "上升" if change > 0 else "下降"
    severity = 3 if abs(float(change)) >= 0.50 else 2
    findings.append(
        {
            "kind": "trend_shift",
            "dataset": str(dataset.get("name") or ""),
            "statement": (
                f"「{dataset.get('name')}」{trend.get('metric_column')} 最近一期环比{direction} "
                f"{_pct(abs(float(change)))}（{trend.get('first_value')} → {trend.get('last_value')}）。"
            ),
            "severity": severity,
            "columns": [str(trend.get("metric_column"))],
            "value": round(float(change), 4),
        }
    )


def _concentration_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    for column in dataset.get("metrics") or []:
        if not isinstance(column, dict):
            continue
        categories = column.get("categories") or []
        if not categories or not isinstance(categories[0], dict):
            continue
        top = categories[0]
        rate = top.get("rate")
        if not isinstance(rate, (int, float)) or rate < CONCENTRATION_THRESHOLD:
            continue
        severity = 3 if rate >= 0.80 else 2
        findings.append(
            {
                "kind": "concentration",
                "dataset": str(dataset.get("name") or ""),
                "statement": (
                    f"「{dataset.get('name')}」{column.get('name')} 高度集中于「{top.get('value')}」"
                    f"（{top.get('count')} 条，占 {_pct(rate)}）。"
                ),
                "severity": severity,
                "columns": [str(column.get("name"))],
                "value": round(float(rate), 4),
            }
        )


def _duplicate_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    duplicates = dataset.get("duplicate_rows")
    row_count = dataset.get("row_count")
    if not isinstance(duplicates, int) or not isinstance(row_count, int) or row_count <= 0:
        return
    rate = duplicates / row_count
    if rate < DUPLICATE_RATE_THRESHOLD:
        return
    findings.append(
        {
            "kind": "duplicate",
            "dataset": str(dataset.get("name") or ""),
            "statement": f"「{dataset.get('name')}」存在 {duplicates} 行重复记录（占 {_pct(rate)}），统计口径可能被重复放大。",
            "severity": 2,
            "columns": [],
            "value": round(rate, 4),
        }
    )


def build_findings_digest(aggregates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distill a severity-ordered findings list from report aggregates.

    Rules (thresholds above): high missing rates, strong numeric correlations
    (strongest pairs per dataset), extreme last-period trend shifts, top-class
    concentration, and heavy duplication.  Output is capped at
    ``DIGEST_MAX_FINDINGS``, sorted by severity descending then by absolute
    value descending, so the first entries are always the loudest signals.
    """

    findings: list[dict[str, Any]] = []
    for dataset in aggregates or []:
        if not isinstance(dataset, dict):
            continue
        _missing_findings(dataset, findings)
        _correlation_findings(dataset, findings)
        _trend_findings(dataset, findings)
        _concentration_findings(dataset, findings)
        _duplicate_findings(dataset, findings)
    findings.sort(
        key=lambda item: (
            -int(item.get("severity") or 0),
            _KIND_ORDER.get(str(item.get("kind")), 9),
            -abs(float(item.get("value") or 0.0)),
        )
    )
    return findings[:DIGEST_MAX_FINDINGS]
