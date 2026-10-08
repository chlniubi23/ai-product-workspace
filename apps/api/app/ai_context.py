"""Safe context and output contracts for product-analytics AI calls.

The application stores considerably more information than an AI call needs.  This
module is deliberately dependency-light so every AI entry point can use the same
allowlist without importing the database layer.  ``build_ai_context`` is the only
supported shape for outbound analytics context; it never returns raw rows, file
contents, storage paths, credentials, or values from ``.env``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any


class AIContextError(ValueError):
    """Raised when a caller supplies an invalid or unsafe AI context."""


class AIOutputValidationError(ValueError):
    """Raised when a provider response does not satisfy the output contract."""


# A tuple preserves the wire order used in prompts and persisted run summaries;
# the frozenset alias keeps membership checks cheap and backwards compatible.
# ``insights`` is a server-side key: the Copilot route loads the session
# project's confirmed insights itself; callers cannot push one in.
ALLOWED_CONTEXT_KEY_ORDER = ("goal", "metrics", "artifacts", "quality", "schema", "question", "insights")
ALLOWED_CONTEXT_KEYS = frozenset(ALLOWED_CONTEXT_KEY_ORDER)

# These names are intentionally broader than the database column names.  A model
# or a future adapter must not be able to smuggle a file/row payload by choosing a
# slightly different spelling.
FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "raw",
        "raw_data",
        "raw_rows",
        "rows",
        "records",
        "cells",
        "cell_values",
        "values",
        "dataframe",
        "df",
        "file",
        "file_content",
        "content_bytes",
        "storage_path",
        "file_path",
        "path",
        "source_path",
        "env",
        "environment",
        "password",
        "password_hash",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "token",
        "database_url",
        "deepseek_api_key",
    }
)
# Free-form feedback is persisted in a separate domain model and must never be
# copied into an aggregate artifact sent to a provider.  Include common naming
# variants because imported CSV/JSON payloads are not consistent about casing.
_FEEDBACK_CONTENT_KEYS = frozenset(
    {
        "feedback",
        "feedback_item",
        "feedback_items",
        "feedback_text",
        "feedbacktext",
        "feedback_content",
        "feedbackcontent",
        "comment",
        "comments",
        "comment_text",
        "commenttext",
        "message",
        "messages",
        "message_text",
        "messagetext",
        "review",
        "reviews",
        "review_text",
        "reviewtext",
        "content",
        "content_text",
        "contenttext",
        "verbatim",
        "verbatims",
    }
)
_SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|database[_-]?url|storage[_-]?path|file[_-]?path)",
    re.I,
)
_PII_KEY_RE = re.compile(r"(?:email|phone|mobile|telephone|address|user[_-]?id|account[_-]?id|external[_-]?ref)", re.I)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d .()\-]{8,}\d)(?!\d)")
# ISO dates are analysis objects, not PII -- but the phone pattern above eats
# them whole ("2026-03-30" -> "[phone]"), which manufactured fake limitations
# in every report. Dates are shielded before masking and restored afterwards.
_ISO_DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?")


def _mask_keeping_dates(value: str) -> str:
    """Protect-then-mask-then-restore emails/phones over ISO dates."""

    dates: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        dates.append(match.group(0))
        # Private-use-area placeholders: unique per date and unmatchable by
        # the phone/email patterns.
        return chr(0xE000 + len(dates) - 1)

    text = _ISO_DATE_RE.sub(_protect, value)
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    for index, original in enumerate(dates):
        text = text.replace(chr(0xE000 + index), original)
    return text

_ALIASES = {
    "goal_statement": "goal",
    "metric_definitions": "metrics",
    "analysis_artifacts": "artifacts",
    "quality_summary": "quality",
    "quality_report": "quality",
    "dataset_schema": "schema",
    "columns": "schema",
    "user_question": "question",
}

# Lists under these keys are commonly row-level data rather than aggregate
# results.  They are removed even when nested in an otherwise valid artifact.
_ROW_LIST_KEYS = frozenset(
    {
        "raw",
        "raw_data",
        "raw_rows",
        "rows",
        "records",
        "cells",
        "cell_values",
        "data",
        "samples",
        "sample_rows",
        "observations",
        "user_events",
    }
)
_AGGREGATE_LIST_KEYS = frozenset(
    {
        "categories",
        "labels",
        "periods",
        "series",
        "breakdown",
        "counts",
        "rates",
        "means",
        "medians",
        "quantiles",
        "bins",
        "evidence",
        "ids",
        "metrics",
        # Variable-pair aggregates (e.g. correlation pairs: var1/var2 + scalars).
        # Each entry is a pair of variable names plus scalar statistics -- never
        # row-level data -- so the key is allow-listed as an aggregate carrier.
        "pairs",
        "stages",
        "cohorts",
    }
)


def _get(value: Any, key: str, default: Any = None) -> Any:
    """Read a field from mappings, Pydantic models, dataclasses, or ORM rows."""

    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    try:
        return getattr(value, key)
    except AttributeError:
        pass
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            return dumped.get(key, default) if isinstance(dumped, Mapping) else default
        except Exception:
            return default
    if is_dataclass(value):
        try:
            return asdict(value).get(key, default)
        except Exception:
            return default
    return default


def _normal_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(key).strip().lower().replace("-", "_"))


def _is_forbidden_key(key: Any) -> bool:
    normalized = _normal_key(key)
    return (
        normalized in FORBIDDEN_CONTEXT_KEYS
        or normalized in _FEEDBACK_CONTENT_KEYS
        or bool(_SECRET_KEY_RE.search(normalized))
    )


def _anon(value: Any) -> str:
    return "anon_" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _safe_scalar(value: Any, *, key: str | None = None, max_length: int = 2000) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception:
            return str(value)[:max_length]
    if isinstance(value, str):
        if key and _SECRET_KEY_RE.search(key):
            return "[REDACTED]"
        text = _mask_keeping_dates(value)
        if key and _PII_KEY_RE.search(key) and key not in {"summary", "description", "question"}:
            return _anon(value)
        return text[:max_length]
    # Numpy scalar values and UUIDs are safe after conversion to text, but never
    # pass an arbitrary object (which could serialize a dataframe or file handle).
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[REDACTED]"
    return str(value)[:max_length]


def _dataframe_shape(value: Any) -> dict[str, Any] | None:
    """Return metadata for dataframe-like objects without touching cell values."""

    if value is None or not hasattr(value, "columns") or not hasattr(value, "shape"):
        return None
    try:
        columns = [str(item)[:120] for item in list(value.columns)[:100]]
        shape = tuple(value.shape)
        return {"row_count": int(shape[0]), "column_count": int(shape[1]), "columns": columns}
    except Exception:
        return {"row_count": None, "column_count": None, "columns": []}


def _sanitize_aggregate(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Copy aggregate JSON while dropping row-like structures and secrets."""

    if depth > 8:
        return None
    if key and _is_forbidden_key(key):
        return None
    shape = _dataframe_shape(value)
    if shape is not None:
        return shape
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in list(value.items())[:200]:
            child_key = str(raw_key)
            normalized = _normal_key(child_key)
            if _is_forbidden_key(normalized) or normalized in _ROW_LIST_KEYS:
                continue
            cleaned = _sanitize_aggregate(child, key=normalized, depth=depth + 1)
            if cleaned is not None:
                result[child_key[:120]] = cleaned
        return result
    if isinstance(value, (list, tuple, set)):
        normalized_key = _normal_key(key or "")
        if normalized_key in _ROW_LIST_KEYS:
            return None
        # Scalar arrays are retained only where an aggregate artifact convention
        # makes their semantics clear.  An arbitrary list could be raw rows or
        # cell values and is therefore dropped.
        if normalized_key not in _AGGREGATE_LIST_KEYS:
            if value and all(isinstance(item, Mapping) for item in list(value)[:10]):
                return None
            return None
        cleaned_items = []
        for item in list(value)[:100]:
            if isinstance(item, Mapping):
                cleaned = _sanitize_aggregate(item, depth=depth + 1)
            else:
                cleaned = _safe_scalar(item, key=normalized_key, max_length=400)
            if cleaned is not None:
                cleaned_items.append(cleaned)
        return cleaned_items
    return _safe_scalar(value, key=key)


