"""Deterministic data quality checks and explicitly approved cleaning operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

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

    Date inference is deliberately conservative: a value must parse for at least 80%
    of non-empty cells.  This avoids treating arbitrary numeric or identifier strings
    as dates.
    """

    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    non_empty = series.dropna()
    if not non_empty.empty and pd.api.types.is_string_dtype(series):
        # ``format="mixed"`` is exactly the per-element fallback pandas would
        # take on its own; naming it keeps real-world mixed-format columns
        # (ISO dates next to "2026/1/5") from spamming UserWarnings.
        parsed = pd.to_datetime(non_empty, errors="coerce", utc=True, format="mixed")
        if float(parsed.notna().mean()) >= 0.8:
            return "datetime"
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


def apply_cleaning(
    data: pd.DataFrame | Sequence[Mapping[str, Any]],
    operations: Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    """Apply only explicit, deterministic cleaning operations.

    Supported operations are ``drop_duplicates`` (optionally by ``columns``),
    ``drop_missing`` (``columns``), ``fill_missing`` (``columns`` plus ``value``
    or a ``strategy`` of mean/median/mode/forward_fill/backward_fill),
    ``coerce_type`` (``column`` and ``type``), ``strip_strings``, ``lowercase``,
    ``filter_range`` (``column`` with ``min``/``max``), ``rename_column`` and
    ``drop_column``.  The function rejects unknown operations and never mutates
    the source frame.
    """

    frame = _as_dataframe(data)
    for operation in operations:
        kind = str(operation.get("operation") or operation.get("type") or "").lower()
        params = operation.get("parameters") if isinstance(operation.get("parameters"), Mapping) else {}
        if kind == "drop_duplicates":
            subset = operation.get("columns") or params.get("subset")
            frame = frame.drop_duplicates(subset=list(subset) if subset else None, keep="first")
        elif kind == "drop_missing":
            subset = operation.get("columns") or params.get("columns")
            if not subset:
                raise ValueError("drop_missing requires columns")
            frame = frame.dropna(subset=list(subset))
        elif kind == "fill_missing":
            columns = operation.get("columns") or params.get("columns")
            if not columns:
                raise ValueError("fill_missing requires columns")
            strategy = str(operation.get("strategy") or params.get("strategy") or "").lower()
            if strategy:
                for column in columns:
                    if column not in frame.columns:
                        raise ValueError(f"fill_missing column does not exist: {column}")
                    series = frame[column]
                    if strategy == "mean":
                        fill = pd.to_numeric(series, errors="coerce").mean()
                    elif strategy == "median":
                        fill = pd.to_numeric(series, errors="coerce").median()
                    elif strategy == "mode":
                        modes = series.mode(dropna=True)
                        fill = modes.iloc[0] if not modes.empty else None
                    elif strategy in {"forward_fill", "ffill"}:
                        frame[column] = series.ffill()
                        continue
                    elif strategy in {"backward_fill", "bfill"}:
                        frame[column] = series.bfill()
                        continue
                    else:
                        raise ValueError(f"unsupported fill_missing strategy: {strategy}")
                    if fill is not None and not pd.isna(fill):
                        frame[column] = series.fillna(fill)
            else:
                value = operation.get("value", params.get("value"))
                frame.loc[:, list(columns)] = frame.loc[:, list(columns)].fillna(value)
        elif kind in {"coerce_type", "coerce_numeric"}:
            columns = operation.get("columns") or params.get("columns")
            column = operation.get("column") or params.get("column")
            target_name = operation.get("target_type") or operation.get("type_name") or params.get("target_type") or ("numeric" if kind == "coerce_numeric" else "")
            target = _TYPE_ALIASES.get(str(target_name).lower())
            if columns and not column:
                for selected in columns:
                    frame = apply_cleaning(frame, [{"operation": "coerce_type", "column": selected, "target_type": target_name}])
                continue
            if not column or target not in {"numeric", "datetime", "boolean", "categorical"}:
                raise ValueError("coerce_type requires column and a supported target_type")
            if target == "numeric":
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            elif target == "datetime":
                frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=True)
            elif target == "boolean":
                # True/1 and False/0 are the same dict key in Python, so list each
                # truth value once and bind the mapping as a default argument.
                mapping = {"true": True, "1": True, True: True, "false": False, "0": False, False: False}
                frame[column] = frame[column].map(
                    lambda value, mapping=mapping: mapping.get(value, value if pd.isna(value) else np.nan)
                )
            else:
                frame[column] = frame[column].astype("string")
        elif kind == "strip_strings":
            columns = operation.get("columns") or list(frame.select_dtypes(include=["object", "string"]).columns)
            for column in columns:
                frame[column] = frame[column].map(lambda value: value.strip() if isinstance(value, str) else value)
        elif kind in {"lowercase", "uppercase"}:
            columns = operation.get("columns") or params.get("columns")
            if not columns:
                raise ValueError(f"{kind} requires columns")
            for column in columns:
                if column not in frame.columns:
                    raise ValueError(f"{kind} column does not exist: {column}")
                transform = str.lower if kind == "lowercase" else str.upper
                frame[column] = frame[column].map(
                    lambda value, transform=transform: transform(value) if isinstance(value, str) else value
                )
        elif kind == "filter_range":
            column = operation.get("column") or params.get("column")
            if not column or column not in frame.columns:
                raise ValueError("filter_range requires an existing column")
            minimum = operation.get("min", params.get("min"))
            maximum = operation.get("max", params.get("max"))
            if minimum is None and maximum is None:
                raise ValueError("filter_range requires min and/or max")
            values = pd.to_numeric(frame[column], errors="coerce")
            mask = pd.Series(True, index=frame.index)
            if minimum is not None:
                mask &= values >= float(minimum)
            if maximum is not None:
                mask &= values <= float(maximum)
            frame = frame[mask.fillna(False)]
        elif kind == "rename_column":
            column = operation.get("column") or params.get("column")
            new_name = operation.get("new_name") or params.get("new_name")
            if not column or column not in frame.columns or not new_name:
                raise ValueError("rename_column requires an existing column and new_name")
            if new_name in frame.columns:
                raise ValueError(f"rename_column target already exists: {new_name}")
            frame = frame.rename(columns={column: str(new_name)})
        elif kind == "drop_column":
            columns = operation.get("columns") or params.get("columns") or ([operation.get("column")] if operation.get("column") else None)
            if not columns:
                raise ValueError("drop_column requires column(s)")
            missing = [column for column in columns if column not in frame.columns]
            if missing:
                raise ValueError(f"drop_column columns do not exist: {missing}")
            frame = frame.drop(columns=list(columns))
        else:
            raise ValueError(f"unsupported cleaning operation: {kind or '<missing>'}")
    return frame.reset_index(drop=True)


# Backwards-compatible aliases used by job/application layers.
run_quality_checks = assess_quality
check_data_quality = assess_quality

__all__ = [
    "QualityReport",
    "apply_cleaning",
    "assess_quality",
    "check_data_quality",
    "infer_column_type",
    "run_quality_checks",
]
