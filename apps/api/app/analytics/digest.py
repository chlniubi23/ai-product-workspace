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
# Batch 14 additions.
OUTLIER_RATE_THRESHOLD = 0.05
OUTLIER_SEVERE_THRESHOLD = 0.15
CALENDAR_GAP_MIN = 1

_KIND_ORDER = {
    "missing": 0,
    "correlation": 1,
    "trend_shift": 2,
    "concentration": 3,
    "duplicate": 4,
    "outlier": 5,
    "constant": 6,
    "calendar_gap": 7,
    "small_sample": 8,
}


def _pct(rate: float) -> str:
    return f"{round(float(rate) * 100, 1)}%"


# ---------------------------------------------------------------------------
# Batch 21: business-label display helpers.  The field-semantics dictionary
# puts an optional "label" on every metric entry (and "dataset_label" on the
# dataset); statements then read "attendee_count（参会人数）".  Without a
# label the output is byte-identical to the pre-21 statements.
# ---------------------------------------------------------------------------


def display_name(name: Any, label: Any) -> str:
    name = str(name or "")
    label = str(label or "").strip()
    return f"{name}（{label}）" if label else name


def dataset_display(dataset: dict[str, Any]) -> str:
    return display_name(dataset.get("name"), dataset.get("dataset_label"))


def column_display(dataset: dict[str, Any], column_name: Any) -> str:
    """Display one column name with its label, looked up in the dataset's
    metrics entries (used where only the raw name is at hand, e.g. trends)."""

    name = str(column_name or "")
    for column in dataset.get("metrics") or []:
        if isinstance(column, dict) and str(column.get("name") or "") == name:
            return display_name(name, column.get("label"))
    return name