def _extract_goal(project: Any, goal: Any) -> str:
    value = goal
    if value is None:
        value = _get(project, "goal_statement")
    if value is None:
        value = _get(project, "goal")
    return str(_safe_scalar(value or "", key="goal", max_length=4000) or "")


def _extract_metrics(metrics: Any) -> list[dict[str, Any]]:
    if metrics is None:
        return []
    if isinstance(metrics, (Mapping, str, bytes)):
        metrics = [metrics]
    output: list[dict[str, Any]] = []
    for item in list(metrics)[:100]:
        name = _get(item, "name") or _get(item, "metric_name")
        definition = _get(item, "definition") or _get(item, "description") or ""
        unit = _get(item, "unit") or ""
        if name is None and not definition:
            continue
        output.append(
            {
                "name": str(_safe_scalar(name or "", key="name", max_length=255) or ""),
                "definition": str(_safe_scalar(definition, key="definition", max_length=2000) or ""),
                "unit": str(_safe_scalar(unit, key="unit", max_length=80) or ""),
            }
        )
    return output


def _extract_artifacts(artifacts: Any) -> list[dict[str, Any]]:
    if artifacts is None:
        return []
    if isinstance(artifacts, (Mapping, str, bytes)):
        artifacts = [artifacts]
    output: list[dict[str, Any]] = []
    for item in list(artifacts)[:100]:
        payload = _get(item, "payload_json")
        if payload is None:
            payload = _get(item, "payload")
        # A plain aggregate mapping is accepted for convenient service callers.
        if payload is None and isinstance(item, Mapping):
            payload = item
        cleaned_payload = _sanitize_aggregate(payload, key="payload") if payload is not None else {}
        record: dict[str, Any] = {}
        for field in ("id", "artifact_type", "title", "fingerprint", "analysis_run_id"):
            value = _get(item, field)
            if value is not None:
                record[field] = str(_safe_scalar(value, key=field, max_length=255))
        record["payload"] = cleaned_payload if isinstance(cleaned_payload, Mapping) else {}
        output.append(record)
    return output


def _extract_quality(quality: Any, quality_summary: Any = None) -> dict[str, Any]:
    source = quality_summary if quality_summary is not None else quality
    if source is None:
        return {}
    summary = _get(source, "summary_json")
    if summary is None:
        summary = _get(source, "summary")
    result: dict[str, Any] = {}
    for field in ("overall_score", "status"):
        value = _get(source, field)
        if value is not None:
            result[field] = _safe_scalar(value, key=field)
    cleaned = _sanitize_aggregate(summary, key="summary") if summary is not None else None
    if isinstance(cleaned, Mapping):
        result["summary"] = cleaned
    elif isinstance(source, Mapping):
        cleaned_source = _sanitize_aggregate(source, key="quality")
        if isinstance(cleaned_source, Mapping):
            result["summary"] = cleaned_source
    return result


