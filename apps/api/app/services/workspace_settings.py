from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..common import error, model_dict, serialize
from ..config import settings
from ..infrastructure.llm.deepseek import DeepSeekSettings
from ..models import AIRun, MetricDefinition, User, Workspace
from ..schemas import WorkspaceSettings, WorkspaceSettingsPatch
from ..services.audit import audit


def _workspace_settings(workspace: Workspace) -> dict[str, Any]:
    """Return the allowlisted settings contract without exposing legacy secrets."""

    defaults = WorkspaceSettings().model_dump()
    # Keep the workspace view aligned with the provider health endpoint when a
    # workspace has no explicit model override.  Otherwise the UI would show
    # the schema fallback (``deepseek-chat``) while the configured server model
    # could be different.
    defaults["ai_model_id"] = DeepSeekSettings.from_app_settings(settings).model
    source = workspace.settings_json if isinstance(workspace.settings_json, dict) else {}
    candidate = dict(defaults)
    for key in (
        "timezone",
        "data_retention_days",
        "analysis_threshold",
        "ai_model_id",
        "ai_max_output_tokens",
        "ai_per_request_token_budget",
        "ai_daily_token_budget",
    ):
        if key in source:
            candidate[key] = source[key]

    flags = dict(defaults["feature_flags"])
    stored_flags = source.get("feature_flags")
    if isinstance(stored_flags, dict):
        for key in flags:
            if key in stored_flags:
                flags[key] = stored_flags[key]
    for key in ("anomaly_alert", "quality_alert", "review_alert", "email_digest"):
        if key in source:
            flags[key] = source[key]
    candidate["feature_flags"] = flags

    try:
        return WorkspaceSettings.model_validate(candidate).model_dump()
    except Exception:
        # Invalid legacy values are never reflected. The next valid PATCH will
        # rewrite the stored JSON to the canonical, allowlisted representation.
        return defaults


def _merge_workspace_settings(workspace: Workspace, patch: WorkspaceSettingsPatch) -> tuple[dict[str, Any], list[str]]:
    merged = _workspace_settings(workspace)
    updates = patch.model_dump(exclude_unset=True)
    feature_updates = updates.pop("feature_flags", None)
    changed_fields = list(updates)
    merged.update(updates)
    if isinstance(feature_updates, dict):
        merged["feature_flags"] = {**merged["feature_flags"], **feature_updates}
        changed_fields.extend(f"feature_flags.{key}" for key in feature_updates)

    validated = WorkspaceSettings.model_validate(merged)
    if validated.ai_max_output_tokens > validated.ai_per_request_token_budget:
        raise error("VALIDATION_ERROR", "AI max output cannot exceed the per-request token budget", 400)
    if validated.ai_per_request_token_budget > validated.ai_daily_token_budget:
        raise error("VALIDATION_ERROR", "Per-request token budget cannot exceed the daily token budget", 400)
    workspace.settings_json = validated.model_dump()
    return workspace.settings_json, changed_fields


def _token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _workspace_day_start(workspace: Workspace) -> datetime:
    settings_json = _workspace_settings(workspace)
    try:
        local_zone = ZoneInfo(str(settings_json.get("timezone") or "UTC"))
    except Exception:
        local_zone = UTC
    local_now = datetime.now(local_zone)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(UTC).replace(tzinfo=None)


def _workspace_ai_budget(workspace: Workspace) -> dict[str, Any]:
    workspace_settings = _workspace_settings(workspace)
    provider_settings = DeepSeekSettings.from_app_settings(settings)
    per_request = max(1, _token_count(workspace_settings.get("ai_per_request_token_budget")))
    return {
        "per_request": per_request,
        "daily": max(1, _token_count(workspace_settings.get("ai_daily_token_budget") or provider_settings.daily_token_budget)),
        "max_output": min(per_request, max(1, _token_count(workspace_settings.get("ai_max_output_tokens")))),
        "model": str(workspace_settings.get("ai_model_id") or provider_settings.model),
    }


