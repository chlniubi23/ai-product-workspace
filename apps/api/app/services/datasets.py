from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.quality import apply_cleaning as apply_quality_cleaning
from ..analytics.quality import assess_quality, infer_column_type
from ..common import _require_pandas, error, model_dict, pd, serialize
from ..config import settings
from ..infrastructure.jobs import JobExecutionError
from ..models import CleaningOperation, DatasetVersion, now


def _cleaning_operation_rows(version: DatasetVersion) -> list[CleaningOperation]:
    """Return operation history touching a version, ordered oldest first."""

    rows = {item.id: item for item in (*version.cleaning_operations_from, *version.cleaning_operations_to)}
    return sorted(rows.values(), key=lambda item: item.created_at)


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


def _cleaning_operation_parameters(operation: dict[str, Any]) -> dict[str, Any]:
    raw = operation.get("parameters") or {}
    params = dict(raw) if isinstance(raw, dict) else {}
    for key in ("columns", "subset", "column", "value", "target_type"):
        if key in operation and key not in params:
            params[key] = operation[key]
    return params


def _normalise_cleaning_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the public cleaning request into replayable operation payloads."""

    normalised: list[dict[str, Any]] = []
    for operation in operations:
        typ = str(operation.get("operation") or operation.get("type") or "").lower()
        params = _cleaning_operation_parameters(operation)
        if typ in {"dropna", "drop_missing"}:
            typ = "drop_missing"
        elif typ in {"fillna", "fill_missing"}:
            typ = "fill_missing"
        elif typ in {"coerce_numeric", "coerce_type"}:
            typ = "coerce_type" if typ == "coerce_type" else "coerce_numeric"
            if typ == "coerce_numeric":
                params.setdefault("target_type", "numeric")
        # The API accepts the concise top-level form used by the web client.
        # Copy those values into parameters so the persisted row is sufficient
        # to replay the operation without relying on the original request.
        normalised.append({**operation, "operation": typ, "parameters": params})
    return normalised


def _apply_cleaning(df: pd.DataFrame, operations: list[dict[str, Any]]) -> tuple[pd.DataFrame, dict[str, Any]]:
    _require_pandas()
    normalised = _normalise_cleaning_operations(operations)
    affected: list[str] = []
    for operation in normalised:
        params = dict(operation.get("parameters") or {})
        affected.extend([str(item) for item in (operation.get("columns") or params.get("columns") or params.get("subset") or ([operation.get("column")] if operation.get("column") else []))])
    try:
        result = apply_quality_cleaning(df, normalised)
    except (KeyError, TypeError, ValueError) as exc:
        raise error("VALIDATION_ERROR", f"Invalid cleaning operation: {exc}", 400) from exc
    removed = max(0, len(df) - len(result))
    risks: list[str] = []
    if not len(result):
        risks.append("Cleaning operations would remove every row")
    if removed:
        risks.append(f"{removed} rows will be removed from the derived version")
    # ``estimated_dropped_rows`` is the documented field name (BUG-016);
    # ``estimated_deleted_rows`` is kept as a compatibility alias.
    return result, {"estimated_dropped_rows": int(removed), "estimated_deleted_rows": int(removed), "affected_fields": sorted(set(item for item in affected if item)), "sample_before": _json_records(df.head(5)), "sample_after": _json_records(result.head(5)), "risks": risks}


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


def _update_cleaning_operation_rows(
    db: Session,
    payload: dict[str, Any],
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    source_row_count: int | None = None,
    result_row_count: int | None = None,
    result_fingerprint: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    operation_ids = [str(value) for value in (payload.get("_cleaning_operation_ids") or []) if value]
    if not operation_ids:
        return
    rows = db.scalars(select(CleaningOperation).where(CleaningOperation.id.in_(operation_ids))).all()
    by_id = {row.id: row for row in rows}
    for index, operation_id in enumerate(operation_ids):
        row = by_id.get(operation_id)
        if row is None:
            continue
        preview = dict(row.preview_json or {})
        preview.update({"status": status, "operation_index": index})
        if summary is not None:
            preview.update(summary)
        if source_row_count is not None:
            preview["source_row_count"] = source_row_count
        if result_row_count is not None:
            preview["result_row_count"] = result_row_count
        if result_fingerprint:
            preview["result_fingerprint"] = result_fingerprint
        if error_code:
            preview["error_code"] = error_code
        if error_message:
            preview["error_message"] = error_message[:4000]
        preview["updated_at"] = serialize(now())
        row.preview_json = preview
