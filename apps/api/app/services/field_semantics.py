"""Batch 21: LLM field-semantics dictionary for freshly parsed datasets.

After the deterministic parse pipeline has profiled every column, one optional
AI call names each column's business meaning (a short label plus a one-line
reading) and the dataset as a whole.  Results land on the data dictionary
(``data_columns.semantic_label/semantic_description``, matched strictly by
column name) and in ``dataset_versions.schema_json["dataset_label"]``, so
every downstream AI context -- report narration, interview, distillation,
documents -- carries the business meaning of the fields it reasons about.

This is an optional enhancement in the same spirit as ``_run_auto_analyses``:
without a provider key it degrades to a no-op, and the handler wraps the call
so that no failure here can ever fail the parse job.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..ai_context import FIELD_SEMANTICS_SCHEMA, validate_field_semantics
from ..models import Dataset, DatasetVersion, User, Workspace
from .ai_stages import _run_ai_stage

# Output size expectation: ~3k tokens worst case (60 columns x ~50 chars).
_SYSTEM_PROMPT = (
    "你是产品数据字典助手。根据给定的数据集字段画像（字段名、类型、唯一率、缺失率、样例值），"
    "为每个字段推断其业务含义：label 是不超过 10 个字的业务短标签（如「参会人数」），"
    "description 是一句话业务解读（不超过 50 字，说明这个字段记录了什么、如何被使用）。"
    "dataset_label 用不超过 15 个字概括整个数据集的业务主题。"
    "只能为给定字段命名，禁止发明输入中不存在的字段；无法判断含义的字段直接跳过。"
    "输出必须完整闭合 JSON，全部使用中文。"
)


def _column_profile(schema: list[dict[str, Any]], frame: Any) -> list[dict[str, Any]]:
    """Plain-data per-column profile sent to the provider (≤60 columns).

    Sample values go through the adapter's PII masking on the way out -- no
    extra redaction is applied here, matching the problem-draft convention.
    """

    profile: list[dict[str, Any]] = []
    for item in schema[:60]:
        name = str(item.get("name") or "")
        if not name or name not in frame.columns:
            continue
        series = frame[name]
        total = len(series)
        non_null = series.dropna()
        entry: dict[str, Any] = {
            "name": name,
            "type": str(item.get("inferred_type") or item.get("type") or "unknown"),
            "unique_ratio": round(float(item.get("unique_ratio") or 0), 4),
            "missing_rate": round(1 - len(non_null) / total, 4) if total else 0.0,
            "top_values": [str(value) for value in non_null.astype(str).unique()[:5] if str(value).strip()],
        }
        # Batch 14 derived columns carry their provenance so the model does not
        # mistake "metrics_summary__DAU" for a raw uploaded field.
        if item.get("source") == "extracted":
            entry["extracted"] = True
            entry["source_column"] = name.split("__", 1)[0]
        profile.append(entry)
    return profile


async def interpret_fields(
    db: Session,
    *,
    workspace: Workspace,
    user: User,
    dataset: Dataset,
    version: DatasetVersion,
    frame: Any,
    schema: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run the field-semantics AI pass and persist the dictionary.

    Returns ``{status, error_code, labeled, skipped}``; the caller audits and
    isolates failures.  Only entries whose ``name`` exactly matches an input
    column survive validation, so a hallucinated column can never be written.
    """

    profile = _column_profile(schema, frame)
    known = {item["name"] for item in profile}
    if not profile:
        return {"status": "skipped", "error_code": None, "labeled": 0, "skipped": 0}
    context = {
        "dataset": {
            "name": str(dataset.name or ""),
            "file_name": str(version.file_name or ""),
            "row_count": int(version.row_count or len(frame)),
        },
        "columns": profile,
    }
    try:
        stage = await _run_ai_stage(
            db=db,
            user=user,
            workspace=workspace,
            feature_name="field_semantics",
            system_prompt=_SYSTEM_PROMPT,
            context=context,
            flag_name="insight_suggestions_enabled",
            response_schema=FIELD_SEMANTICS_SCHEMA,
            output_validator=partial(validate_field_semantics, known_columns=known),
            empty_output={"dataset_label": "", "columns": []},
        )
    except HTTPException as exc:
        # Budget-valve rejection (429, pre-call, zero spend) or a mid-flight
        # feature-flag flip: the AIRun bookkeeping already happened inside.
        code = str(exc.detail.get("code") or "AI_REJECTED") if isinstance(exc.detail, dict) else "AI_REJECTED"
        return {"status": "failed", "error_code": code, "labeled": 0, "skipped": len(known)}
    if stage.get("status") != "succeeded":
        # Not configured / provider failure / invalid output: leave both
        # semantic columns NULL and let the parse carry on.
        return {
            "status": str(stage.get("status") or "failed"),
            "error_code": stage.get("error_code"),
            "labeled": 0,
            "skipped": len(known),
        }
    output = stage.get("output") or {}
    by_name = {item.get("name"): item for item in output.get("columns") or [] if isinstance(item, dict)}
    labeled = 0
    for column in version.columns:
        match = by_name.get(column.name)
        if match is None:
            continue
        column.semantic_label = str(match.get("label") or "")[:120]
        column.semantic_description = str(match.get("description") or "")[:600]
        labeled += 1
    dataset_label = str(output.get("dataset_label") or "").strip()
    if dataset_label:
        # Reassign the mapping so SQLAlchemy detects the JSON mutation.
        payload = dict(version.schema_json or {})
        payload["dataset_label"] = dataset_label[:60]
        version.schema_json = payload
    return {"status": "succeeded", "error_code": None, "labeled": labeled, "skipped": max(0, len(known) - labeled)}