def _extract_schema(schema: Any) -> list[dict[str, str]]:
    if schema is None:
        return []
    if isinstance(schema, (Mapping, str, bytes)):
        schema = [schema]
    result: list[dict[str, str]] = []
    for item in list(schema)[:200]:
        name = _get(item, "column_name") or _get(item, "name") or _get(item, "display_name")
        inferred = _get(item, "inferred_type") or _get(item, "confirmed_type") or _get(item, "type")
        if name is None:
            continue
        result.append(
            {
                "column_name": str(_safe_scalar(name, key="column_name", max_length=255) or ""),
                "inferred_type": str(_safe_scalar(inferred or "unknown", key="inferred_type", max_length=80) or "unknown"),
            }
        )
    return result


_INSIGHT_EVIDENCE_ITEM_LIMIT = 20


def _extract_insights(insights: Any) -> list[dict[str, Any]]:
    """Project confirmed insights onto the fields a provider may see.

    Only ``id/title/content/confidence/evidence`` survive; every value passes
    the same scalar sanitizer as the rest of the context.  ``content`` is the
    adopted insight body a human already confirmed, which is why it is allowed
    here while the same word stays denied for raw feedback payloads.  Unknown
    keys never survive and the list is capped, so a caller cannot widen the
    projection by adding fields to the rows it passes in.
    """

    if insights is None:
        return []
    if isinstance(insights, (Mapping, str, bytes)):
        insights = [insights]
    output: list[dict[str, Any]] = []
    for item in list(insights)[:20]:
        title = _get(item, "title")
        content = _get(item, "content")
        if title is None and content is None:
            continue
        entry: dict[str, Any] = {
            "id": str(_safe_scalar(_get(item, "id") or "", key="id", max_length=255) or ""),
            "title": str(_safe_scalar(title or "", key="title", max_length=255) or ""),
            "content": str(_safe_scalar(content or "", key="content", max_length=2000) or ""),
        }
        confidence = _get(item, "confidence")
        if confidence is not None:
            entry["confidence"] = str(_safe_scalar(confidence, key="confidence", max_length=20))
        evidence = _get(item, "evidence")
        if isinstance(evidence, (list, tuple)):
            cleaned_evidence: list[Any] = []
            for evidence_item in list(evidence)[:_INSIGHT_EVIDENCE_ITEM_LIMIT]:
                if isinstance(evidence_item, Mapping):
                    cleaned_item = {
                        str(evidence_key)[:80]: _safe_scalar(evidence_value, key=str(evidence_key), max_length=255)
                        for evidence_key, evidence_value in list(evidence_item.items())[:10]
                        if not _is_forbidden_key(evidence_key)
                    }
                    if cleaned_item:
                        cleaned_evidence.append(cleaned_item)
                else:
                    cleaned_evidence.append(_safe_scalar(evidence_item, max_length=255))
            if cleaned_evidence:
                entry["evidence"] = cleaned_evidence
        output.append(entry)
    return output


