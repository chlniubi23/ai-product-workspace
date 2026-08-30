from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.engine import AnalysisEngine
from ..common import _require_pandas, model_dict
from ..config import settings
from ..models import AutoAnalysisReport, Dataset, DatasetVersion, Project
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
    project_name: str, aggregates: list[dict[str, Any]]
) -> tuple[str, str, list[dict[str, str]], list[str]]:
    """Deterministic report body used directly when AI is unavailable, and as
    the persisted trace of the numbers behind an AI-written report."""

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
