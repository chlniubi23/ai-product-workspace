"""Deterministic data quality checks (the cleaning pipeline was removed in batch 11; the cleaning_operations table is kept but unused)."""

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
    """Infer one of the product's stable display types.

    Batch 14 delegates to ``parsing.infer_column_type_v2`` so upload inference,
    the report pipeline and the derived-metric engine share one grammar
    (thousands separators, currency, percent, magnitude suffixes, Chinese
    dates, booleans).  The v2 semantic type maps onto the historical
    vocabulary: numeric/datetime/boolean keep their names, while
    category/text/identifier all surface as ``categorical`` -- the finer
    classification lives in the v2 result, not in this legacy contract.
    """

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

    # Count rows that would be removed by a stable first-occurrence de-duplication.
    # This matches the backend quality endpoint and keeps the metric actionable.
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

__all__ = [
    "QualityReport",
    "assess_quality",
    "check_data_quality",
    "infer_column_type",
    "run_quality_checks",
]