def build_ai_context(
    source: Any | None = None,
    *,
    project: Any | None = None,
    goal: Any | None = None,
    metrics: Any | None = None,
    metric_definitions: Any | None = None,
    artifacts: Any | None = None,
    analysis_artifacts: Any | None = None,
    quality: Any | None = None,
    quality_summary: Any | None = None,
    schema: Any | None = None,
    dataset_schema: Any | None = None,
    columns: Any | None = None,
    dataframe: Any | None = None,
    question: Any | None = None,
    user_question: Any | None = None,
    insights: Any | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Build the sole allowlisted payload accepted by analytics AI calls.

    ``source`` may be a mapping containing aliases such as ``goal_statement`` or
    ``raw_rows``.  Unknown and forbidden fields are intentionally ignored, rather
    than echoed back.  This makes the helper safe at trust boundaries while still
    allowing callers to pass an ORM object or an existing page-context mapping.
    """

    source_map: Mapping[str, Any] = source if isinstance(source, Mapping) else {}
    if project is None:
        project = source_map.get("project")
    if goal is None:
        goal = source_map.get("goal", source_map.get("goal_statement"))
    if metrics is None:
        metrics = source_map.get("metrics", source_map.get("metric_definitions"))
    if artifacts is None:
        artifacts = source_map.get("artifacts", source_map.get("analysis_artifacts"))
    if quality is None and quality_summary is None:
        quality = source_map.get("quality", source_map.get("quality_summary", source_map.get("quality_report")))
    if schema is None:
        schema = source_map.get("schema", source_map.get("dataset_schema", source_map.get("columns")))
    if schema is None and dataframe is not None:
        # A dataframe may be supplied by legacy callers, but only its shape and
        # column names/types can cross the boundary.  No cell is ever inspected.
        shape = _dataframe_shape(dataframe)
        if shape:
            schema = [{"column_name": name, "inferred_type": "unknown"} for name in shape.get("columns", [])]
    if question is None:
        question = source_map.get("question", source_map.get("user_question"))
    if insights is None:
        insights = source_map.get("insights")

    # Explicit keyword aliases win over values in ``source``.
    metrics = metrics if metrics is not None else metric_definitions
    artifacts = artifacts if artifacts is not None else analysis_artifacts
    schema = schema if schema is not None else (dataset_schema if dataset_schema is not None else columns)
    question = question if question is not None else user_question

    context = {
        "goal": _extract_goal(project, goal),
        "metrics": _extract_metrics(metrics),
        "artifacts": _extract_artifacts(artifacts),
        "quality": _extract_quality(quality, quality_summary),
        "schema": _extract_schema(schema),
        "question": str(_safe_scalar(question or "", key="question", max_length=4000) or ""),
        "insights": _extract_insights(insights),
    }
    return context


def _contains_forbidden(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _is_forbidden_key(key):
                return str(key)
            found = _contains_forbidden(child)
            if found:
                return found
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found = _contains_forbidden(child)
            if found:
                return found
    return None


def assert_safe_ai_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a previously built context and return a shallow plain copy."""

    if not isinstance(context, Mapping):
        raise AIContextError("AI context must be an object")
    unknown = set(context) - ALLOWED_CONTEXT_KEYS
    if unknown:
        raise AIContextError(f"AI context contains non-allowlisted fields: {sorted(unknown)}")
    # ``insights`` is the server-side channel for human-adjudicated rows: its
    # entries carry a sanctioned ``content`` key (see ``_extract_insights``),
    # so a raw forbidden-key scan would reject the very shape ``build_ai_context``
    # produces.  Re-project instead -- the projection is that channel's
    # sanitizer (fixed key set, scalar caps, evidence item keys still checked
    # against the forbidden lists) -- keeping construct and re-validate
    # symmetric; smuggled keys are dropped by the projection, never passed.
    forbidden = _contains_forbidden({key: value for key, value in context.items() if key != "insights"})
    if forbidden:
        raise AIContextError(f"AI context contains forbidden field: {forbidden}")
    result = {key: context.get(key) for key in ALLOWED_CONTEXT_KEY_ORDER}
    if result["insights"] is not None:
        result["insights"] = _extract_insights(result["insights"])
    return result


# JSON schema used in provider prompts and tests.  ``evidence`` is required on
# every claim; an empty list is allowed in a response so the UI can flag it, while
# persistence paths can request ``require_nonempty_evidence=True``.
AI_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["facts", "hypotheses", "recommendations", "limitations"],
    "properties": {
        "summary": {"type": "string"},
        "facts": {"type": "array", "items": {"$ref": "#/$defs/claim"}},
        "hypotheses": {"type": "array", "items": {"$ref": "#/$defs/claim"}},
        "recommendations": {"type": "array", "items": {"$ref": "#/$defs/claim"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "$defs": {
        "claim": {
            "type": "object",
            "additionalProperties": True,
            "required": ["text", "evidence"],
            "properties": {
                "text": {"type": "string"},
                "evidence": {"type": "array"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
        }
    },
}


def _normalize_evidence(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise AIOutputValidationError("claim.evidence must be an array")
    result: list[Any] = []
    for item in value[:100]:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append(text[:255])
        elif isinstance(item, Mapping):
            ref_id = item.get("id") or item.get("artifact_id") or item.get("ref")
            if not ref_id:
                raise AIOutputValidationError("evidence objects require an id")
            ref: dict[str, Any] = {"id": str(ref_id)[:255]}
            if item.get("type") is not None:
                ref["type"] = str(item.get("type"))[:80]
            result.append(ref)
        else:
            raise AIOutputValidationError("evidence entries must be strings or objects")
    return result


def validate_ai_output(value: Any, *, require_nonempty_evidence: bool = False) -> dict[str, Any]:
    """Validate and normalize the fixed facts/hypotheses/recommendations shape."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("AI output must be a JSON object")
    missing = [key for key in ("facts", "hypotheses", "recommendations", "limitations") if key not in value]
    if missing:
        raise AIOutputValidationError(f"AI output is missing required sections: {', '.join(missing)}")
    result: dict[str, Any] = {}
    if value.get("summary") is not None:
        result["summary"] = str(value.get("summary"))[:6000]
    for section in ("facts", "hypotheses", "recommendations"):
        entries = value.get(section)
        if not isinstance(entries, list):
            raise AIOutputValidationError(f"AI output section '{section}' must be an array")
        normalized_entries: list[dict[str, Any]] = []
        for entry in entries[:100]:
            if not isinstance(entry, Mapping):
                raise AIOutputValidationError(f"{section} entries must be objects")
            text = entry.get("text")
            if text is None or not str(text).strip():
                raise AIOutputValidationError(f"{section} entries require non-empty text")
            if "evidence" not in entry:
                raise AIOutputValidationError(f"{section} entries require an evidence array")
            evidence = _normalize_evidence(entry.get("evidence"))
            if require_nonempty_evidence and not evidence:
                raise AIOutputValidationError(f"{section} entries require at least one evidence reference")
            normalized: dict[str, Any] = {"text": str(text)[:4000], "evidence": evidence}
            if section == "hypotheses":
                confidence = str(entry.get("confidence") or "medium").lower()
                if confidence not in {"high", "medium", "low"}:
                    raise AIOutputValidationError("hypothesis confidence must be high, medium, or low")
                normalized["confidence"] = confidence
            elif entry.get("confidence") is not None:
                normalized["confidence"] = str(entry.get("confidence"))[:20]
            if "requires_approval" in entry:
                normalized["requires_approval"] = bool(entry.get("requires_approval"))
            normalized_entries.append(normalized)
        result[section] = normalized_entries
    limitations = value.get("limitations")
    if not isinstance(limitations, list):
        raise AIOutputValidationError("AI output limitations must be an array")
    result["limitations"] = [str(item)[:1000] for item in limitations[:100] if str(item).strip()]
    forbidden = _contains_forbidden(result)
    if forbidden:
        raise AIOutputValidationError(f"AI output contains forbidden field: {forbidden}")
    return result


def empty_ai_output(*, summary: str = "", limitation: str | None = None) -> dict[str, Any]:
    """Return a valid no-claims response for unavailable providers or clarification."""

    return {
        "summary": summary,
        "facts": [],
        "hypotheses": [],
        "recommendations": [],
        "limitations": [limitation] if limitation else [],
    }


# Structured contract for the multi-section analysis report.  Every number the
# model writes must come from the aggregate context; sections are free-form
# markdown so the UI can render prose, lists and inline emphasis.
REPORT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "sections", "key_findings", "recommendations", "limitations"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "sections": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading", "content"],
                "properties": {
                    "heading": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
        "key_findings": {"type": "array", "maxItems": 15, "items": {"type": "string"}},
        "recommendations": {"type": "array", "maxItems": 15, "items": {"type": "string"}},
        "limitations": {"type": "array", "maxItems": 15, "items": {"type": "string"}},
    },
}


def validate_report_output(value: Any) -> dict[str, Any]:
    """Validate and normalize the structured report contract."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("report output must be a JSON object")
    missing = [key for key in ("title", "summary", "sections", "key_findings", "recommendations", "limitations") if key not in value]
    if missing:
        raise AIOutputValidationError(f"report output is missing required sections: {', '.join(missing)}")

    def strings(key: str) -> list[str]:
        entries = value.get(key)
        if not isinstance(entries, list):
            raise AIOutputValidationError(f"report output '{key}' must be an array")
        return [str(item)[:1000] for item in entries[:15] if str(item).strip()]

    sections: list[dict[str, str]] = []
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list):
        raise AIOutputValidationError("report output 'sections' must be an array")
    for item in raw_sections[:12]:
        if not isinstance(item, Mapping):
            raise AIOutputValidationError("report sections must be objects")
        heading = str(item.get("heading") or "").strip()
        content = str(item.get("content") or "").strip()
        if not heading or not content:
            raise AIOutputValidationError("report sections require non-empty heading and content")
        sections.append({"heading": heading[:200], "content": content[:20000]})
    return {
        "title": str(value.get("title") or "").strip()[:255] or "数据分析报告",
        "summary": str(value.get("summary") or "")[:6000],
        "sections": sections,
        "key_findings": strings("key_findings"),
        "recommendations": strings("recommendations"),
        "limitations": strings("limitations"),
    }


def empty_report_output(*, summary: str = "", limitation: str | None = None) -> dict[str, Any]:
    """Return a valid empty report for unavailable providers."""

    return {
        "title": "",
        "summary": summary,
        "sections": [],
        "key_findings": [],
        "recommendations": [],
        "limitations": [limitation] if limitation else [],
    }


# Batch 17: two-pass deep document generation.  Pass 1 produces the outline
# (key findings + per-section plan + root cause); pass 2 writes one section
# per call.  Both ride the existing _run_ai_stage budget/valve machinery --
# only the output contracts are new here, the context allowlist is untouched.
DOCUMENT_OUTLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "sections", "root_cause"],
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "severity"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "evidence_hint": {"type": "string"},
                    "severity": {"type": "string", "enum": ["高", "中", "低"]},
                },
            },
        },
        "sections": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading", "purpose"],
                "properties": {
                    "heading": {"type": "string"},
                    "purpose": {"type": "string"},
                    # 批 35：本章依赖的 finding 编号（2-4 个），驱动分节上下文裁剪。
                    # 可选字段：旧模型输出缺失时校验器容忍，分节侧回退全量上下文。
                    "key_refs": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4},
                },
            },
        },
        "root_cause": {"type": "string"},
    },
}

