from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..analytics.quality import assess_quality, infer_column_type
from ..common import _require_pandas, error, model_dict, pd, serialize
from ..config import settings
from ..infrastructure.jobs import JobExecutionError
from ..models import DatasetVersion


def _version_payload(version: DatasetVersion, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize a dataset version without exposing the internal storage path."""

    payload = model_dict(version, extra)
    payload.pop("storage_path", None)
    return payload


def _safe_name(name: str) -> str:
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base)[:255] or "upload.csv"


# ``.xls`` is deliberately absent: reading it needs ``xlrd``, which is neither
# declared nor installed, so accepting it only moves the failure from a clear 400
# into the parse job where the user sees a dead version instead of an error.
UPLOAD_SUFFIXES = frozenset({".csv", ".xlsx"})
UPLOAD_SUFFIX_MESSAGE = "Only CSV and XLSX files are supported"


def _reject_unsupported_upload(filename: str) -> None:
    if Path(filename).suffix.lower() not in UPLOAD_SUFFIXES:
        raise error("VALIDATION_ERROR", UPLOAD_SUFFIX_MESSAGE, 400)


def _read_dataframe(path: str | Path, file_name: str, worksheet_name: str | None = None) -> pd.DataFrame:
    pd = _require_pandas()
    _require_pandas()
    suffix = Path(file_name).suffix.lower()
    if suffix == ".csv":
        # Uploaded CSVs commonly arrive from spreadsheet tools with a BOM or a
        # Chinese locale encoding. Try the documented encodings in a deterministic
        # order and only fail after each decoder has been attempted.
        last_error: Exception | None = None
        for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
            try:
                return pd.read_csv(path, encoding=encoding)
            except UnicodeDecodeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return pd.read_csv(path)
    if suffix == ".xlsx":
        return pd.read_excel(path, sheet_name=worksheet_name or 0)
    raise error("VALIDATION_ERROR", UPLOAD_SUFFIX_MESSAGE, 400)


def _type_name(series: pd.Series) -> str:
    """Map a pandas dtype to the product's documented display vocabulary.

    The data dictionary exposes string/integer/float/boolean/datetime/category
    (BUG-014); internal analytics names such as "numeric" must not leak out.
    """
    pd = _require_pandas()
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    inferred = infer_column_type(series)
    if inferred == "datetime":
        return "datetime"
    if inferred == "numeric":
        return "float"
    if inferred == "boolean":
        return "boolean"
    return "string"


_IDENTIFIER_NAME = re.compile(r"(?:^|[_-])(id|uuid|guid)$", re.IGNORECASE)


def _column_schema(df: pd.DataFrame) -> list[dict[str, Any]]:
    pd = _require_pandas()
    _require_pandas()
    result: list[dict[str, Any]] = []
    role_names = {
        "user_id": "user_id",
        "event_time": "event_time",
        "event_name": "event_name",
        "date": "date",
        "channel": "segment",
        "version": "version",
        "feedback_text": "feedback_text",
        "rating": "rating",
    }
    for ordinal, name in enumerate(df.columns):
        col = str(name)
        series = df[name]
        nullable = bool(series.isna().any())
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            nullable = nullable or bool(series.astype("string").str.strip().eq("").any())
        unique_ratio = float(series.nunique(dropna=True) / max(1, len(series)))
        inferred = _type_name(series)
        # Identifier columns are labels, not measures: an "id"-named column that
        # happens to parse as numbers must still be treated as a string key.
        if inferred in {"integer", "float"} and _IDENTIFIER_NAME.search(col):
            inferred = "string"
        result.append({"name": col, "display_name": col, "inferred_type": inferred, "confirmed_type": None, "nullable": nullable, "unique_ratio": unique_ratio, "mapping_role": role_names.get(col.lower()), "ordinal": ordinal})
    return result


def _quality_summary(df: pd.DataFrame) -> tuple[float, str, dict[str, Any]]:
    _require_pandas()
    report = assess_quality(df).to_dict()
    missing = {
        str(item["name"]): int(item["missing_count"])
        for item in report.get("columns", [])
        if int(item.get("missing_count", 0)) > 0
    }
    type_errors = {
        str(item["column"]): int(item["invalid_count"])
        for item in report.get("type_errors", [])
    }
    anomalies = {
        str(column): int(details.get("count", 0))
        for column, details in (report.get("outliers") or {}).items()
        if int(details.get("count", 0)) > 0
    }
    # Keep the compact keys used by the V1 API while retaining the richer quality
    # report produced by the analytics package for the UI and Copilot.
    report.update({
        "missing_values": missing,
        "duplicate_rows": int(report.get("duplicate_rows", 0)),
        "type_errors": type_errors,
        "anomalies": anomalies,
        "sample": report.get("sample") or _json_records(df.head(5)),
    })
    return float(report.get("overall_score", 0)), str(report.get("status", "needs_review")), report


def _json_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    pd = _require_pandas()
    _require_pandas()
    clean = df.astype(object).where(pd.notna(df), None)
    return [serialize(row) for row in clean.to_dict(orient="records")]


def _safe_data_file(relative_path: str) -> Path | None:
    """Resolve a stored file inside DATA_ROOT, or None if it escapes the root.

    Deletion walks ``storage_path`` values straight from the database, so this
    refuses anything resolving outside the data root instead of unlinking it.
    """

    if not relative_path:
        return None
    root = settings.data_path.resolve()
    candidate = (root / str(relative_path)).resolve()
    if root != candidate and root not in candidate.parents:
        return None
    return candidate


def _job_storage_path(relative_path: str) -> Path:
    """Resolve an internal job path without allowing path traversal."""

    root = settings.data_path.resolve()
    candidate = (root / str(relative_path)).resolve()
    if root != candidate and root not in candidate.parents:
        raise JobExecutionError("INVALID_STORAGE_PATH", "Job storage path is outside the data root", retryable=False)
    return candidate