def _workspace_token_usage(db: Session, workspace: Workspace, per_request_budget: int, exclude_run_id: str | None = None) -> int:
    rows = db.scalars(
        select(AIRun).where(
            AIRun.workspace_id == workspace.id,
            AIRun.status.in_(["running", "succeeded"]),
            AIRun.created_at >= _workspace_day_start(workspace),
        )
    ).all()
    total = 0
    for run in rows:
        if exclude_run_id and run.id == exclude_run_id:
            continue
        observed = _token_count(run.prompt_tokens) + _token_count(run.completion_tokens)
        if run.status == "running":
            # A running provider call has no final usage yet. Reserve the whole
            # per-request budget so concurrent requests cannot oversubscribe.
            total += max(per_request_budget, observed)
        elif observed:
            total += observed
        else:
            # Legacy succeeded runs may predate provider usage persistence.
            total += per_request_budget
    return total


def _reject_ai_budget(
    db: Session,
    workspace: Workspace,
    user: User,
    ai_run: AIRun,
    budget: dict[str, Any],
    *,
    daily_used: int,
    observed_tokens: int = 0,
    reason: str = "Workspace AI token budget exceeded",
) -> None:
    details = {
        "daily_used_tokens": daily_used,
        "daily_token_budget": budget["daily"],
        "per_request_token_budget": budget["per_request"],
        "observed_tokens": observed_tokens,
    }
    ai_run.status = "failed"
    ai_run.error_code = "AI_BUDGET_EXCEEDED"
    ai_run.input_summary_json = {**(ai_run.input_summary_json or {}), "budget": details, "events": [{"type": "run.failed", "data": {"code": "AI_BUDGET_EXCEEDED", "retryable": False}}]}
    audit(db, workspace.id, user.id, "copilot.budget_exceeded", "ai_run", ai_run.id, details)
    db.commit()
    raise error("AI_BUDGET_EXCEEDED", reason, 429, details)




def _workspace_payload(workspace: Workspace, role: str | None = None) -> dict[str, Any]:
    payload = model_dict(workspace)
    payload["settings_json"] = _workspace_settings(workspace)
    if role is not None:
        payload["role"] = role
    return payload




def _metric_payload(metric: MetricDefinition) -> dict[str, Any]:
    return {
        "id": metric.id,
        "workspace_id": metric.workspace_id,
        "name": metric.name,
        "definition": metric.definition,
        "category": metric.category,
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "unit": metric.unit,
        "aggregation_period": metric.aggregation_period,
        "field_mapping": metric.field_mapping_json or {},
        "display_format": metric.display_format,
        "maintainer_id": metric.updated_by,
        "created_at": serialize(metric.created_at),
        "updated_at": serialize(metric.updated_at),
    }


def _migrate_legacy_metric_dictionary(db: Session, workspace: Workspace) -> None:
    active_count = db.scalar(
        select(func.count()).select_from(MetricDefinition).where(
            MetricDefinition.workspace_id == workspace.id,
            MetricDefinition.deleted_at.is_(None),
        )
    ) or 0
    source = workspace.settings_json if isinstance(workspace.settings_json, dict) else {}
    legacy_metrics = source.get("metric_dictionary")
    if active_count or not isinstance(legacy_metrics, list):
        return

    migrated = 0
    period_map = {"日": "day", "周": "week", "月": "month", "daily": "day", "weekly": "week", "monthly": "month"}
    for item in legacy_metrics:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        definition = str(item.get("definition") or "").strip()
        if not name or not definition:
            continue
        unit = str(item.get("unit") or "").strip()
        display_format = str(item.get("display_format") or ("percentage" if "百分比" in unit or unit == "%" else "number"))
        if display_format not in {"number", "percentage", "duration", "currency"}:
            display_format = "number"
        db.add(
            MetricDefinition(
                workspace_id=workspace.id,
                name=name[:255],
                definition=definition[:4000],
                category=str(item.get("category") or "other") if item.get("category") in {"active", "retention", "conversion", "quality", "cost", "feedback", "other"} else "other",
                numerator=str(item.get("numerator") or "")[:4000],
                denominator=str(item.get("denominator") or "")[:4000],
                unit=unit[:80],
                aggregation_period=period_map.get(str(item.get("aggregation_period") or item.get("period") or ""), "day"),
                field_mapping_json=item.get("field_mapping") if isinstance(item.get("field_mapping"), dict) else {},
                display_format=display_format,
                created_by=workspace.owner_id,
                updated_by=workspace.owner_id,
            )
        )
        migrated += 1
    if not migrated:
        return
    workspace.settings_json = _workspace_settings(workspace)
    audit(db, workspace.id, None, "metric_dictionary.migrated", "workspace", workspace.id, {"count": migrated}, "system")
    db.commit()