_DOCUMENT_SEVERITIES = {"高", "中", "低"}


def validate_document_outline(value: Any) -> dict[str, Any]:
    """Validate and normalize the outline contract (batch 17)."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("document outline must be a JSON object")
    raw_findings = value.get("findings")
    raw_sections = value.get("sections")
    if not isinstance(raw_findings, list) or not isinstance(raw_sections, list):
        raise AIOutputValidationError("document outline requires findings and sections arrays")
    findings: list[dict[str, Any]] = []
    for item in raw_findings[:6]:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        severity = str(item.get("severity") or "中").strip()
        findings.append(
            {
                "id": str(item.get("id") or "")[:120],
                "title": title[:500],
                "evidence_hint": str(item.get("evidence_hint") or "")[:300],
                "severity": severity if severity in _DOCUMENT_SEVERITIES else "中",
            }
        )
    sections: list[dict[str, Any]] = []
    for item in raw_sections[:10]:
        if not isinstance(item, Mapping):
            continue
        heading = str(item.get("heading") or "").strip()
        if not heading:
            continue
        section: dict[str, Any] = {"heading": heading[:200], "purpose": str(item.get("purpose") or "").strip()[:500]}
        # 批 35：key_refs 清洗而非拒绝 —— 非法元素静默剔除，剩不足 2 个时视为
        # 缺失（分节侧回退全量上下文）；坏引用绝不让整份大纲校验失败。
        raw_refs = item.get("key_refs")
        if isinstance(raw_refs, list):
            refs: list[str] = []
            for ref in raw_refs:
                if isinstance(ref, str) and ref.strip():
                    refs.append(ref.strip()[:120])
                if len(refs) == 4:
                    break
            if len(refs) >= 2:
                section["key_refs"] = refs
        sections.append(section)
    if not sections:
        raise AIOutputValidationError("document outline requires at least one section")
    return {
        "findings": findings,
        "sections": sections,
        "root_cause": str(value.get("root_cause") or "").strip()[:2000],
    }


DOCUMENT_SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["heading", "content"],
    "properties": {"heading": {"type": "string"}, "content": {"type": "string"}},
}


def validate_document_section(value: Any) -> dict[str, Any]:
    """Validate one written section; near-empty bodies are rejected so the
    caller can fall back to the outline-bullet content (batch 17)."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("document section must be a JSON object")
    heading = str(value.get("heading") or "").strip()
    content = str(value.get("content") or "").strip()
    if not heading:
        raise AIOutputValidationError("document section requires a heading")
    if len(content) < 50:
        raise AIOutputValidationError("document section content is too short to be a real section")
    return {"heading": heading[:200], "content": content[:60000]}


