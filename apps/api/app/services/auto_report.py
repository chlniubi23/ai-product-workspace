from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import (
    REPORT_OUTPUT_SCHEMA,
    assert_safe_ai_context,
    build_ai_context,
    empty_report_output,
    validate_report_output,
)
from ..analytics.engine import AnalysisEngine
from ..common import _require_pandas, model_dict
from ..config import settings
from ..models import AutoAnalysisReport, Dataset, DatasetVersion, Project, User, Workspace
from .ai_stages import _run_ai_stage
from .audit import audit
from .datasets import _read_dataframe

# ---------------------------------------------------------------------------
# Project-level auto analysis report (upload -> auto analysis -> report)
# ---------------------------------------------------------------------------

_REPORT_DATASET_LIMIT = 5
_REPORT_TREND_POINT_LIMIT = 80


def _latest_project_versions(db: Session, project: Project) -> list[DatasetVersion]:
    """Latest ready version of every active dataset, newest dataset first."""

    datasets = db.scalars(
        select(Dataset)
        .where(Dataset.project_id == project.id, Dataset.deleted_at.is_(None))
        .order_by(Dataset.created_at.desc())
    ).all()
    versions: list[DatasetVersion] = []
    for dataset in datasets:
        ready = [item for item in dataset.versions if item.status in {"ready", "confirmed"}]
        if not ready:
            continue
        versions.append(max(ready, key=lambda item: item.version_number or 0))
    return versions[:_REPORT_DATASET_LIMIT]


