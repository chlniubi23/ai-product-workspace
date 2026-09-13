"""Deterministic data quality checks, plus enhanced dual-dimension quality assessment.

The first part of this module preserves the original, stable quality contract
(``QualityReport`` / ``assess_quality``) used across the API and background jobs.
The second part adds the batch: split quality into a *data quality* score
(completeness/type compliance) and an *analysis quality* score (method coverage),
so callers no longer read a single number as if it meant both.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .parsing import infer_column_type_v2

_TYPE_ALIASES = {
    "numeric": "numeric",
    "number": "numeric",
    "float": "numeric",
    "integer": "numeric",
    "int": "numeric",
    "date": "datetime",
    "datetime64": "datetime",
    "datetime": "datetime",
    "timestamp": "datetime",
    "string": "categorical",
    "category": "categorical",
    "bool": "boolean",
    "boolean": "boolean",
}


def _finite(value: Any) -> Any:
    """Convert NumPy scalars and non-finite values to JSON-safe Python values."""

    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (np.generic,)):
        value = value.item()
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return value.isoformat()
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return _finite(value)


def infer_column_type(series: pd.Series) -> str:
    """Infer one of the product's stable display types (legacy contract)."""

    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    semantic = infer_column_type_v2(series)["semantic_type"]
    if semantic in {"numeric", "datetime", "boolean"}:
        return semantic
    return "categorical"


@dataclass(frozen=True)
class QualityReport:
    """Serializable quality report returned by :func:`assess_quality`."""

    row_count: int
    column_count: int
    overall_score: float
    status: str
    columns: list[dict[str, Any]]
    duplicate_rows: int
    duplicate_key_rows: int
    outliers: dict[str, dict[str, Any]]
    type_errors: list[dict[str, Any]]
    summary: dict[str, Any]
    sample: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return _jsonable({
            "row_count": self.row_count,
            "column_count": self.column_count,
            "overall_score": self.overall_score,
            "status": self.status,
            "columns": self.columns,
            "duplicate_rows": self.duplicate_rows,
            "duplicate_key_rows": self.duplicate_key_rows,
            "outliers": self.outliers,
            "type_errors": self.type_errors,
            "summary": self.summary,
            "sample": self.sample,
        })


