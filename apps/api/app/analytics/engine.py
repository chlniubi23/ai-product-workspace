"""Deterministic Pandas analysis engine.

All methods in this module return JSON-safe, evidence-oriented artifacts.  No LLM is
used for calculations.  The API/background-job layer can persist ``artifact.to_dict()``
as ``analysis_artifacts.payload_json`` and retain the configuration/fingerprint for
reproducibility.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd


def _jsonable(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (np.generic,)):
        return _jsonable(value.item())
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _dataframe(data: pd.DataFrame | Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    if isinstance(data, pd.DataFrame):
        return data.copy()
    return pd.DataFrame(list(data))


def _require_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = [str(column) for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"required columns not found: {', '.join(missing)}")


# The public API uses stable semantic names while uploaded files can use any
# vocabulary (including non-ASCII column names).  Keep this normalization in
# the deterministic engine as well as the HTTP layer so direct callers and the
# background job use identical calculations.
_FIELD_MAPPING_ALIASES = {
    "user_id": "user_id",
    "user_id_column": "user_id",
    "user": "user_id",
    "event_time": "event_time",
    "event_time_column": "event_time",
    "time": "event_time",
    "time_column": "event_time",
    "event_name": "event_name",
    "event_name_column": "event_name",
    "event": "event_name",
    "session_id": "session_id",
    "session_id_column": "session_id",
}


def _normalise_field_mapping(field_mapping: Mapping[str, Any] | None) -> dict[str, str]:
    """Return ``canonical_field -> source_column`` mappings.

    Unknown keys are intentionally ignored here.  The API validator reports
    them to clients, while the pure engine remains backwards compatible with
    callers that pass unrelated configuration fields.
    """

    normalized: dict[str, str] = {}
    if not isinstance(field_mapping, Mapping):
        return normalized
    for key, value in field_mapping.items():
        canonical = _FIELD_MAPPING_ALIASES.get(str(key).strip().lower())
        if canonical is None or value is None:
            continue
        source = str(value).strip()
        if source:
            normalized[canonical] = source
    return normalized


def _apply_field_mapping(
    data: pd.DataFrame | Sequence[Mapping[str, Any]],
    field_mapping: Mapping[str, Any] | None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Rename mapped source columns to stable engine columns.

    Existing canonical columns are retained under a private name when a custom
    source is mapped onto the same canonical target.  This avoids pandas
    duplicate-column semantics and makes the selected mapping authoritative.
    """

    frame = _dataframe(data)
    mapping = _normalise_field_mapping(field_mapping)
    if not mapping:
        return frame, mapping
    for target, source in mapping.items():
        if source not in frame.columns or source == target:
            continue
        if target in frame.columns and target != source:
            backup = f"__original_{target}"
            suffix = 1
            while backup in frame.columns:
                suffix += 1
                backup = f"__original_{target}_{suffix}"
            frame = frame.rename(columns={target: backup})
        frame = frame.rename(columns={source: target})
    return frame, mapping


def _resolve_mapped_column(requested: str | None, canonical: str, mapping: Mapping[str, str]) -> str:
    """Resolve a caller's source or canonical name after mapping."""

    source = mapping.get(canonical)
    if not requested or requested == canonical or (source is not None and requested == source):
        return canonical
    return str(requested)