def _compute_report_aggregates(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Deterministic per-dataset aggregates that ground the report.

    Runs in a worker thread on plain data (no ORM session).  Payload keys are
    chosen to survive ``build_ai_context``'s sanitizer: lists of mappings must
    live under aggregate keys (``metrics``, ``categories``, ``periods``,
    ``counts``); row-like lists would be dropped at the boundary.  No raw rows
    are ever included -- the model narrates statistics, not cells.
    """
    pd = _require_pandas()

    _require_pandas()
    frame = _read_dataframe(settings.data_path / snapshot["storage_path"], snapshot["file_name"])
    engine = AnalysisEngine(snapshot["version_id"])
    eda = engine.run_eda(frame, top_n=5).to_dict()
    eda_payload = dict(eda.get("payload_json") or {})

    schema_types = {str(item["name"]): str(item["type"]) for item in snapshot.get("columns") or []}
    datetime_column = next((name for name, kind in schema_types.items() if kind == "datetime"), None)
    numeric_column = next((name for name, kind in schema_types.items() if kind in {"integer", "float"}), None)

    columns: list[dict[str, Any]] = []
    row_total = int(eda_payload.get("row_count") or 0)
    for item in list(eda_payload.get("columns") or [])[:30]:
        entry: dict[str, Any] = {
            "name": str(item.get("name")),
            "type": str(item.get("dtype")),
            "missing_rate": round(float(item.get("missing_rate") or 0), 4),
            "unique_count": int(item.get("unique_count") or 0),
        }
        stats = item.get("statistics")
        if isinstance(stats, dict) and stats:
            entry["statistics"] = {
                key: stats.get(key)
                for key in ("count", "mean", "median", "std", "min", "max")
                if stats.get(key) is not None
            }
        # Batch 13: a near-unique column (report_id, 20 rows / 20 unique) only
        # produces "top value: 1 row (5%)" noise in per-value distributions and
        # in the digest's concentration rule. Keep its missing statistics, drop
        # the top-values breakdown, and mark it so narration knows why.
        if row_total >= 10 and entry["unique_count"] >= 0.9 * row_total:
            entry["identifier"] = True
        else:
            top_values = item.get("top_values")
            if isinstance(top_values, list) and top_values:
                entry["categories"] = [
                    {"value": str(row.get("value")), "count": int(row.get("count") or 0), "rate": round(float(row.get("rate") or 0), 4)}
                    for row in top_values[:5]
                    if isinstance(row, Mapping)
                ]
        columns.append(entry)

    aggregates: dict[str, Any] = {
        "dataset_version_id": snapshot["version_id"],
        "name": snapshot["dataset_name"],
        "version_number": snapshot["version_number"],
        "row_count": int(snapshot.get("row_count") or len(frame)),
        "column_count": int(snapshot.get("column_count") or len(frame.columns)),
        "duplicate_rows": int(eda_payload.get("duplicate_rows") or 0),
        "metrics": columns,
    }
    if snapshot.get("quality_score") is not None:
        aggregates["quality_score"] = snapshot["quality_score"]
        aggregates["quality_status"] = snapshot.get("quality_status")
    if isinstance(snapshot.get("missing_values"), dict) and snapshot["missing_values"]:
        aggregates["missing_values"] = snapshot["missing_values"]
    if isinstance(snapshot.get("anomalies"), dict) and snapshot["anomalies"]:
        aggregates["anomalies"] = snapshot["anomalies"]

    correlations: dict[str, float] = {}
    for pair in list(eda_payload.get("correlations") or [])[:12]:
        if isinstance(pair, Mapping) and pair.get("correlation") is not None:
            correlations[f"{pair.get('left')} ~ {pair.get('right')}"] = round(float(pair["correlation"]), 4)
    if correlations:
        aggregates["correlation_pairs"] = correlations

    if datetime_column and numeric_column:
        try:
            parsed = pd.to_datetime(frame[datetime_column], errors="coerce", utc=True, format="mixed").dropna()
            span_days = (parsed.max() - parsed.min()).days if len(parsed) >= 2 else 0
            frequency = "W" if span_days > 70 else "D"
            trend = engine.run_trend_analysis(frame, time_column=datetime_column, metric_column=numeric_column, frequency=frequency).to_dict()
            rows = [row for row in (trend.get("payload_json") or {}).get("rows", []) if isinstance(row, Mapping)]
            if rows:
                values = [row.get("value") for row in rows]
                numeric_values = [float(value) for value in values if value is not None]
                aggregates["trend"] = {
                    "time_column": datetime_column,
                    "metric_column": numeric_column,
                    "frequency": frequency,
                    "periods": [str(row.get("period")) for row in rows[:_REPORT_TREND_POINT_LIMIT]],
                    "counts": values[:_REPORT_TREND_POINT_LIMIT],
                    "first_value": values[0],
                    "last_value": values[-1],
                    "max_value": max(numeric_values) if numeric_values else None,
                    "min_value": min(numeric_values) if numeric_values else None,
                    "last_period_change": rows[-1].get("period_over_period"),
                }
        except Exception:  # noqa: BLE001 - trend is optional grounding for the report
            pass

    # Batch 12: group comparison over a low-cardinality category column and a
    # complete numeric column.  The frame is already in memory here, so this is
    # one extra pandas pass, and the persisted breakdown gives business tables
    # (no event roles) real Pareto material for narration and documents.
    row_total = int(aggregates.get("row_count") or len(frame))
    group_column = next(
        (
            item.get("name")
            for item in aggregates.get("metrics") or []
            if isinstance(item, dict)
            and item.get("categories")
            and isinstance(item.get("unique_count"), int)
            and row_total
            and 0 < item["unique_count"] / row_total <= 0.4
        ),
        None,
    )
    value_column = next(
        (
            item.get("name")
            for item in aggregates.get("metrics") or []
            if isinstance(item, dict)
            and isinstance(item.get("statistics"), dict)
            and item["statistics"].get("mean") is not None
        ),
        None,
    )
    if group_column and value_column:
        try:
            comparison = engine.run_group_comparison(
                frame, group_column=str(group_column), value_column=str(value_column), aggregation="mean", top_n=10
            )
            breakdown = [row for row in comparison.payload.get("categories") or [] if isinstance(row, dict)]
            if breakdown:
                aggregates["breakdown"] = breakdown
                aggregates["breakdown_column"] = f"{group_column} ~ {value_column}"
        except Exception:  # noqa: BLE001 - breakdown is optional grounding
            pass
    return aggregates


def _compute_report_aggregates_batch(snapshots: list[dict[str, Any]]) -> list[Any]:
    """Aggregate every snapshot, keeping per-dataset failures isolated.

    One unreadable file must not sink the whole report: a failing dataset
    returns its exception instance, which the caller records as a
    ``read_failure`` while the remaining datasets still produce sections.
    """

    results: list[Any] = []
    for snapshot in snapshots:
        try:
            results.append(_compute_report_aggregates(snapshot))
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            results.append(exc)
    return results


def _deterministic_report_parts(
    project_name: str,
    aggregates: list[dict[str, Any]],
    digest: list[dict[str, Any]] | None = None,
) -> tuple[str, str, list[dict[str, str]], list[str]]:
    """Deterministic report body used directly when AI is unavailable, and as
    the persisted trace of the numbers behind an AI-written report.

    ``digest`` is the rule-based findings digest (``build_findings_digest``);
    when present, the key-findings section is generated from its statements --
    strictly more informative than the legacy top-category/missing-rate
    listing, which is kept as the fallback for empty digests.
    """

    title = f"{project_name} 数据分析报告"
    total_rows = sum(int(item.get("row_count") or 0) for item in aggregates)
    summary = (
        f"本次分析覆盖 {len(aggregates)} 个数据集，共 {total_rows} 行数据。"
        "以下统计全部由确定性计算生成。"
    )

    sections: list[dict[str, str]] = []
    findings: list[str] = []

    overview_lines: list[str] = []
    for item in aggregates:
        line = f"- **{item.get('name')}**：{item.get('row_count')} 行 × {item.get('column_count')} 列"
        if item.get("quality_score") is not None:
            line += f"，质量分 {item.get('quality_score')}（{item.get('quality_status')}）"
        overview_lines.append(line)
    if overview_lines:
        sections.append({"heading": "一、数据概况", "content": "\n".join(overview_lines)})

    distribution_lines: list[str] = []
    statistic_lines: list[str] = []
    for item in aggregates:
        for column in item.get("metrics") or []:
            label = f"{item.get('name')} · {column.get('name')}"
            categories = column.get("categories") or []
            if categories:
                top = categories[0]
                line = f"- {label}：最高占比「{top.get('value')}」{top.get('count')} 条（{round(float(top.get('rate') or 0) * 100, 1)}%）"
                runners = "、".join(f"「{row.get('value')}」{round(float(row.get('rate') or 0) * 100, 1)}%" for row in categories[1:3])
                if runners:
                    line += f"，其次 {runners}"
                distribution_lines.append(line)
                findings.append(f"{label} 中「{top.get('value')}」占比最高（{top.get('count')} 条，{round(float(top.get('rate') or 0) * 100, 1)}%）")
            stats = column.get("statistics")
            if isinstance(stats, dict) and stats.get("mean") is not None:
                median = stats.get("median")
                statistic_lines.append(
                    f"- {label}：均值 {round(float(stats['mean']), 4)}"
                    + (f"，中位数 {round(float(median), 4)}" if median is not None else "")
                    + (f"，范围 [{round(float(stats['min']), 4)}, {round(float(stats['max']), 4)}]" if stats.get("min") is not None and stats.get("max") is not None else "")
                )
                if column.get("missing_rate"):
                    findings.append(f"{label} 缺失率 {round(float(column['missing_rate']) * 100, 1)}%")
    if distribution_lines:
        sections.append({"heading": "二、维度分布", "content": "\n".join(distribution_lines)})
    if statistic_lines:
        sections.append({"heading": "三、数值统计", "content": "\n".join(statistic_lines)})

    trend_lines: list[str] = []
    for item in aggregates:
        trend = item.get("trend")
        if not isinstance(trend, dict):
            continue
        change = trend.get("last_period_change")
        change_text = f"，最近一期环比 {round(float(change) * 100, 1)}%" if isinstance(change, (int, float)) else ""
        trend_lines.append(
            f"- {item.get('name')}：{trend.get('metric_column')} 按 {'周' if trend.get('frequency') == 'W' else '日'} 汇总共 {len(trend.get('periods') or [])} 期，"
            f"从 {trend.get('first_value')} 变化到 {trend.get('last_value')}{change_text}"
        )
    if trend_lines:
        sections.append({"heading": "四、时间趋势", "content": "\n".join(trend_lines)})

    digest_statements = [
        str(item.get("statement")).strip()
        for item in (digest or [])
        if isinstance(item, dict) and str(item.get("statement") or "").strip()
    ]
    if digest_statements:
        return title, summary, sections, digest_statements[:12]
    return title, summary, sections, findings[:12]


def _report_markdown(title: str, summary: str, sections: list[dict[str, str]], findings: list[str], recommendations: list[str], limitations: list[str]) -> str:
    parts: list[str] = [f"# {title}", ""]
    if summary:
        parts += [summary, ""]
    for section in sections:
        parts += [f"## {section.get('heading', '')}", "", section.get("content", ""), ""]
    if findings:
        parts += ["## 关键发现", ""] + [f"- {item}" for item in findings] + [""]
    if recommendations:
        parts += ["## 建议", ""] + [f"- {item}" for item in recommendations] + [""]
    if limitations:
        parts += ["## 局限", ""] + [f"- {item}" for item in limitations] + [""]
    return "\n".join(parts).strip()


def _auto_report_payload(report: AutoAnalysisReport) -> dict[str, Any]:
    payload = model_dict(report)
    # Convenience alias: the web client renders the markdown directly.
    payload["markdown"] = report.content_markdown
    return payload


# ---------------------------------------------------------------------------
# AI narration (batch 10): the async half of "compute first, narrate later"
# ---------------------------------------------------------------------------

_NARRATION_FEATURE = "auto_report_narration"


def _auto_report_system_prompt() -> str:
    """Narration prompt, shared verbatim by the legacy combined endpoint and
    the ``auto_report_narration`` job handler.  The response schema itself is
    appended by ``_run_ai_stage``."""

    return (
        "你是资深产品数据分析师，为产品团队撰写数据分析报告。只使用给定的聚合统计，"
        "禁止编造任何未提供的数字，禁止输出或猜测原始行数据。要求："
        "1) title 概括数据主题；2) summary 用 3-5 句话概述数据规模、质量与总体结论；"
        "3) sections 分 3-6 个主题章节（如 数据概况、核心维度分布、数值统计、时间趋势、数据质量），"
        "每章 content 用 Markdown，包含要点列表与具体数字，每章不超过 400 字；"
        "4) key_findings 列出最重要的发现（最多 8 条），每条必须包含具体数字；"
        "5) recommendations 给出可执行的下一步（最多 6 条），与发现一一对应；"
        "6) limitations 写明分析局限（自动选列、聚合统计、相关性不代表因果）。"
        "输出必须完整闭合 JSON，全部使用中文。"
    )


def _report_ai_context(
    goal: str,
    aggregates: list[dict[str, Any]],
    digest: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Allow-listed provider context, rebuilt from persisted aggregates.

    The aggregates are the exact payloads stored in
    ``deterministic_json.datasets``, so narration after the fact sees the same
    numbers the compute step grounded the report on.  Each digest finding is
    injected as its own ``finding`` artifact through the artifacts channel --
    every payload key is a firewall aggregate key, so the allowlist itself is
    untouched (batch 12).
    """

    artifacts: list[dict[str, Any]] = [
        {
            "id": item["dataset_version_id"],
            "artifact_type": "dataset_summary",
            "title": item.get("name"),
            "payload_json": item,
        }
        for item in aggregates
    ]
    for index, item in enumerate(digest or [], start=1):
        if not isinstance(item, dict):
            continue
        artifacts.append(
            {
                "id": f"finding-{index}",
                "artifact_type": "finding",
                "title": str(item.get("statement") or "")[:200],
                "payload_json": {
                    "kind": str(item.get("kind") or ""),
                    "dataset": str(item.get("dataset") or ""),
                    "severity": int(item.get("severity") or 1),
                    "rate": item.get("value"),
                    "metrics": [str(column) for column in item.get("columns") or []],
                },
            }
        )
    return assert_safe_ai_context(
        build_ai_context(
            goal=goal or "",
            artifacts=artifacts,
            question=(
                "请基于这些聚合统计生成分章节的数据分析报告：先概述数据规模与质量，"
                "再按维度分布、数值统计、时间趋势等主题分章展开，最后给出关键发现与建议。"
                "所有数字必须来自给定统计，不得编造。"
            ),
        )
    )


async def _narrate_report(
    db: Session,
    user: User,
    workspace: Workspace,
    project: Project,
    report: AutoAnalysisReport,
) -> dict[str, Any]:
    """Run the AI narration for a computed report and persist the outcome.

    Success re-renders ``content_markdown`` with the deterministic sections
    kept in front and the AI interpretation appended, upgrades the status to
    ``succeeded`` and records ``ai_run_id``.  Any degraded outcome (not
    configured, provider failure, invalid output) leaves the deterministic
    body untouched: the status stays ``not_configured`` and only ``error_code``
    records the reason.  A budget-valve rejection raises HTTPException(429)
    from ``_run_ai_stage``'s pre-call check (batch 8 semantics) before any
    provider spend -- callers keep the compute-only report in that case.
    """

    deterministic_json = report.deterministic_json if isinstance(report.deterministic_json, dict) else {}
    aggregates = [item for item in deterministic_json.get("datasets") or [] if isinstance(item, dict)]
    if not aggregates:
        raise ValueError("report has no deterministic aggregates to narrate")
    digest = [item for item in deterministic_json.get("findings") or [] if isinstance(item, dict)]

    system_prompt = _auto_report_system_prompt()
    if digest:
        system_prompt += (
            " 给定的 findings 是规则从数据中提炼的重点发现，叙述中的关键发现必须逐条覆盖这些内容，"
            "不得遗漏，也不得虚构清单之外的发现。"
        )
    result = await _run_ai_stage(
        db=db,
        user=user,
        workspace=workspace,
        feature_name=_NARRATION_FEATURE,
        system_prompt=system_prompt,
        context=_report_ai_context(project.goal_statement or "", aggregates, digest),
        flag_name="auto_report_enabled",
        response_schema=REPORT_OUTPUT_SCHEMA,
        output_validator=validate_report_output,
        empty_output=dict(empty_report_output(limitation="AI provider is not configured.")),
        min_output_tokens=8192,
    )
    if result.get("status") == "succeeded":
        output = result["output"]
        deterministic_sections = [section for section in (report.sections_json or []) if isinstance(section, dict)]
        report.title = str(output.get("title") or report.title)[:255]
        report.summary = str(output.get("summary") or "")
        report.sections_json = deterministic_sections + list(output.get("sections") or [])
        report.key_findings = list(output.get("key_findings") or [])
        report.recommendations = list(output.get("recommendations") or [])
        report.limitations = list(output.get("limitations") or [])
        report.content_markdown = _report_markdown(
            report.title, report.summary, report.sections_json, report.key_findings, report.recommendations, report.limitations
        )
        report.status = "succeeded"
        report.error_code = None
        report.ai_run_id = result.get("run_id")
        audit(
            db,
            report.workspace_id,
            user.id,
            "report.narrated",
            "auto_report",
            report.id,
            {
                "feature": _NARRATION_FEATURE,
                "prompt_tokens": (result.get("usage") or {}).get("prompt_tokens"),
                "completion_tokens": (result.get("usage") or {}).get("completion_tokens"),
            },
        )
    else:
        # Numbers-first by construction: the deterministic body stays the
        # report of record and only the failure reason is recorded.
        report.status = "not_configured"
        report.error_code = str(result.get("error_code") or "AI_UNAVAILABLE")[:80]
        audit(db, report.workspace_id, user.id, "report.narration_degraded", "auto_report", report.id, {"error_code": report.error_code})
    db.commit()
    return result