def _as_dataframe(data: pd.DataFrame | Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    if isinstance(data, pd.DataFrame):
        return data.copy()
    return pd.DataFrame(list(data))


def assess_quality(
    data: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    expected_types: Mapping[str, str] | None = None,
    key_columns: Sequence[str] | None = None,
    iqr_multiplier: float = 1.5,
    z_threshold: float = 3.0,
    sample_size: int = 10,
) -> QualityReport:
    """Run deterministic missing, duplicate, type and outlier checks.

    The input is copied.  Empty strings and whitespace-only strings are counted as
    missing for quality metrics, but the original values are not changed.  Duplicate
    rows are reported only; no rows are removed automatically.
    """

    if iqr_multiplier <= 0 or z_threshold <= 0:
        raise ValueError("outlier thresholds must be positive")
    frame = _as_dataframe(data)
    frame_for_checks = frame.copy()
    for column in frame_for_checks.select_dtypes(include=["object", "string"]).columns:
        frame_for_checks[column] = frame_for_checks[column].replace(r"^\s*$", np.nan, regex=True)

    rows, columns = frame_for_checks.shape
    column_reports: list[dict[str, Any]] = []
    type_errors: list[dict[str, Any]] = []
    expected_types = expected_types or {}

    for name in frame_for_checks.columns:
        series = frame_for_checks[name]
        inferred = infer_column_type(series)
        missing = int(series.isna().sum())
        unique = int(series.nunique(dropna=True))
        report: dict[str, Any] = {
            "name": str(name),
            "inferred_type": inferred,
            "missing_count": missing,
            "missing_rate": (missing / rows if rows else 0.0),
            "unique_count": unique,
            "unique_ratio": (unique / rows if rows else 0.0),
            "nullable": bool(missing),
        }

        requested = expected_types.get(name)
        if requested:
            requested_normalized = _TYPE_ALIASES.get(str(requested).lower(), str(requested).lower())
            invalid = 0
            if requested_normalized == "numeric":
                candidate = pd.to_numeric(series, errors="coerce")
                invalid = int((series.notna() & candidate.isna()).sum())
            elif requested_normalized == "datetime":
                candidate = pd.to_datetime(series, errors="coerce", utc=True)
                invalid = int((series.notna() & candidate.isna()).sum())
            elif requested_normalized == "boolean":
                allowed = {True, False, "0", "1", "true", "false", "True", "False"}
                invalid = int(sum(value not in allowed for value in series.dropna().tolist()))
            elif requested_normalized != inferred:
                invalid = int(series.notna().sum())
            report["confirmed_type"] = requested_normalized
            report["type_error_count"] = invalid
            if invalid:
                type_errors.append({
                    "column": str(name),
                    "expected_type": requested_normalized,
                    "invalid_count": invalid,
                })
        column_reports.append(report)

    duplicate_rows = int(frame_for_checks.duplicated(keep="first").sum())
    duplicate_key_rows = 0
    if key_columns:
        missing_keys = [column for column in key_columns if column not in frame_for_checks.columns]
        if missing_keys:
            raise ValueError(f"key columns not found: {', '.join(map(str, missing_keys))}")
        duplicate_key_rows = int(frame_for_checks.duplicated(list(key_columns), keep=False).sum())

    outliers: dict[str, dict[str, Any]] = {}
    outlier_fraction = 0.0
    numeric_columns = frame_for_checks.select_dtypes(include=[np.number]).columns
    for name in numeric_columns:
        values = pd.to_numeric(frame_for_checks[name], errors="coerce").dropna()
        if values.empty:
            outliers[str(name)] = {"method": "iqr_and_zscore", "count": 0, "indices": [], "bounds": {}}
            continue
        q1 = float(values.quantile(0.25))
        q3 = float(values.quantile(0.75))
        iqr = q3 - q1
        lower = q1 - iqr_multiplier * iqr
        upper = q3 + iqr_multiplier * iqr
        iqr_mask = (frame_for_checks[name] < lower) | (frame_for_checks[name] > upper)
        mean = float(values.mean())
        std = float(values.std(ddof=0))
        if std > 0:
            z_mask = (frame_for_checks[name] - mean).abs() > z_threshold * std
        else:
            z_mask = pd.Series(False, index=frame_for_checks.index)
        mask = (iqr_mask | z_mask).fillna(False)
        indices = [int(index) if isinstance(index, (int, np.integer)) else str(index) for index in frame_for_checks.index[mask]]
        count = len(indices)
        outliers[str(name)] = {
            "method": "iqr_and_zscore",
            "count": count,
            "rate": (count / rows if rows else 0.0),
            "indices": indices[:100],
            "bounds": {"iqr_lower": lower, "iqr_upper": upper, "z_threshold": z_threshold},
        }
        outlier_fraction += count / max(rows, 1)
    if numeric_columns.size:
        outlier_fraction /= float(numeric_columns.size)

    missing_fraction = float(sum(item["missing_count"] for item in column_reports)) / max(rows * max(columns, 1), 1)
    duplicate_fraction = duplicate_rows / max(rows, 1)
    type_error_fraction = sum(item["invalid_count"] for item in type_errors) / max(rows * max(columns, 1), 1)
    penalty = min(100.0, 100.0 * (0.4 * missing_fraction + 0.25 * duplicate_fraction + 0.2 * type_error_fraction + 0.15 * min(outlier_fraction, 1.0)))
    score = round(max(0.0, 100.0 - penalty), 2)
    status = "passed" if score >= 95 else "needs_review" if score >= 80 else "failed"
    summary = {
        "missing_cells": int(sum(item["missing_count"] for item in column_reports)),
        "duplicate_rows": duplicate_rows,
        "duplicate_key_rows": duplicate_key_rows,
        "type_error_count": int(sum(item["invalid_count"] for item in type_errors)),
        "outlier_cells": int(sum(item["count"] for item in outliers.values())),
        "checks": ["missing", "duplicates", "type_errors", "outliers"],
    }
    sample = frame.head(max(0, sample_size)).replace({np.nan: None}).to_dict(orient="records")
    return QualityReport(
        row_count=int(rows),
        column_count=int(columns),
        overall_score=score,
        status=status,
        columns=column_reports,
        duplicate_rows=duplicate_rows,
        duplicate_key_rows=duplicate_key_rows,
        outliers=outliers,
        type_errors=type_errors,
        summary=summary,
        sample=_jsonable(sample),
    )


run_quality_checks = assess_quality
check_data_quality = assess_quality


# ============================================================================
# Enhanced dual-dimension quality assessment (batch: split the single score)
# ============================================================================


@dataclass
class DataQualityMetrics:
    """数据质量指标：完整性、类型合规、缺失率（原质量分真正衡量的维度）。"""

    completeness_score: float
    type_compliance_score: float
    missing_rate_overall: float
    n_columns: int
    n_rows: int
    duplicate_rows: int

    @property
    def overall_score(self) -> float:
        weighted = self.completeness_score * 0.6 + self.type_compliance_score * 0.4
        return round(min(100, max(0, weighted)), 1)

    def to_dict(self) -> dict:
        return {
            "score": self.overall_score,
            "completeness": round(self.completeness_score, 1),
            "type_compliance": round(self.type_compliance_score, 1),
            "missing_rate": round(self.missing_rate_overall, 4),
            "n_columns": self.n_columns,
            "n_rows": self.n_rows,
            "duplicate_rows": self.duplicate_rows,
            "label": "数据质量",
        }


@dataclass
class AnalysisQualityMetrics:
    """分析质量指标：统计方法是否完备、口径是否正确（新维度）。"""

    has_outlier_detection: bool
    correlation_tests_completed: int
    has_robust_correlation: bool
    ordinal_cols_handled_properly: int
    ratio_cols_explicitly_marked: int

    @property
    def overall_score(self) -> float:
        base = 0
        if self.has_outlier_detection:
            base += 30
        if self.correlation_tests_completed > 0:
            base += min(30, self.correlation_tests_completed * 5)
        if self.has_robust_correlation:
            base += 15
        if self.ordinal_cols_handled_properly > 0:
            base += min(15, self.ordinal_cols_handled_properly * 5)
        if self.ratio_cols_explicitly_marked > 0:
            base += min(10, self.ratio_cols_explicitly_marked * 3)
        return round(base, 1)

    def to_dict(self) -> dict:
        return {
            "score": self.overall_score,
            "outlier_detection": self.has_outlier_detection,
            "correlation_tests": self.correlation_tests_completed,
            "robust_analysis": self.has_robust_correlation,
            "ordinal_handling": self.ordinal_cols_handled_properly,
            "ratio_marking": self.ratio_cols_explicitly_marked,
            "label": "分析质量",
            "note": "反映统计分析方法的完整性与口径正确性",
        }


@dataclass
class ComprehensiveQualityReport:
    """完整质量评估：数据质量 + 分析质量 两个独立维度。"""

    data_quality: DataQualityMetrics
    analysis_quality: AnalysisQualityMetrics

    @property
    def recommendation(self) -> str:
        issues: list[str] = []
        if self.data_quality.missing_rate_overall > 0.1:
            issues.append("填充或减少关键列的缺失值")
        if not self.analysis_quality.has_outlier_detection:
            issues.append("添加离群值检测")
        if self.analysis_quality.correlation_tests_completed < 3:
            issues.append("增加相关性分析深度")
        if self.analysis_quality.ordinal_cols_handled_properly == 0:
            issues.append("为序数量表列输出分布而非均值")
        return ", ".join(issues) if issues else "无明显改进空间"

    @property
    def summary(self) -> str:
        dq = self.data_quality.overall_score
        aq = self.analysis_quality.overall_score
        if dq >= 90 and aq >= 85:
            level = "优秀"
        elif dq >= 75 and aq >= 70:
            level = "良好"
        elif dq >= 60 and aq >= 55:
            level = "合格"
        else:
            level = "待改进"
        return f"{level} | 数据质量{dq}, 分析质量{aq} | 建议：{self.recommendation}"

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "recommendation": self.recommendation,
            "data_quality": self.data_quality.to_dict(),
            "analysis_quality": self.analysis_quality.to_dict(),
        }