# Batch 25: the coherence harmonize pass returns the SAME sections it was
# given (de-duplicated and smoothed).  The heading set is verified by the
# caller against its input -- the validator only enforces per-section shape.
DOCUMENT_SECTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sections"],
    "properties": {
        "sections": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading", "content"],
                "properties": {"heading": {"type": "string"}, "content": {"type": "string"}},
            },
        }
    },
}


def validate_document_sections(value: Any) -> dict[str, Any]:
    """Validate the harmonized sections payload (batch 25).

    Every section must carry a non-empty heading and body; the caller layers
    the strict input-heading-match check on top so a model that renames,
    reorders or drops chapters can never silently replace the draft.
    """

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("document sections output must be a JSON object")
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list):
        raise AIOutputValidationError("document sections output requires a sections array")
    sections: list[dict[str, str]] = []
    for item in raw_sections[:12]:
        if not isinstance(item, Mapping):
            raise AIOutputValidationError("document sections entries must be objects")
        heading = str(item.get("heading") or "").strip()
        content = str(item.get("content") or "").strip()
        if not heading or not content:
            raise AIOutputValidationError("document sections entries require non-empty heading and content")
        sections.append({"heading": heading[:120], "content": content[:20000]})
    if not sections:
        raise AIOutputValidationError("document sections output cannot be empty")
    return {"sections": sections}


# Stage 9 contract: a single problem statement draft.  ``priority`` is optional
# because a model that cannot judge urgency should omit it rather than guess;
# the effort/priority enums match the persistence models exactly so a draft can
# be posted back to the REST layer without re-mapping.
PROBLEM_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "statement", "impact_scope", "limitations"],
    "properties": {
        "title": {"type": "string"},
        "statement": {"type": "string"},
        "impact_scope": {"type": "string"},
        "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
        "limitations": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        # Batch 19: the draft must cite which insight ids it actually leans on;
        # the route intersects this with the caller's validated insight ids, so
        # a hallucinated id can never survive into source_insight_ids.
        "used_insight_ids": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
    },
}


# Stage 10 contract: candidate solution options.  ``effort`` deliberately has no
# XL -- SolutionCreate (apps/api/app/schemas.py) only accepts S/M/L, so a draft
# outside that set could never be saved.
SOLUTION_DRAFTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["options", "limitations"],
    "properties": {
        "options": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "approach", "pros", "cons", "effort"],
                "properties": {
                    "title": {"type": "string"},
                    "approach": {"type": "string"},
                    "pros": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
                    "cons": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
                    "effort": {"type": "string", "enum": ["S", "M", "L"]},
                    # Batch 19: exactly one option is the model's top pick (the
                    # validator enforces this deterministically).
                    "recommended": {"type": "boolean"},
                    "recommendation_reason": {"type": "string"},
                },
            },
        },
        "limitations": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
    },
}


def _clamped_strings(value: Any, *, limit: int, max_length: int, label: str) -> list[str]:
    if not isinstance(value, list):
        raise AIOutputValidationError(f"'{label}' must be an array of strings")
    return [str(item)[:max_length] for item in value[:limit] if str(item).strip()]


