from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Query

# pandas is imported lazily by _require_pandas() to keep API startup fast; this
# sentinel holds the cached module once that first import succeeds.
pd = None  # type: ignore[assignment]


def _redact_validation_details(value: Any) -> Any:
    sensitive_names = {"apikey", "deepseekapikey", "password", "secret", "accesstoken", "refreshtoken", "authorization"}
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in sensitive_names:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_validation_details(item)
        error_location = tuple(redacted.get("loc") or ())
        if error_location:
            last_field = re.sub(r"[^a-z0-9]", "", str(error_location[-1]).lower())
            if last_field in sensitive_names:
                if "input" in redacted:
                    redacted["input"] = "[REDACTED]"
                if "msg" in redacted:
                    for name in sensitive_names:
                        if name in redacted["msg"].lower():
                            redacted["msg"] = re.sub(name, "[REDACTED]", redacted["msg"], flags=re.IGNORECASE)
        return redacted
    if isinstance(value, list):
        return [_redact_validation_details(item) for item in value]
    return value


def _request_id() -> str:
    return f"req_{uuid4().hex}"


def _require_pandas():
    global pd
    if pd is None:
        try:
            import importlib

            pd = importlib.import_module("pandas")
        except Exception:
            pd = None
    if pd is None:
        raise error("DEPENDENCY_ERROR", "Pandas is unavailable; install API dependencies before using data endpoints", 503)
    return pd


def ok(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": _request_id(), **meta}}


def error(code: str, message: str, status_code: int = 400, details: Any = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        detail["details"] = details
    return HTTPException(status_code=status_code, detail=detail)


# Credential material must never leave the API, regardless of which endpoint
# serializes the model (BUG-013).
SENSITIVE_MODEL_FIELDS = {"password_hash"}


def serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items() if str(k) not in SENSITIVE_MODEL_FIELDS}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "__table__"):
        return {k: serialize(v) for k, v in vars(value).items() if not k.startswith("_") and k not in SENSITIVE_MODEL_FIELDS}
    return value


def model_dict(obj: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if hasattr(obj, "__table__"):
        result = {column.name: serialize(getattr(obj, column.name)) for column in obj.__table__.columns if column.name not in SENSITIVE_MODEL_FIELDS}
    else:
        result = {k: serialize(v) for k, v in vars(obj).items() if not k.startswith("_") and k not in SENSITIVE_MODEL_FIELDS}
    if extra:
        result.update(serialize(extra))
    return result


def page_params(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> tuple[int, int]:
    return page, page_size


def paged(items: list[Any], page: int, page_size: int, total: int | None = None) -> dict[str, Any]:
    total = len(items) if total is None else total
    start = (page - 1) * page_size
    return ok(items[start : start + page_size], page=page, page_size=page_size, total=total)