def compute_data_quality_metrics(df: pd.DataFrame) -> DataQualityMetrics:
    """计算数据质量维度：完整性 + 类型合规。"""

    n_rows, n_cols = df.shape
    total_cells = max(n_rows * n_cols, 1)
    missing_cells = int(df.isna().sum().sum())
    missing_rate = missing_cells / total_cells
    completeness = (1 - missing_rate) * 100

    numeric_cells = 0
    finite_cells = 0
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            non_null = df[col].dropna()
            numeric_cells += len(non_null)
            finite_cells += int(pd.to_numeric(non_null, errors="coerce").apply(np.isfinite).sum())
    type_compliance = (finite_cells / numeric_cells * 100) if numeric_cells else 100.0

    return DataQualityMetrics(
        completeness_score=completeness,
        type_compliance_score=type_compliance,
        missing_rate_overall=missing_rate,
        n_columns=int(n_cols),
        n_rows=int(n_rows),
        duplicate_rows=int(df.duplicated().sum()),
    )


def compute_analysis_quality_metrics(
    outstats_output: dict,
    corr_results: list,
    type_stats_info: dict,
) -> AnalysisQualityMetrics:
    """计算分析质量维度：统计方法覆盖度与口径正确性。

    Args:
        outstats_output: 对象级离群值检测结果（含 has_outliers）
        corr_results: 相关性结果列表（元素可访问 robustness_note / is_significant）
        type_stats_info: types.compute_type_aware_stats 的输出（按列含 inferred_type）
    """

    has_outlier_detection = bool(outstats_output) and ("total_count" in outstats_output or "has_outliers" in outstats_output)
    n_corr = len(corr_results or [])

    def _has_robust(item: Any) -> bool:
        note = getattr(item, "robustness_note", None)
        if note is None and isinstance(item, dict):
            note = item.get("robustness_note")
        return bool(note)

    has_robust = any(_has_robust(r) for r in (corr_results or []))

    ordinal_handled = 0
    ratio_marked = 0
    for _col, info in (type_stats_info or {}).items():
        if not isinstance(info, dict):
            continue
        inferred = info.get("inferred_type")
        # 序数列若带 distribution/mode 说明走了正确的序数路径
        if inferred == "ordinal" and ("distribution" in info or "mode" in info):
            ordinal_handled += 1
        # 占比列若带分位数/极值对象说明走了正确的占比路径
        if inferred in {"ratio", "percentage"} and (
            "quantiles" in info or "range_info" in info or "min_row_index" in info
        ):
            ratio_marked += 1

    return AnalysisQualityMetrics(
        has_outlier_detection=has_outlier_detection,
        correlation_tests_completed=n_corr,
        has_robust_correlation=has_robust,
        ordinal_cols_handled_properly=ordinal_handled,
        ratio_cols_explicitly_marked=ratio_marked,
    )


def generate_quality_report(
    df: pd.DataFrame,
    outstats_output: dict | None = None,
    corr_results: list | None = None,
    type_stats_info: dict | None = None,
) -> ComprehensiveQualityReport:
    """生成综合质量报告（数据质量 + 分析质量）。"""

    data_quality = compute_data_quality_metrics(df)
    analysis_quality = compute_analysis_quality_metrics(
        outstats_output or {},
        corr_results or [],
        type_stats_info or {},
    )
    return ComprehensiveQualityReport(
        data_quality=data_quality,
        analysis_quality=analysis_quality,
    )


__all__ = [
    "QualityReport",
    "assess_quality",
    "check_data_quality",
    "infer_column_type",
    "run_quality_checks",
    "DataQualityMetrics",
    "AnalysisQualityMetrics",
    "ComprehensiveQualityReport",
    "compute_data_quality_metrics",
    "compute_analysis_quality_metrics",
    "generate_quality_report",
]