def validate_problem_draft(value: Any) -> dict[str, Any]:
    """Validate and normalize the stage-9 problem draft contract."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("problem draft must be a JSON object")
    missing = [key for key in ("title", "statement", "impact_scope", "limitations") if key not in value]
    if missing:
        raise AIOutputValidationError(f"problem draft is missing required fields: {', '.join(missing)}")
    title = str(value.get("title") or "").strip()
    statement = str(value.get("statement") or "").strip()
    if not title or not statement:
        raise AIOutputValidationError("problem draft requires non-empty title and statement")
    result: dict[str, Any] = {
        "title": title[:255],
        "statement": statement[:4000],
        "impact_scope": str(value.get("impact_scope") or "")[:2000],
        "limitations": _clamped_strings(value.get("limitations"), limit=10, max_length=1000, label="limitations"),
    }
    priority = value.get("priority")
    if priority is not None and str(priority).strip():
        priority_text = str(priority).strip().upper()
        if priority_text not in {"P0", "P1", "P2", "P3"}:
            raise AIOutputValidationError("problem draft priority must be one of P0, P1, P2, P3")
        result["priority"] = priority_text
    # Batch 19: cite ids as strings, deduped preserving first-seen order.
    used: list[str] = []
    for item in value.get("used_insight_ids") or []:
        if isinstance(item, str) and item.strip():
            candidate = item.strip()
            if candidate not in used:
                used.append(candidate)
        if len(used) >= 20:
            break
    result["used_insight_ids"] = used
    return result


def validate_solution_drafts(value: Any) -> dict[str, Any]:
    """Validate and normalize the stage-10 solution draft contract."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("solution drafts must be a JSON object")
    missing = [key for key in ("options", "limitations") if key not in value]
    if missing:
        raise AIOutputValidationError(f"solution drafts are missing required fields: {', '.join(missing)}")
    raw_options = value.get("options")
    if not isinstance(raw_options, list):
        raise AIOutputValidationError("solution drafts 'options' must be an array")
    options: list[dict[str, Any]] = []
    for item in raw_options[:5]:
        if not isinstance(item, Mapping):
            raise AIOutputValidationError("solution options must be objects")
        option_missing = [key for key in ("title", "approach", "pros", "cons", "effort") if key not in item]
        if option_missing:
            raise AIOutputValidationError(f"solution option is missing required fields: {', '.join(option_missing)}")
        title = str(item.get("title") or "").strip()
        approach = str(item.get("approach") or "").strip()
        if not title or not approach:
            raise AIOutputValidationError("solution options require non-empty title and approach")
        effort = str(item.get("effort") or "").strip().upper()
        if effort not in {"S", "M", "L"}:
            raise AIOutputValidationError("solution effort must be S, M, or L")
        options.append(
            {
                "title": title[:255],
                "approach": approach[:4000],
                "pros": _clamped_strings(item.get("pros"), limit=8, max_length=500, label="pros"),
                "cons": _clamped_strings(item.get("cons"), limit=8, max_length=500, label="cons"),
                "effort": effort,
                # Batch 19: recommendation flags are normalized AFTER the loop --
                # exactly one winner, deterministically.
                "recommended": bool(item.get("recommended")),
                "recommendation_reason": str(item.get("recommendation_reason") or "").strip()[:300],
            }
        )
    if not options:
        raise AIOutputValidationError("solution drafts require at least one option")
    # Batch 19: exactly one recommended option.  The model marked none -> the
    # first option wins; it marked several -> the first flag survives.
    recommended_seen = False
    for option in options:
        if option["recommended"] and not recommended_seen:
            recommended_seen = True
        else:
            option["recommended"] = False
    if not recommended_seen:
        options[0]["recommended"] = True
        if not options[0]["recommendation_reason"]:
            options[0]["recommendation_reason"] = "综合可行性、成本与风险后的首选方案。"
    return {
        "options": options,
        "limitations": _clamped_strings(value.get("limitations"), limit=10, max_length=1000, label="limitations"),
    }


# Batch 19: the AI-drafted decision proposal.  A pure draft contract -- the
# route never persists it; the user reviews/edits and posts it through the
# regular decision-proposal + approval flow.
DECISION_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "problem_statement", "proposed_action", "expected_impact", "risk_summary", "validation_plan"],
    "properties": {
        "title": {"type": "string"},
        "problem_statement": {"type": "string"},
        "proposed_action": {"type": "string"},
        "expected_impact": {"type": "string"},
        "risk_summary": {"type": "string"},
        "validation_plan": {"type": "string"},
    },
}


def validate_decision_draft(value: Any) -> dict[str, Any]:
    """Validate and normalize the AI-drafted decision proposal (batch 19)."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("decision draft must be a JSON object")
    required = ("title", "problem_statement", "proposed_action", "expected_impact", "risk_summary", "validation_plan")
    missing = [key for key in required if not str(value.get(key) or "").strip()]
    if missing:
        raise AIOutputValidationError(f"decision draft is missing required fields: {', '.join(missing)}")
    limits = {"title": 120, "problem_statement": 2000, "proposed_action": 4000, "expected_impact": 2000, "risk_summary": 2000, "validation_plan": 2000}
    return {key: str(value.get(key)).strip()[:limit] for key, limit in limits.items()}


# Stage 6 contract: one AI-interview round.  The model proposes up to five
# grounded questions; the server dedups against existing rows before persisting,
# so the schema only enforces shape and length.
INTERVIEW_QUESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["topic", "question_text", "rationale"],
                "properties": {
                    "topic": {"type": "string"},
                    "question_text": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        }
    },
}


def validate_interview_questions(value: Any) -> dict[str, Any]:
    """Validate and normalize one AI-interview round (3-5 questions requested,
    up to 5 accepted; the route layers dedup on top of this)."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("interview round must be a JSON object")
    raw_questions = value.get("questions")
    if not isinstance(raw_questions, list):
        raise AIOutputValidationError("interview round 'questions' must be an array")
    questions: list[dict[str, Any]] = []
    for item in raw_questions[:5]:
        if not isinstance(item, Mapping):
            raise AIOutputValidationError("interview questions must be objects")
        question_text = str(item.get("question_text") or "").strip()
        if not question_text:
            raise AIOutputValidationError("interview questions require non-empty question_text")
        questions.append(
            {
                "topic": str(item.get("topic") or "").strip()[:120],
                "question_text": question_text[:2000],
                "rationale": str(item.get("rationale") or "").strip()[:2000],
            }
        )
    if not questions:
        raise AIOutputValidationError("interview round requires at least one question")
    return {"questions": questions}