def _fingerprint(dataset_version_id: str | None, config: Mapping[str, Any]) -> str:
    payload = {"dataset_version_id": dataset_version_id, "config": _jsonable(config)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass
class AnalysisArtifact:
    """A stable analysis product matching the document's artifact contract."""

    artifact_type: str
    title: str
    payload: dict[str, Any]
    config_snapshot: dict[str, Any]
    dataset_version_id: str | None = None
    analysis_run_id: str | None = None
    artifact_id: str = field(default_factory=lambda: str(uuid4()))
    fingerprint: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        self.config_snapshot = _jsonable(dict(self.config_snapshot))
        self.payload = _jsonable(dict(self.payload))
        if not self.fingerprint:
            self.fingerprint = _fingerprint(self.dataset_version_id, self.config_snapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "analysis_run_id": self.analysis_run_id,
            "artifact_type": self.artifact_type,
            "title": self.title,
            "dataset_version_id": self.dataset_version_id,
            "config_snapshot": self.config_snapshot,
            "payload_json": self.payload,
            # ``payload`` is retained as a convenience for callers that do not map
            # directly to the SQLAlchemy JSON column.
            "payload": self.payload,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def _normalise_frequency(frequency: str) -> str:
    value = str(frequency or "D").strip().lower()
    return {"day": "D", "daily": "D", "week": "W", "weekly": "W", "month": "M", "monthly": "M"}.get(value, str(frequency or "D").upper())


def _period_change(values: Sequence[float | None]) -> list[float | None]:
    result: list[float | None] = [None]
    # The offset slice is deliberate, so the shorter sequence must win here.
    for previous, current in zip(values, values[1:], strict=False):
        if previous in (None, 0) or current is None:
            result.append(None)
        else:
            result.append(float((current - previous) / previous))
    return result


class AnalysisEngine:
    """Run reproducible EDA, trend, funnel, retention and anomaly calculations."""

    def __init__(self, dataset_version_id: str | None = None, analysis_run_id: str | None = None):
        self.dataset_version_id = dataset_version_id
        self.analysis_run_id = analysis_run_id

    def _artifact(self, artifact_type: str, title: str, payload: Mapping[str, Any], config: Mapping[str, Any]) -> AnalysisArtifact:
        return AnalysisArtifact(
            artifact_type=artifact_type,
            title=title,
            payload=dict(payload),
            config_snapshot=dict(config),
            dataset_version_id=self.dataset_version_id,
            analysis_run_id=self.analysis_run_id,
            fingerprint=_fingerprint(self.dataset_version_id, config),
        )

    def run_eda(self, data: pd.DataFrame | Sequence[Mapping[str, Any]], *, top_n: int = 10) -> AnalysisArtifact:
        if top_n < 1:
            raise ValueError("top_n must be at least 1")
        frame = _dataframe(data)
        columns: list[dict[str, Any]] = []
        for name in frame.columns:
            series = frame[name]
            missing = int(series.isna().sum())
            item: dict[str, Any] = {
                "name": str(name),
                "dtype": str(series.dtype),
                "row_count": int(len(series)),
                "missing_count": missing,
                "missing_rate": (missing / len(series) if len(series) else 0.0),
                "unique_count": int(series.nunique(dropna=True)),
                "sample_values": _jsonable(series.dropna().head(5).tolist()),
            }
            if pd.api.types.is_numeric_dtype(series):
                values = pd.to_numeric(series, errors="coerce").dropna()
                item["statistics"] = {
                    "count": int(values.count()),
                    "mean": float(values.mean()) if not values.empty else None,
                    "median": float(values.median()) if not values.empty else None,
                    "std": float(values.std(ddof=0)) if not values.empty else None,
                    "min": float(values.min()) if not values.empty else None,
                    "max": float(values.max()) if not values.empty else None,
                    "quantiles": {str(q): (float(values.quantile(q)) if not values.empty else None) for q in (0.25, 0.5, 0.75)},
                }
            else:
                top = series.astype("string").fillna("<missing>").value_counts().head(top_n)
                item["top_values"] = [{"value": _jsonable(index), "count": int(count), "rate": float(count / len(series)) if len(series) else 0.0} for index, count in top.items()]
            columns.append(item)

        numeric = frame.select_dtypes(include=[np.number])
        correlations: list[dict[str, Any]] = []
        if numeric.shape[1] >= 2:
            matrix = numeric.corr(method="pearson")
            for left_index, left in enumerate(matrix.columns):
                for right in matrix.columns[left_index + 1 :]:
                    value = matrix.loc[left, right]
                    correlations.append({"left": str(left), "right": str(right), "correlation": float(value) if pd.notna(value) else None})
        payload = {
            "row_count": int(len(frame)),
            "column_count": int(len(frame.columns)),
            "columns": columns,
            "duplicate_rows": int(frame.duplicated(keep="first").sum()),
            "correlations": correlations,
            "preview": _jsonable(frame.head(10).replace({np.nan: None}).to_dict(orient="records")),
        }
        config = {"analysis_type": "eda", "top_n": top_n}
        return self._artifact("table", "EDA summary", payload, config)

    def run_trend_analysis(
        self,
        data: pd.DataFrame | Sequence[Mapping[str, Any]],
        *,
        time_column: str,
        metric_column: str,
        group_column: str | None = None,
        frequency: str = "D",
        aggregation: str = "sum",
        field_mapping: Mapping[str, str] | None = None,
    ) -> AnalysisArtifact:
        frame, normalized_mapping = _apply_field_mapping(data, field_mapping)
        time_column = _resolve_mapped_column(time_column, "event_time", normalized_mapping)
        _require_columns(frame, [time_column, metric_column] + ([group_column] if group_column else []))
        if aggregation not in {"sum", "mean", "count", "min", "max", "median"}:
            raise ValueError("aggregation must be one of sum, mean, count, min, max, median")
        parsed = pd.to_datetime(frame[time_column], errors="coerce", utc=True, format="mixed")
        numeric = pd.to_numeric(frame[metric_column], errors="coerce")
        valid = parsed.notna() & (numeric.notna() if aggregation != "count" else True)
        work = frame.loc[valid].copy()
        work["__time"] = parsed.loc[valid]
        work["__metric"] = numeric.loc[valid]
        freq = _normalise_frequency(frequency)
        group_fields: list[str] = []
        if group_column:
            group_fields.append(group_column)
        grouped = work.groupby(group_fields + [pd.Grouper(key="__time", freq=freq)], dropna=False, sort=True)
        if aggregation == "count":
            values = grouped[metric_column].count()
        else:
            values = getattr(grouped["__metric"], aggregation)()
        records: list[dict[str, Any]] = []
        if group_column:
            for key, value in values.items():
                group_value, period = key
                records.append({"period": period, "group": group_value, "value": value, "count": int(grouped.size().loc[key])})
        else:
            for period, value in values.items():
                records.append({"period": period, "value": value, "count": int(grouped.size().loc[period])})
        if group_column:
            by_group: dict[str, list[dict[str, Any]]] = {}
            for record in records:
                by_group.setdefault(str(record["group"]), []).append(record)
            for group_records in by_group.values():
                changes = _period_change([float(item["value"]) if item["value"] is not None else None for item in group_records])
                for item, change in zip(group_records, changes, strict=True):
                    item["period_over_period"] = change
        else:
            changes = _period_change([float(item["value"]) if item["value"] is not None else None for item in records])
            for item, change in zip(records, changes, strict=True):
                item["period_over_period"] = change
        chart = self._line_chart(records, x_key="period", y_key="value", series_key="group" if group_column else None)
        payload = {
            "time_column": time_column,
            "metric_column": metric_column,
            "group_column": group_column,
            "frequency": freq,
            "aggregation": aggregation,
            "rows": _jsonable(records),
            "chart": chart,
            "period_count": len(records),
        }
        config = {"analysis_type": "trend", "time_column": time_column, "metric_column": metric_column, "group_column": group_column, "frequency": freq, "aggregation": aggregation, "field_mapping": normalized_mapping}
        return self._artifact("chart", "Trend analysis", payload, config)

    def run_funnel_analysis(
        self,
        data: pd.DataFrame | Sequence[Mapping[str, Any]],
        *,
        user_id_column: str,
        event_time_column: str,
        event_name_column: str,
        steps: Sequence[str],
        window_hours: float | None = None,
        field_mapping: Mapping[str, str] | None = None,
    ) -> AnalysisArtifact:
        frame, normalized_mapping = _apply_field_mapping(data, field_mapping)
        user_id_column = _resolve_mapped_column(user_id_column, "user_id", normalized_mapping)
        event_time_column = _resolve_mapped_column(event_time_column, "event_time", normalized_mapping)
        event_name_column = _resolve_mapped_column(event_name_column, "event_name", normalized_mapping)
        if not steps or len(steps) < 2:
            raise ValueError("funnel requires at least two ordered steps")
        _require_columns(frame, [user_id_column, event_time_column, event_name_column])
        parsed = pd.to_datetime(frame[event_time_column], errors="coerce", utc=True, format="mixed")
        work = frame.loc[parsed.notna() & frame[user_id_column].notna()].copy()
        work["__time"] = parsed.loc[work.index]
        work = work.sort_values([user_id_column, "__time"], kind="mergesort")
        reached = [0 for _ in steps]
        users_by_step: list[set[Any]] = [set() for _ in steps]
        for user, user_frame in work.groupby(user_id_column, sort=False):
            events = list(zip(user_frame["__time"].tolist(), user_frame[event_name_column].tolist(), strict=True))
            cursor: pd.Timestamp | None = None
            start_time: pd.Timestamp | None = None
            for index, step in enumerate(steps):
                match: pd.Timestamp | None = None
                for event_time, event_name in events:
                    if event_name != step:
                        continue
                    if cursor is not None and event_time < cursor:
                        continue
                    if start_time is not None and window_hours is not None and (event_time - start_time).total_seconds() > window_hours * 3600:
                        continue
                    match = event_time
                    break
                if match is None:
                    break
                reached[index] += 1
                users_by_step[index].add(user)
                if start_time is None:
                    start_time = match
                cursor = match
        initial = reached[0]
        step_records = []
        for index, step in enumerate(steps):
            count = reached[index]
            previous = reached[index - 1] if index else initial
            step_records.append({
                "step": str(step),
                "order": index + 1,
                "users": count,
                "conversion_rate": (count / initial if initial else None),
                "step_conversion_rate": (count / previous if previous else None),
                "dropoff_users": (previous - count if index else 0),
                "evidence_user_count": len(users_by_step[index]),
            })
        payload = {
            "user_id_column": user_id_column,
            "event_time_column": event_time_column,
            "event_name_column": event_name_column,
            "steps": [str(step) for step in steps],
            "window_hours": window_hours,
            "total_users": int(work[user_id_column].nunique()),
            "step_results": step_records,
            "chart": {"type": "funnel", "data": [{"name": row["step"], "value": row["users"]} for row in step_records]},
        }
        config = {"analysis_type": "funnel", "user_id_column": user_id_column, "event_time_column": event_time_column, "event_name_column": event_name_column, "steps": list(steps), "window_hours": window_hours, "field_mapping": normalized_mapping}
        return self._artifact("chart", "Funnel analysis", payload, config)

    def run_retention_analysis(
        self,
        data: pd.DataFrame | Sequence[Mapping[str, Any]],
        *,
        user_id_column: str,
        event_time_column: str,
        periods: Sequence[int] = (1, 7, 30),
        cohort_granularity: str = "day",
        return_event_filter: Mapping[str, Any] | None = None,
        field_mapping: Mapping[str, str] | None = None,
    ) -> AnalysisArtifact:
        frame, normalized_mapping = _apply_field_mapping(data, field_mapping)
        user_id_column = _resolve_mapped_column(user_id_column, "user_id", normalized_mapping)
        event_time_column = _resolve_mapped_column(event_time_column, "event_time", normalized_mapping)
        if return_event_filter:
            mapped_filter: dict[str, Any] = {}
            for column, expected in return_event_filter.items():
                name = str(column)
                mapped_name = next((target for target, source in normalized_mapping.items() if name in {target, source}), name)
                mapped_filter[mapped_name] = expected
            return_event_filter = mapped_filter
        _require_columns(frame, [user_id_column, event_time_column])
        periods = tuple(sorted({int(period) for period in periods}))
        if not periods or any(period < 0 for period in periods):
            raise ValueError("periods must contain non-negative integers")
        granularity = str(cohort_granularity).lower()
        if granularity not in {"day", "week", "month"}:
            raise ValueError("cohort_granularity must be day, week or month")
        parsed = pd.to_datetime(frame[event_time_column], errors="coerce", utc=True, format="mixed")
        valid = parsed.notna() & frame[user_id_column].notna()
        work = frame.loc[valid].copy()
        work["__time"] = parsed.loc[valid]
        all_activity = work[[user_id_column, "__time"]].copy()
        if return_event_filter:
            for column in return_event_filter:
                if column not in work.columns and column in frame.columns:
                    work[column] = frame.loc[valid, column]
                if column not in work.columns:
                    raise ValueError(f"return event column not found: {column}")
            mask = pd.Series(True, index=work.index)
            for column, expected in return_event_filter.items():
                mask &= work[column].eq(expected)
            return_activity = work.loc[mask, [user_id_column, "__time"]].copy()
        else:
            return_activity = all_activity.copy()
        # Use calendar dates for explicit D1/D7/D30 semantics.  A user's first day
        # is the cohort anchor and repeated events on one day count once.
        all_activity["__date"] = all_activity["__time"].dt.floor("D")
        first = all_activity.groupby(user_id_column, sort=False)["__date"].min().rename("cohort_date")
        return_activity = return_activity.join(first, on=user_id_column)
        return_activity["__date"] = return_activity["__time"].dt.floor("D")
        return_activity["elapsed_days"] = (return_activity["__date"] - return_activity["cohort_date"]).dt.days
        return_activity = return_activity[return_activity["elapsed_days"] >= 0]
        if granularity == "day":
            return_activity["cohort"] = return_activity["cohort_date"].dt.strftime("%Y-%m-%d")
        elif granularity == "week":
            return_activity["cohort"] = (return_activity["cohort_date"] - pd.to_timedelta(return_activity["cohort_date"].dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")
        else:
            return_activity["cohort"] = return_activity["cohort_date"].dt.to_period("M").astype(str)
        cohort_users: dict[str, set[Any]] = {}
        for user, cohort_date in first.items():
            # Cohorts are derived from first activity, independent of a return-event
            # filter.  Only return activity is used for the numerator.
            if granularity == "day":
                cohort = cohort_date.strftime("%Y-%m-%d")
            elif granularity == "week":
                cohort = (cohort_date - pd.Timedelta(days=cohort_date.weekday())).strftime("%Y-%m-%d")
            else:
                cohort = cohort_date.to_period("M").strftime("%Y-%m")
            cohort_users.setdefault(str(cohort), set()).add(user)
        records: list[dict[str, Any]] = []
        for cohort in sorted(cohort_users):
            cohort_frame = return_activity[return_activity["cohort"] == cohort]
            users = cohort_users[cohort]
            size = len(users)
            for period in periods:
                retained = set(cohort_frame.loc[cohort_frame["elapsed_days"] == period, user_id_column].tolist())
                retained_count = len(retained & users)
                records.append({"cohort": cohort, "period": period, "cohort_size": size, "retained_users": retained_count, "retention_rate": (retained_count / size if size else None)})
        payload = {
            "user_id_column": user_id_column,
            "event_time_column": event_time_column,
            "cohort_granularity": granularity,
            "periods": list(periods),
            "cohort_results": records,
            "chart": {"type": "retention", "rows": records},
        }
        config = {"analysis_type": "retention", "user_id_column": user_id_column, "event_time_column": event_time_column, "periods": list(periods), "cohort_granularity": granularity, "return_event_filter": dict(return_event_filter or {}), "field_mapping": normalized_mapping}
        return self._artifact("table", "Retention analysis", payload, config)

    def run_anomaly_detection(
        self,
        data: pd.DataFrame | Sequence[Mapping[str, Any]],
        *,
        metric_column: str,
        time_column: str | None = None,
        method: str = "iqr",
        threshold: float = 3.0,
        window: int = 7,
        group_column: str | None = None,
    ) -> AnalysisArtifact:
        frame = _dataframe(data)
        _require_columns(frame, [metric_column] + ([time_column] if time_column else []) + ([group_column] if group_column else []))
        method = str(method).lower()
        if method not in {"iqr", "zscore", "rolling_zscore", "rolling"}:
            raise ValueError("method must be iqr, zscore or rolling_zscore")
        if threshold <= 0 or window < 2:
            raise ValueError("threshold must be positive and window must be at least 2")
        work = frame.copy()
        work["__value"] = pd.to_numeric(work[metric_column], errors="coerce")
        if time_column:
            work["__time"] = pd.to_datetime(work[time_column], errors="coerce", utc=True, format="mixed")
            work = work.sort_values("__time", kind="mergesort")
        else:
            work["__time"] = pd.NaT
        result_rows: list[dict[str, Any]] = []

        groups = work.groupby(group_column, dropna=False, sort=False) if group_column else [(None, work)]
        for group_value, group_frame in groups:
            values = group_frame["__value"].astype(float)
            if method == "iqr":
                q1, q3 = float(values.quantile(0.25)), float(values.quantile(0.75))
                spread = q3 - q1
                lower, upper = q1 - 1.5 * spread, q3 + 1.5 * spread
                baseline = pd.Series((q1 + q3) / 2, index=group_frame.index)
                std = pd.Series(spread if spread > 0 else 1.0, index=group_frame.index)
                scores = (values - baseline).abs() / std
                mask = (values < lower) | (values > upper)
            elif method == "zscore":
                baseline_value = float(values.mean()) if len(values) else 0.0
                std_value = float(values.std(ddof=0)) if len(values) else 0.0
                baseline = pd.Series(baseline_value, index=group_frame.index)
                std = pd.Series(std_value if std_value > 0 else 1.0, index=group_frame.index)
                scores = (values - baseline).abs() / std
                lower, upper = baseline_value - threshold * (std_value or 0.0), baseline_value + threshold * (std_value or 0.0)
                mask = scores > threshold if std_value > 0 else pd.Series(False, index=group_frame.index)
            else:
                rolling_mean = values.rolling(window=window, min_periods=2).mean()
                rolling_std = values.rolling(window=window, min_periods=2).std(ddof=0)
                baseline = rolling_mean
                std = rolling_std.replace(0, np.nan)
                scores = ((values - baseline).abs() / std).fillna(0.0)
                lower_series = baseline - threshold * rolling_std
                upper_series = baseline + threshold * rolling_std
                mask = scores > threshold
                lower, upper = lower_series, upper_series
            for index in group_frame.index:
                value = values.loc[index]
                if method == "iqr" or method == "zscore":
                    low, high = lower, upper
                else:
                    low = lower.loc[index]
                    high = upper.loc[index]
                result_rows.append({
                    "index": int(index) if isinstance(index, (int, np.integer)) else str(index),
                    "timestamp": group_frame.loc[index, "__time"],
                    # Also expose the value under the caller's own column names
                    # so consumers can read rows with their dataset vocabulary.
                    **({str(time_column): group_frame.loc[index, "__time"]} if time_column and time_column != "timestamp" else {}),
                    "group": group_value,
                    "value": value,
                    "baseline": baseline.loc[index],
                    "lower_bound": low,
                    "upper_bound": high,
                    "score": scores.loc[index],
                    "is_anomaly": bool(mask.loc[index]),
                })
        anomalies = [row for row in result_rows if row["is_anomaly"]]
        payload = {
            "metric_column": metric_column,
            "time_column": time_column,
            "group_column": group_column,
            "method": "rolling_zscore" if method == "rolling" else method,
            "threshold": threshold,
            "window": window if method in {"rolling", "rolling_zscore"} else None,
            "anomaly_count": len(anomalies),
            "rows": _jsonable(result_rows),
            "anomalies": _jsonable(anomalies),
            "chart": {"type": "line_with_anomalies", "rows": _jsonable(result_rows)},
        }
        config = {"analysis_type": "anomaly", "metric_column": metric_column, "time_column": time_column, "group_column": group_column, "method": method, "threshold": threshold, "window": window}
        return self._artifact("anomaly", "Anomaly detection", payload, config)

    def run_group_comparison(
        self,
        data: pd.DataFrame | Sequence[Mapping[str, Any]],
        *,
        group_column: str,
        value_column: str,
        aggregation: str = "mean",
        top_n: int = 10,
        field_mapping: Mapping[str, str] | None = None,
    ) -> AnalysisArtifact:
        """Aggregate a numeric column per low-cardinality category, Pareto-style.

        Each group gets count/mean/sum/min/max plus its share of the total sum
        (the share is always sum-based, whatever ``aggregation`` the caller
        highlights).  Rows are sorted by share descending and truncated to
        ``top_n``.  The row list lives under the aggregate-allowlist key
        ``categories`` so the payload survives the AI-context firewall without
        any allowlist change.
        """

        frame, normalized_mapping = _apply_field_mapping(data, field_mapping)
        _require_columns(frame, [group_column, value_column])
        if aggregation not in {"sum", "mean"}:
            raise ValueError("aggregation must be sum or mean")
        if top_n < 1:
            raise ValueError("top_n must be at least 1")
        work = frame[[group_column, value_column]].copy()
        work[value_column] = pd.to_numeric(work[value_column], errors="coerce")
        work = work.loc[work[group_column].notna() & work[value_column].notna()]
        if work.empty:
            raise ValueError("group_comparison requires at least one row with a group and a numeric value")
        stats = work.groupby(group_column, dropna=False)[value_column].agg(["count", "mean", "sum", "min", "max"])
        total_sum = float(stats["sum"].sum())
        records: list[dict[str, Any]] = []
        for group_value, row in stats.iterrows():
            group_sum = float(row["sum"])
            records.append(
                {
                    "group": str(group_value),
                    "count": int(row["count"]),
                    "mean": round(float(row["mean"]), 6),
                    "sum": round(group_sum, 6),
                    "min": _jsonable(row["min"]),
                    "max": _jsonable(row["max"]),
                    "share": (round(group_sum / total_sum, 6) if total_sum else None),
                }
            )
        records.sort(key=lambda row: (row["share"] if row["share"] is not None else 0.0), reverse=True)
        records = records[:top_n]
        chart = {
            "type": "bar",
            "data": [{"name": row["group"], "value": (round(row["share"] * 100, 2) if row["share"] is not None else 0)} for row in records],
        }
        payload = {
            "group_column": group_column,
            "value_column": value_column,
            "aggregation": aggregation,
            "total_groups": int(stats.shape[0]),
            "categories": records,
            "chart": chart,
        }
        config = {"analysis_type": "group_comparison", "group_column": group_column, "value_column": value_column, "aggregation": aggregation, "top_n": top_n, "field_mapping": normalized_mapping}
        return self._artifact("chart", f"Group comparison by {group_column}", payload, config)

    @staticmethod
    def _line_chart(records: Sequence[Mapping[str, Any]], *, x_key: str, y_key: str, series_key: str | None) -> dict[str, Any]:
        if series_key:
            series_values: dict[str, list[Mapping[str, Any]]] = {}
            for record in records:
                series_values.setdefault(str(record.get(series_key)), []).append(record)
            series = [{"name": name, "type": "line", "data": [[row.get(x_key), row.get(y_key)] for row in values]} for name, values in series_values.items()]
        else:
            series = [{"name": y_key, "type": "line", "data": [[row.get(x_key), row.get(y_key)] for row in records]}]
        return {"type": "line", "xAxis": {"type": "time"}, "yAxis": {"type": "value"}, "series": _jsonable(series)}


# Functional facade for job handlers and the Copilot whitelist. Each helper
# returns the flat analysis payload (documented result shape, BUG-017) with the
# artifact/config metadata merged in for traceability.
def _facade_result(artifact: AnalysisArtifact) -> dict[str, Any]:
    record = artifact.to_dict()
    payload = dict(record.pop("payload_json", {}) or {})
    payload.update(record)
    return payload


def run_eda(data: pd.DataFrame | Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return _facade_result(AnalysisEngine(kwargs.pop("dataset_version_id", None), kwargs.pop("analysis_run_id", None)).run_eda(data, **kwargs))


def run_trend_analysis(data: pd.DataFrame | Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return _facade_result(AnalysisEngine(kwargs.pop("dataset_version_id", None), kwargs.pop("analysis_run_id", None)).run_trend_analysis(data, **kwargs))


def run_funnel_analysis(data: pd.DataFrame | Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return _facade_result(AnalysisEngine(kwargs.pop("dataset_version_id", None), kwargs.pop("analysis_run_id", None)).run_funnel_analysis(data, **kwargs))


def run_retention_analysis(data: pd.DataFrame | Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return _facade_result(AnalysisEngine(kwargs.pop("dataset_version_id", None), kwargs.pop("analysis_run_id", None)).run_retention_analysis(data, **kwargs))


def run_anomaly_detection(data: pd.DataFrame | Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return _facade_result(AnalysisEngine(kwargs.pop("dataset_version_id", None), kwargs.pop("analysis_run_id", None)).run_anomaly_detection(data, **kwargs))


def run_group_comparison(data: pd.DataFrame | Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return _facade_result(AnalysisEngine(kwargs.pop("dataset_version_id", None), kwargs.pop("analysis_run_id", None)).run_group_comparison(data, **kwargs))


__all__ = [
    "AnalysisArtifact",
    "AnalysisEngine",
    "run_anomaly_detection",
    "run_eda",
    "run_funnel_analysis",
    "run_group_comparison",
    "run_retention_analysis",
    "run_trend_analysis",
]