def _missing_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    for column in dataset.get("metrics") or []:
        if not isinstance(column, dict):
            continue
        # Batch 20: extracted columns ("src__label") are derived -- their
        # missing cells reflect extraction coverage (already disclosed in the
        # extraction report), not source-data quality.
        if "__" in str(column.get("name") or ""):
            continue
        rate = column.get("missing_rate")
        if not isinstance(rate, (int, float)) or rate < MISSING_RATE_THRESHOLD:
            continue
        severity = 3 if rate >= 0.30 else 2 if rate >= 0.20 else 1
        findings.append(
            {
                "kind": "missing",
                "dataset": str(dataset.get("name") or ""),
                "statement": f"「{dataset_display(dataset)}」字段 {display_name(column.get('name'), column.get('label'))} 缺失率高达 {_pct(rate)}，分析结论受其完整性影响。",
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
        # Batch 21: render both sides of the pair with their business labels.
        pair_display = " ~ ".join(column_display(dataset, part.strip()) for part in label.split("~"))
        findings.append(
            {
                "kind": "correlation",
                "dataset": str(dataset.get("name") or ""),
                "statement": f"「{dataset_display(dataset)}」中 {pair_display} 呈{direction}相关（r={round(value, 2)}）；相关不代表因果。",
                "severity": severity,
                "columns": [part.strip() for part in label.split("~")],
                "value": round(value, 4),
            }
        )


def _excluded_correlation_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """Expose pseudo-correlation exclusions as an auditable, bounded finding."""

    count = dataset.get("excluded_correlation_pairs")
    if not isinstance(count, (int, float)) or int(count) <= 0:
        return
    findings.append(
        {
            "kind": "pseudo_correlation_excluded",
            "dataset": str(dataset.get("name") or ""),
            "statement": f"「{dataset_display(dataset)}」已排除 {int(count)} 对派生列/机械相关，避免将数学必然性误判为业务洞察。",
            "severity": 1,
            "columns": [],
            "value": int(count),
        }
    )


def _trend_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    trend = dataset.get("trend")
    if not isinstance(trend, dict):
        return
    change = trend.get("last_period_change")
    if not isinstance(change, (int, float)) or abs(float(change)) < TREND_SHIFT_THRESHOLD:
        return
    # Batch 20: honest period-over-period.  The numbers quoted in the
    # statement MUST be the same two rows the percentage is computed from
    # (previous_value -> last_value, period label included) -- mixing the
    # percentage with the whole-span first/last values produced findings that
    # contradicted themselves.  Thin samples are disclosed and downgraded.
    previous_value = trend.get("previous_value")
    last_value = trend.get("last_value")
    if previous_value is None or last_value is None:
        return  # honest default: no comparable previous period, no claim
    direction = "上升" if change > 0 else "下降"
    severity = 3 if abs(float(change)) >= 0.50 else 2
    metric_column = str(trend.get("metric_column") or "")
    # Batch 14: a derived metric column (source__label) makes this an
    # extracted-metric trend finding -- same rule, marked provenance.
    derived_mark = "（抽取指标）" if "__" in metric_column else ""
    # Batch 21: the metric column reads with its business label when present.
    metric_display = column_display(dataset, metric_column)
    period_label = str(trend.get("last_period") or "最近一期")
    previous_count = trend.get("previous_count")
    last_count = trend.get("last_count")
    thin_sample = (
        isinstance(previous_count, int)
        and isinstance(last_count, int)
        and previous_count + last_count < 3
    )
    if thin_sample:
        severity = 1
    sample_note = "（样本量较小，仅供参考）" if thin_sample else ""
    findings.append(
        {
            "kind": "trend_shift",
            "dataset": str(dataset.get("name") or ""),
            "statement": (
                f"「{dataset_display(dataset)}」{metric_display}{derived_mark} "
                f"最近一期（{period_label}）环比{direction} {_pct(abs(float(change)))}"
                f"（上期 {previous_value} → 本期 {last_value}）{sample_note}。"
            ),
            "severity": severity,
            "columns": [metric_column] if metric_column else [],
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
                    f"「{dataset_display(dataset)}」{display_name(column.get('name'), column.get('label'))} 高度集中于「{top.get('value')}」"
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


def _constant_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """One merged finding per dataset for its constant source columns
    (batch 20: extracted columns are skipped -- a derived metric repeating
    one value is an extraction artifact, not a data-quality signal)."""

    names = [
        str(column.get("name"))
        for column in dataset.get("metrics") or []
        if isinstance(column, dict) and column.get("constant") and "__" not in str(column.get("name") or "")
    ]
    if not names:
        return
    # Batch 21: the statement reads the business labels; the columns field
    # keeps the raw names so downstream evidence scope checks still match.
    names_display = "、".join(column_display(dataset, name) for name in names)
    findings.append(
        {
            "kind": "constant",
            "dataset": str(dataset.get("name") or ""),
            "statement": f"「{dataset_display(dataset)}」{len(names)} 个字段内容完全固化（{names_display}），不构成区分维度。",
            "severity": 1,
            "columns": names,
            "value": float(len(names)),
        }
    )


def _outlier_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """Numeric columns whose outlier share clears the threshold.

    ``column["outliers"]`` is produced by ``auto_report`` from the single outlier
    definition in ``analytics.outliers`` (IQR bounds **union** extreme z values,
    de-duplicated per row), and ``statistics["count"]`` is the non-null sample
    size -- the same denominator the quality report uses.  The statement says
    "离群值" rather than "IQR 离群值" so the wording matches that definition.
    """

    for column in dataset.get("metrics") or []:
        if not isinstance(column, dict):
            continue
        outliers = column.get("outliers")
        statistics = column.get("statistics") if isinstance(column.get("statistics"), dict) else {}
        valid = statistics.get("count")
        if not isinstance(outliers, int) or not isinstance(valid, int) or valid <= 0:
            continue
        rate = outliers / valid
        if rate < OUTLIER_RATE_THRESHOLD:
            continue
        severity = 3 if rate >= OUTLIER_SEVERE_THRESHOLD else 2
        findings.append(
            {
                "kind": "outlier",
                "dataset": str(dataset.get("name") or ""),
                "statement": f"「{dataset_display(dataset)}」{display_name(column.get('name'), column.get('label'))} 有 {outliers} 个离群值（占 {_pct(rate)}），均值类结论可能被拉偏。",
                "severity": severity,
                "columns": [str(column.get("name"))],
                "value": round(rate, 4),
            }
        )


def _calendar_gap_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """Batch 14: missing calendar periods inside an existing trend."""

    trend = dataset.get("trend")
    if not isinstance(trend, dict):
        return
    gaps = trend.get("gaps")
    if not isinstance(gaps, int) or gaps < CALENDAR_GAP_MIN:
        return
    findings.append(
        {
            "kind": "calendar_gap",
            "dataset": str(dataset.get("name") or ""),
            "statement": f"「{dataset.get('name')}」的时间序列存在 {gaps} 个缺失期，趋势与环比结论在缺口处不连续。",
            "severity": 1,
            "columns": [str(trend.get("metric_column") or "")],
            "value": float(gaps),
        }
    )


def _small_sample_findings(dataset: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """批 33：小样本数据集的结构化说明（聚合层已停用跨行统计）。"""

    if not dataset.get("small_sample"):
        return
    row_count = int(dataset.get("row_count") or 0)
    findings.append(
        {
            "kind": "small_sample",
            "dataset": str(dataset.get("name") or ""),
            "statement": f"「{dataset_display(dataset)}」仅 {row_count} 行，跨行统计已停用，避免小样本误判。",
            "severity": 1,
            "value": float(row_count),
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
        _excluded_correlation_findings(dataset, findings)
        _trend_findings(dataset, findings)
        _concentration_findings(dataset, findings)
        _duplicate_findings(dataset, findings)
        _constant_findings(dataset, findings)
        _outlier_findings(dataset, findings)
        _calendar_gap_findings(dataset, findings)
        _small_sample_findings(dataset, findings)
    findings.sort(
        key=lambda item: (
            -int(item.get("severity") or 0),
            _KIND_ORDER.get(str(item.get("kind")), 9),
            -abs(float(item.get("value") or 0.0)),
        )
    )
    return findings[:DIGEST_MAX_FINDINGS]