# Batch 18: adaptive one-question interview.  One call proposes the next
# single question OR declares the interview complete; the summary contract
# powers the end-of-interview digest.
NEXT_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["question_text", "topic", "rationale", "interview_complete", "completion_note"],
    "properties": {
        "question_text": {"type": "string"},
        "topic": {"type": "string"},
        "rationale": {"type": "string"},
        "interview_complete": {"type": "boolean"},
        "completion_note": {"type": "string"},
    },
}


def validate_next_question(value: Any) -> dict[str, Any]:
    """Validate the next-question contract.  A non-complete answer with an
    empty question is malformed and counts as a failed call."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("next question must be a JSON object")
    complete = bool(value.get("interview_complete"))
    question_text = str(value.get("question_text") or "").strip()
    if not complete and not question_text:
        raise AIOutputValidationError("next question requires question_text unless interview_complete")
    return {
        "question_text": question_text[:2000],
        "topic": str(value.get("topic") or "").strip()[:120],
        "rationale": str(value.get("rationale") or "").strip()[:2000],
        "interview_complete": complete,
        "completion_note": str(value.get("completion_note") or "").strip()[:2000],
    }


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["collected", "gaps", "ready_for"],
    "properties": {
        "collected": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "gaps": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "ready_for": {"type": "string"},
    },
}


def validate_interview_summary(value: Any) -> dict[str, Any]:
    """Validate the end-of-interview digest (batch 18)."""

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("interview summary must be a JSON object")

    def strings(key: str) -> list[str]:
        raw = value.get(key)
        if not isinstance(raw, list):
            raise AIOutputValidationError(f"interview summary '{key}' must be an array")
        return [str(item).strip()[:500] for item in raw[:10] if str(item).strip()]

    return {
        "collected": strings("collected"),
        "gaps": strings("gaps"),
        "ready_for": str(value.get("ready_for") or "").strip()[:2000],
    }


# Batch 21: field-semantics dictionary.  The parse pipeline sends one column
# profile per dataset and the model names every column's business meaning.
# The allowlist itself is untouched -- the outbound profile is a direct
# structured dict (same convention as the problem draft) and PII in the
# sample values is masked by the adapter layer.
FIELD_SEMANTICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dataset_label", "columns"],
    "properties": {
        "dataset_label": {"type": "string"},
        "columns": {
            "type": "array",
            "maxItems": 60,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "label", "description"],
                "properties": {
                    "name": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
    },
}


def validate_field_semantics(value: Any, *, known_columns: frozenset[str] | set[str] | None = None) -> dict[str, Any]:
    """Validate the field-semantics contract (batch 21).

    ``known_columns`` is the exact set of column names sent to the provider:
    an entry naming anything else is a hallucination and is dropped.  A
    duplicate column name keeps its first occurrence; text is truncated to the
    schema limits (label ≤40, description ≤200; storage columns give extra
    headroom).  Entries without a usable name/label are ignored rather than
    failing the whole response -- a partially useful dictionary beats none.
    """

    if not isinstance(value, Mapping):
        raise AIOutputValidationError("field semantics output must be a JSON object")
    raw_columns = value.get("columns")
    if not isinstance(raw_columns, list):
        raise AIOutputValidationError("field semantics 'columns' must be an array")
    columns: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_columns[:60]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        label = str(item.get("label") or "").strip()
        if not name or not label:
            continue
        if known_columns is not None and name not in known_columns:
            continue
        if name in seen:
            continue
        seen.add(name)
        columns.append(
            {
                "name": name[:255],
                "label": label[:40],
                "description": str(item.get("description") or "").strip()[:200],
            }
        )
    return {
        "dataset_label": str(value.get("dataset_label") or "").strip()[:60],
        "columns": columns,
    }


def empty_interview_round(*, limitation: str | None = None) -> dict[str, Any]:
    """Return a valid empty round for unavailable providers."""

    return {"questions": [], "limitations": [limitation] if limitation else []}


# Friendly aliases for callers/tests that use the wording from the V1.1 document.
validate_structured_ai_output = validate_ai_output
build_safe_ai_context = build_ai_context
extract_ai_insights = _extract_insights


__all__ = [
    "AIContextError",
    "AIOutputValidationError",
    "AI_OUTPUT_SCHEMA",
    "ALLOWED_CONTEXT_KEYS",
    "ALLOWED_CONTEXT_KEY_ORDER",
    "INTERVIEW_QUESTIONS_SCHEMA",
    "NEXT_QUESTION_SCHEMA",
    "PROBLEM_DRAFT_SCHEMA",
    "SUMMARY_SCHEMA",
    "REPORT_OUTPUT_SCHEMA",
    "DECISION_DRAFT_SCHEMA",
    "SOLUTION_DRAFTS_SCHEMA",
    "FIELD_SEMANTICS_SCHEMA",
    "DOCUMENT_SECTIONS_SCHEMA",
    "assert_safe_ai_context",
    "build_ai_context",
    "build_safe_ai_context",
    "empty_ai_output",
    "empty_interview_round",
    "empty_report_output",
    "extract_ai_insights",
    "validate_ai_output",
    "validate_decision_draft",
    "validate_document_sections",
    "validate_field_semantics",
    "validate_interview_questions",
    "validate_interview_summary",
    "validate_next_question",
    "validate_problem_draft",
    "validate_report_output",
    "validate_solution_drafts",
    "validate_structured_ai_output",
]
