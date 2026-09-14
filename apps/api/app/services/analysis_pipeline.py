from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.corelation import EXCLUDED_PAIRS_DETAIL_LIMIT, compute_full_correlation_matrix
from ..analytics.dag import build_lineage_map
from ..analytics.engine import AnalysisEngine, choose_trend_frequency
from ..analytics.outliers import build_outlier_aggregates, build_raw_outliers_map
from ..analytics.quality import generate_quality_report
from ..analytics.types import TYPE_LABELS, compute_type_aware_stats
from ..common import _require_pandas, error, pd, serialize
from ..models import AnalysisArtifact, AnalysisRun, DataColumn, DataQualityReport, DatasetVersion, Project, now
from ..schemas import AnalysisCreate
from ..services.audit import audit
from .datasets import _json_records

#: Phase 1 新增的增强分析类型（**仅手动可选**，不进 `_auto_analysis_plan`）。
ENHANCED_ANALYSIS_TYPES = frozenset({"correlation_analysis", "type_profile", "outlier_objects"})


def _extracted_column_names(version: DatasetVersion) -> set[str]:
    """``DataColumn.source == "extracted"`` 的列名集合 —— 派生列的**权威来源**。

    解析阶段（``job_handlers._handle_dataset_parse``）把字段字典写进
    ``version.schema_json`` 并同步落库 ``data_columns.source``，两者同源。这里读
    ``schema_json`` 而不是 ``version.columns`` 关系，避免在无 session 的调用路径上
    触发额外查询。
    """

    schema = version.schema_json
    if isinstance(schema, Mapping):
        entries = schema.get("columns") or []
    elif isinstance(schema, list):
        entries = schema
    else:
        entries = []
    extracted = {
        str(column.name)
        for column in (getattr(version, "columns", None) or [])
        if getattr(column, "source", None) == "extracted" and getattr(column, "name", None)
    }
    if extracted:
        return extracted
    return {
        str(item.get("name"))
        for item in entries
        if isinstance(item, Mapping) and item.get("source") == "extracted" and item.get("name")
    }


def _lineage_for_version(frame: pd.DataFrame, version: DatasetVersion) -> dict[str, Any]:
    """合并派生列的两套判定（抽取记录为权威 + dag 列名模式识别叠加）。"""

    return build_lineage_map([str(column) for column in frame.columns], _extracted_column_names(version))


def _numeric_targets(frame: pd.DataFrame, config: Mapping[str, Any]) -> list[str]:
    """新分析类型的数值列目标：``columns`` 白名单优先，其次 ``metric_column``，否则全部数值列。"""

    pd = _require_pandas()
    requested = config.get("columns")
    if isinstance(requested, list) and requested:
        candidates = [str(name) for name in requested]
    elif config.get("metric_column"):
        candidates = [str(config["metric_column"])]
    else:
        candidates = [str(column) for column in frame.select_dtypes(include="number").columns]
    return [name for name in candidates if name in frame.columns and pd.api.types.is_numeric_dtype(frame[name])]


def _correlation_analysis_artifact(frame: pd.DataFrame, version: DatasetVersion, config: dict[str, Any]) -> dict[str, Any]:
    """增强相关性分析：每对变量带 p 值与稳健估计，并排除伪相关对。

    产物形状按 §3 对齐防火墙：变量对放在 ``pairs``（Phase 1 决策 2 新增的聚合键）
    下，每项全为标量，**不含行级数据**，因此不需要放宽 ``_ROW_LIST_KEYS``。
    """

    pd = _require_pandas()
    numeric_columns = _numeric_targets(frame, config)
    lineage_map = _lineage_for_version(frame, version)
    outliers_map = build_raw_outliers_map(frame, numeric_columns) if numeric_columns else {}

    matrix, results, mechanical_excluded, excluded_derived_detail = compute_full_correlation_matrix(
        frame,
        numeric_columns,
        lineage_map,
        outliers_map=outliers_map,
    )
    excluded_detail = [*excluded_derived_detail, *mechanical_excluded]
    labels = [str(column) for column in numeric_columns]
    heatmap: list[list[Any]] = []
    for row_index, row_name in enumerate(labels):
        for column_index, column_name in enumerate(labels):
            value = matrix.loc[row_name, column_name] if labels else None
            heatmap.append([column_index, row_index, round(float(value), 4) if pd.notna(value) else None])

    option = {
        "tooltip": {"position": "top"},
        "grid": {"left": 104, "right": 24, "top": 20, "bottom": 64},
        "xAxis": {"type": "category", "data": labels, "splitArea": {"show": True}},
        "yAxis": {"type": "category", "data": labels, "splitArea": {"show": True}},
        "visualMap": {"min": -1, "max": 1, "calculable": True, "orient": "horizontal", "left": "center", "bottom": 0},
        "series": [{"name": "Pearson r", "type": "heatmap", "data": heatmap, "label": {"show": True, "formatter": "{@[2]}"}}],
    }
    payload = {
        "datasetVersionId": version.id,
        "configSnapshot": config,
        "chartType": "correlation_heatmap",
        "title": "Correlation analysis",
        "pairs": [result.to_dict() for result in results],
        "excluded_correlation_pairs": len(excluded_detail),
        "excluded_correlation_pairs_detail": excluded_detail[:EXCLUDED_PAIRS_DETAIL_LIMIT],
        "chart": {"type": "correlation_heatmap", "labels": labels, "data": heatmap},
        "option": option,
    }
    return {"artifact_type": "chart", "title": "Correlation analysis", "payload_json": payload}


def _type_profile_artifact(frame: pd.DataFrame, version: DatasetVersion, config: dict[str, Any]) -> dict[str, Any]:
    """类型感知统计：序数列出分布+众数、占比列出分位数+极值，其余出标准描述统计。

    每列一项 ``metrics`` 条目（``[{name, ...}]`` 列表形状，不是「以列名为键的 dict」）。
    刻意**不搬运** ``min_row_index`` / ``max_row_index`` / ``range_info`` —— 它们携带
    行身份（``range_info`` 文本里写着「第 N 行」），按决策 1 一律不出站。
    """

    stats = compute_type_aware_stats(frame)
    metrics: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for name, info in stats.items():
        if not isinstance(info, Mapping):
            continue
        inferred = str(info.get("inferred_type") or "unknown")
        label = TYPE_LABELS.get(inferred, "未知")
        entry: dict[str, Any] = {
            "name": str(name),
            "inferred_type": inferred,
            "type_label": label,
            "dtype": str(info.get("dtype") or ""),
            "confidence": info.get("confidence"),
        }
        for key in ("distribution", "percentages", "mode", "mode_percentage", "quantiles", "min_value", "max_value", "mean", "median", "n_total"):
            value = info.get(key)
            if value is not None:
                entry[key] = value
        statistics = info.get("statistics")
        if isinstance(statistics, Mapping):
            entry["statistics"] = {key: value for key, value in statistics.items() if value is not None}
        metrics.append(entry)
        counts[label] = counts.get(label, 0) + 1

    data = [{"name": label, "value": count} for label, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)]
    option = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 52, "right": 20, "top": 24, "bottom": 44},
        "xAxis": {"type": "category", "data": [item["name"] for item in data]},
        "yAxis": {"type": "value"},
        "series": [{"name": "列数", "type": "bar", "data": [item["value"] for item in data], "itemStyle": {"color": "#6366f1"}}],
    }
    payload = {
        "datasetVersionId": version.id,
        "configSnapshot": config,
        "chartType": "count_bar",
        "title": "Type-aware statistics",
        "metrics": metrics,
        "chart": {"type": "count_bar", "data": data, "unit": "列"},
        "option": option,
    }
    return {"artifact_type": "metric", "title": "Type-aware statistics", "payload_json": payload}


def _outlier_objects_artifact(frame: pd.DataFrame, version: DatasetVersion, config: dict[str, Any]) -> dict[str, Any]:
    """对象级离群值摘要（决策 1 · 方案 A：按列有界聚合，**不含行号**）。"""

    metrics = build_outlier_aggregates(frame, _numeric_targets(frame, config))
    data = [{"name": str(item["name"]), "value": int(item["count"])} for item in metrics]
    option = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 52, "right": 20, "top": 24, "bottom": 64},
        "xAxis": {"type": "category", "axisLabel": {"rotate": 28}, "data": [item["name"] for item in data]},
        "yAxis": {"type": "value"},
        "series": [{"name": "离群值个数", "type": "bar", "data": [item["value"] for item in data], "itemStyle": {"color": "#6366f1"}}],
    }
    payload = {
        "datasetVersionId": version.id,
        "configSnapshot": config,
        "chartType": "count_bar",
        "title": "Outlier objects",
        "metrics": metrics,
        "chart": {"type": "count_bar", "data": data, "unit": "个"},
        "option": option,
    }
    return {"artifact_type": "chart", "title": "Outlier objects", "payload_json": payload}


_ENHANCED_ARTIFACT_BUILDERS = {
    "correlation_analysis": _correlation_analysis_artifact,
    "type_profile": _type_profile_artifact,
    "outlier_objects": _outlier_objects_artifact,
}


def _analysis_artifacts(frame: pd.DataFrame, version: DatasetVersion, analysis_type: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    pd = _require_pandas()
    _require_pandas()
    artifacts: list[dict[str, Any]] = []
    numeric_columns = [str(c) for c in frame.select_dtypes(include="number").columns]
    kind = str(analysis_type or "").strip().lower()
    engine = AnalysisEngine(dataset_version_id=version.id)
    field_mapping = dict(config.get("field_mapping") or {}) if isinstance(config.get("field_mapping"), dict) else {}

    enhanced_builder = _ENHANCED_ARTIFACT_BUILDERS.get(kind)
    if enhanced_builder is not None:
        return [enhanced_builder(frame, version, config)]

    # The user-facing API and Copilot tool registry share the same deterministic
    # implementation so an identical version/config produces the same evidence.
    engine_artifact = None
    if kind in {"trend", "time_series"}:
        trend_time_column = str(config.get("time_column") or config.get("event_time_column") or config.get("date_column") or "event_time")
        trend_frequency = str(config.get("frequency") or "").strip().upper()
        if not trend_frequency:
            # Batch 20: no explicit frequency -> choose from data density so
            # sparse tables stop producing walls of empty daily buckets.
            parsed_time = pd.to_datetime(frame[trend_time_column], errors="coerce", utc=True, format="mixed").dropna()
            span_days = (parsed_time.max() - parsed_time.min()).days if len(parsed_time) >= 2 else 0
            trend_frequency = choose_trend_frequency(span_days, len(frame))
        engine_artifact = engine.run_trend_analysis(
            frame,
            time_column=trend_time_column,
            metric_column=str(config.get("metric_column")),
            group_column=str(config["group_column"]) if config.get("group_column") else None,
            frequency=trend_frequency,
            aggregation=str(config.get("aggregation") or "mean"),
            field_mapping=field_mapping,
        )
    elif kind in {"funnel", "conversion"}:
        engine_artifact = engine.run_funnel_analysis(
            frame,
            user_id_column=str(config.get("user_id_column") or "user_id"),
            event_time_column=str(config.get("event_time_column") or "event_time"),
            event_name_column=str(config.get("event_name_column") or "event_name"),
            steps=[str(step) for step in config.get("steps") or []],
            window_hours=float(config.get("window_hours") or config.get("time_window_hours")) if config.get("window_hours") is not None or config.get("time_window_hours") is not None else None,
            field_mapping=field_mapping,
        )
    elif kind in {"retention", "retention_analysis"}:
        engine_artifact = engine.run_retention_analysis(
            frame,
            user_id_column=str(config.get("user_id_column") or "user_id"),
            event_time_column=str(config.get("event_time_column") or "event_time"),
            periods=[int(period) for period in config.get("periods") or [1, 7, 30]],
            cohort_granularity=str(config.get("cohort_granularity") or "day"),
            return_event_filter=dict(config.get("return_event_filter") or {}),
            field_mapping=field_mapping,
        )
    elif kind in {"anomaly", "anomalies"}:
        method = str(config.get("method") or "iqr").lower().replace("z_score", "zscore")
        engine_artifact = engine.run_anomaly_detection(
            frame,
            metric_column=str(config.get("metric_column")),
            time_column=str(config["time_column"]) if config.get("time_column") else None,
            method=method,
            threshold=float(config.get("threshold") or config.get("z_threshold") or 3),
            window=int(config.get("window") or 7),
            group_column=str(config["group_column"]) if config.get("group_column") else None,
        )
    elif kind == "group_comparison":
        engine_artifact = engine.run_group_comparison(
            frame,
            group_column=str(config.get("group_column")),
            value_column=str(config.get("value_column")),
            aggregation=str(config.get("aggregation") or "mean"),
            top_n=int(config.get("top_n") or 10),
            field_mapping=field_mapping,
        )
    if engine_artifact is not None:
        result = engine_artifact.to_dict()
        payload = dict(result["payload_json"])
        option: dict[str, Any] | None = None
        chart_type: str | None = None
        if kind in {"trend", "time_series"}:
            chart = dict(payload.get("chart") or {})
            option = {
                "tooltip": {"trigger": "axis"},
                "legend": {"type": "scroll", "bottom": 0},
                "grid": {"left": 48, "right": 20, "top": 24, "bottom": 52},
                "xAxis": chart.get("xAxis") or {"type": "time"},
                "yAxis": chart.get("yAxis") or {"type": "value"},
                "series": chart.get("series") or [],
            }
            chart_type = "line"
        elif kind in {"funnel", "conversion"}:
            chart = dict(payload.get("chart") or {})
            option = {
                "tooltip": {"trigger": "item", "formatter": "{b}: {c}"},
                "series": [{"type": "funnel", "left": "10%", "width": "80%", "label": {"show": True, "position": "inside"}, "data": chart.get("data") or []}],
            }
            chart_type = "funnel"
        elif kind in {"retention", "retention_analysis"}:
            rows = list(payload.get("cohort_results") or [])
            cohorts = sorted({str(row.get("cohort")) for row in rows})
            periods = sorted({int(row.get("period") or 0) for row in rows})
            values = [[periods.index(int(row.get("period") or 0)), cohorts.index(str(row.get("cohort"))), row.get("retention_rate")] for row in rows]
            option = {
                "tooltip": {"position": "top"},
                "grid": {"left": 92, "right": 28, "top": 20, "bottom": 52},
                "xAxis": {"type": "category", "data": [f"D{period}" for period in periods], "splitArea": {"show": True}},
                "yAxis": {"type": "category", "data": cohorts, "splitArea": {"show": True}},
                "visualMap": {"min": 0, "max": 1, "calculable": True, "orient": "horizontal", "left": "center", "bottom": 0},
                "series": [{"name": "Retention", "type": "heatmap", "data": values, "label": {"show": True, "formatter": "{@[2]}"}}],
            }
            chart_type = "heatmap"
        elif kind in {"anomaly", "anomalies"}:
            rows = list(payload.get("rows") or [])
            x_values = [row.get("timestamp") or row.get("index") for row in rows]
            option = {
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 48, "right": 20, "top": 24, "bottom": 44},
                "xAxis": {"type": "category", "data": x_values},
                "yAxis": {"type": "value"},
                "series": [
                    {"name": str(payload.get("metric_column") or "value"), "type": "line", "data": [row.get("value") for row in rows], "showSymbol": False},
                    {"name": "anomaly", "type": "scatter", "symbolSize": 10, "data": [[index, row.get("value")] for index, row in enumerate(rows) if row.get("is_anomaly")]},
                ],
            }
            chart_type = "line"
        elif kind == "group_comparison":
            rows = list(payload.get("categories") or [])
            option = {
                "tooltip": {"trigger": "axis", "valueFormatter": "{c}%"},
                "grid": {"left": 48, "right": 20, "top": 24, "bottom": 72},
                "xAxis": {"type": "category", "axisLabel": {"rotate": 28}, "data": [row.get("group") for row in rows]},
                "yAxis": {"type": "value", "axisLabel": {"formatter": "{value}%"}},
                "series": [
                    {
                        "name": "占比",
                        "type": "bar",
                        "data": [row.get("share") * 100 if row.get("share") is not None else 0 for row in rows],
                        "itemStyle": {"color": "#4a6cf7"},
                    }
                ],
            }
            chart_type = "bar"
        payload.update({"datasetVersionId": version.id, "configSnapshot": result["config_snapshot"], "chartType": chart_type, "title": result["title"], "option": option})
        return [{"artifact_type": result["artifact_type"], "title": result["title"], "payload_json": payload}]

    if analysis_type in {"eda", "descriptive", "overview"}:
        # Phase 1: EDA has one deterministic correlation implementation.  The
        # enhanced engine keeps the legacy ``correlations`` entries and appends
        # scalar detail under ``correlation_pairs_detail``; the service stores
        # that artifact unchanged so report narration and digest share it.
        eda_payload = engine.run_eda(
            frame,
            top_n=int(config.get("top_n") or 10),
            lineage_map=_lineage_for_version(frame, version),
        ).to_dict()["payload_json"]
        artifacts.append({"artifact_type": "table", "title": "EDA summary", "payload_json": eda_payload})
    elif analysis_type in {"group", "grouped", "segmentation", "group_analysis"}:
        group_column = config.get("group_column") or config.get("segment_column")
        metric_column = config.get("metric_column")
        if not group_column or group_column not in frame.columns:
            raise error("VALIDATION_ERROR", "Grouped analysis requires a group_column", 400)
        if metric_column and metric_column not in frame.columns:
            raise error("VALIDATION_ERROR", f"Missing metric column: {metric_column}", 400)
        aggregation = str(config.get("aggregation") or "count").lower()
        working = frame[[group_column] + ([metric_column] if metric_column else [])].copy()
        if metric_column:
            working[metric_column] = pd.to_numeric(working[metric_column], errors="coerce")
            grouped = working.groupby(group_column, dropna=False)[metric_column].agg(aggregation if aggregation in {"sum", "mean", "median", "min", "max"} else "mean").reset_index(name="value")
        else:
            grouped = working.groupby(group_column, dropna=False).size().reset_index(name="value")
        grouped[group_column] = grouped[group_column].astype(str)
        grouped = grouped.sort_values("value", ascending=False).head(int(config.get("top_n") or 20))
        artifacts.append({"artifact_type": "chart", "title": f"Grouped analysis by {group_column}", "payload_json": {"chartType": "bar", "title": f"Grouped analysis by {group_column}", "datasetVersionId": version.id, "option": {"xAxis": {"type": "category", "data": grouped[group_column].tolist()}, "yAxis": {"type": "value"}, "series": [{"name": metric_column or "count", "type": "bar", "data": [serialize(value) for value in grouped["value"].tolist()]}]}}})
        artifacts.append({"artifact_type": "table", "title": "Grouped values", "payload_json": {"rows": _json_records(grouped)}})
    elif analysis_type in {"health", "health_score"}:
        metrics: list[dict[str, Any]] = []
        for column in numeric_columns:
            values = pd.to_numeric(frame[column], errors="coerce").dropna()
            metrics.append({"metric": column, "value": float(values.mean()) if not values.empty else None, "sample_count": int(values.size)})
        artifacts.append({"artifact_type": "metric", "title": "AI product health overview", "payload_json": {"chartType": "bar", "title": "AI product health overview", "datasetVersionId": version.id, "configSnapshot": config, "metrics": metrics, "definition": "Deterministic summary of numeric fields; metric semantics come from the workspace dictionary.", "option": {"tooltip": {"trigger": "axis"}, "grid": {"left": 48, "right": 20, "top": 24, "bottom": 64}, "xAxis": {"type": "category", "axisLabel": {"rotate": 28}, "data": [item["metric"] for item in metrics]}, "yAxis": {"type": "value"}, "series": [{"name": "mean", "type": "bar", "data": [item["value"] for item in metrics]}]}}})
    else:
        artifacts.append({"artifact_type": "table", "title": "Sample data", "payload_json": {"rows": _json_records(frame.head(100))}})
    return artifacts


def _analysis_result_summary(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"artifact_count": len(artifacts)}
    for artifact in artifacts:
        payload = artifact.get("payload_json") or {}
        retention_rows = payload.get("cohort_results") or []
        if retention_rows:
            d7_rows = [row for row in retention_rows if int(row.get("period") or -1) == 7]
            if d7_rows:
                summary["d7_retention"] = sum(float(row.get("retention_rate") or 0) for row in d7_rows) / len(d7_rows)
            summary["max_cohort_size"] = max((int(row.get("cohort_size") or 0) for row in retention_rows), default=0)
        funnel_rows = payload.get("step_results") or []
        if funnel_rows:
            summary["max_dropoff_users"] = max((int(row.get("dropoff_users") or 0) for row in funnel_rows), default=0)
        if "anomaly_count" in payload:
            summary["anomaly_count"] = int(payload.get("anomaly_count") or 0)
    return summary


def _replace_version_columns(db: Session, version: DatasetVersion, schema: list[dict[str, Any]]) -> None:
    for column in list(version.columns):
        db.delete(column)
    db.flush()
    for item in schema:
        # Append through the relationship (not a bare session.add): a FK-only
        # add never updates the already-loaded ``version.columns`` cache, which
        # made every non-EDA auto analysis fail its own column validation
        # (batch 12 fix; the columns themselves were persisted correctly).
        column = DataColumn(dataset_version_id=version.id, **item)
        db.add(column)
        version.columns.append(column)


def _replace_quality_report(db: Session, version: DatasetVersion, score: float, quality_status: str, summary: dict[str, Any]) -> None:
    if version.quality_report is not None:
        db.delete(version.quality_report)
        db.flush()
    db.add(DataQualityReport(dataset_version_id=version.id, overall_score=score, status=quality_status, summary_json=summary))


def _analysis_quality_payload(frame: pd.DataFrame, artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """把本次自动分析的产物折算成双维度质量报告（``generate_quality_report``）。

    * ``corr_results`` 取 EDA 产物的 ``correlation_pairs_detail``（元素含
      ``robustness_note`` / ``is_significant``，兼容属性与字典两种访问方式）；
    * ``outstats_output`` 由 anomaly 产物的 ``anomaly_count`` 构造，没有 anomaly
      产物时传 ``{}``（``compute_analysis_quality_metrics`` 会据此判
      ``has_outlier_detection=False``）；
    * ``type_stats_info`` 来自 ``types.compute_type_aware_stats``。

    ``artifacts`` 为空说明自动分析没有真正跑过（最常见是 ``recover_pending``
    重放时 ``schema_auto_accepted_at`` 已置位）——此时返回 ``None``，调用方保持
    ``_quality_summary`` 写入的解析时点基线不动，避免用空输入把真实值覆盖回去。
    """

    if not artifacts:
        return None

    eda_payload: dict[str, Any] = {}
    anomaly_count = 0
    for artifact in artifacts:
        payload = artifact.get("payload_json") or {}
        if isinstance(payload, Mapping) and "correlation_pairs_detail" in payload:
            eda_payload = payload
        anomaly_count += int(payload.get("anomaly_count") or 0) if isinstance(payload, Mapping) else 0

    corr_results = eda_payload.get("correlation_pairs_detail") or []
    outstats_output = {"has_outliers": anomaly_count > 0, "total_count": anomaly_count} if anomaly_count else {}
    return generate_quality_report(frame, outstats_output, corr_results, compute_type_aware_stats(frame)).to_dict()


def _refresh_dual_quality(db: Session, version: DatasetVersion, frame: pd.DataFrame, artifacts: list[dict[str, Any]]) -> None:
    """解析 job 收尾：把「分析质量」真实值写进 ``summary_json["dual_quality"]``。

    只覆盖这一个键 —— ``overall_score`` / ``status`` / 其它键（含
    ``_quality_summary`` 写入的解析时点基线）一律不动，它们才是阶段 3 与
    analysis-run 门控的唯一来源。覆盖式而非追加式，因此 ``recover_pending`` 重放
    是幂等的。
    """

    payload = _analysis_quality_payload(frame, artifacts)
    if payload is None:
        return
    report = db.scalar(select(DataQualityReport).where(DataQualityReport.dataset_version_id == version.id))
    if report is None:
        return
    report.summary_json = {**(report.summary_json or {}), "dual_quality": payload}
    db.flush()


def _run_auto_analyses(
    db: Session,
    version: DatasetVersion,
    schema: list[dict[str, Any]],
    frame: pd.DataFrame,
    actor_id: str,
) -> dict[str, Any]:
    """Accept the inferred schema and run the auto-selected analyses inline.

    Called from the parse job with the frame already in memory, so nothing is
    re-read from disk.  Idempotent on ``schema_auto_accepted_at``: ``recover_pending``
    (app/infrastructure/jobs.py:134) re-runs the whole composite after a restart,
    and a second pass must not stack duplicate runs onto the same version.

    A failure here never fails the upload.  The parse result -- schema, columns,
    quality report -- is already committed-worthy at this point, and losing it
    because an optional convenience analysis raised would be a bad trade.
    """

    if version.schema_auto_accepted_at is not None:
        return {"run_ids": [], "plan": [], "skipped": [{"reason": "already_auto_accepted"}], "artifacts": []}
    if not actor_id:
        # ``AnalysisRun.requested_by`` is NOT NULL and FK-bound to users.id
        # (app/models.py:287); without a real actor there is no run to create.
        return {"run_ids": [], "plan": [], "skipped": [{"reason": "no_actor"}], "artifacts": []}

    project = version.dataset.project
    stamp = now()
    version.schema_auto_accepted_at = stamp

    plan = _auto_analysis_plan(schema)
    run_ids: list[str] = []
    skipped: list[dict[str, Any]] = []
    all_artifacts: list[dict[str, Any]] = []
    for entry in plan:
        kind = str(entry["analysis_type"])
        try:
            run, rejection = _prepare_analysis_run(
                db,
                project=project,
                version=version,
                analysis_type=kind,
                config=dict(entry["config"]),
                actor_id=actor_id,
            )
            if run is None:
                skipped.append({"analysis_type": kind, "reason": rejection.get("reason")})
                continue
            run.config_json = {
                **(run.config_json or {}),
                "auto_selected": True,
                "auto_reason": entry["reason"],
                "auto_columns": entry["columns"],
            }
            # Executed inline rather than via a queued ``analysis_run`` job: the
            # frame is already in memory here, and a separate job would re-read
            # the file and leave the run ``queued`` until it drained.
            run.status = "running"
            run.started_at = stamp
            artifacts = _analysis_artifacts(frame, version, kind, dict(run.config_json))
            for artifact in artifacts:
                db.add(
                    AnalysisArtifact(
                        analysis_run_id=run.id,
                        artifact_type=artifact["artifact_type"],
                        title=artifact["title"],
                        payload_json=artifact.get("payload_json") or {},
                        fingerprint=hashlib.sha256(
                            json.dumps(artifact.get("payload_json") or {}, ensure_ascii=False, sort_keys=True, default=str).encode()
                        ).hexdigest(),
                    )
                )
            run.status = "succeeded"
            run.completed_at = now()
            run.result_summary = _analysis_result_summary(artifacts)
            db.flush()
            run_ids.append(run.id)
            # 分析质量维度需要这些产物（correlation_pairs_detail / anomaly_count）。
            all_artifacts.extend(artifacts)
        except Exception as exc:  # noqa: BLE001 - an optional analysis must not sink a good parse
            skipped.append({"analysis_type": kind, "reason": "error", "detail": type(exc).__name__})
            continue

    audit(
        db,
        project.workspace_id,
        actor_id or None,
        "dataset.schema_auto_accepted",
        "dataset_version",
        version.id,
        {
            "analysis_run_ids": run_ids,
            "plan": [{"type": e["analysis_type"], "reason": e["reason"]} for e in plan],
            "skipped": skipped,
        },
    )
    return {"run_ids": run_ids, "plan": plan, "skipped": skipped, "artifacts": all_artifacts}


SUPPORTED_ANALYSIS_TYPES = {
    "eda", "descriptive", "overview", "trend", "time_series", "group", "grouped",
    "segmentation", "group_analysis", "group_comparison", "funnel", "conversion",
    "retention", "retention_analysis", "anomaly", "anomalies", "health", "health_score",
    # Phase 1: enhanced analysis types.  Deliberately **manual only** -- they are
    # not added to ``_auto_analysis_plan`` (which stays capped at four).
    "correlation_analysis", "type_profile", "outlier_objects",
}


FIELD_MAPPING_ALIASES = {
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


def _analysis_request_config(body: AnalysisCreate) -> dict[str, Any]:
    """Merge the top-level mapping into the persisted job configuration."""

    config = dict(body.config or {})
    nested_mapping = config.get("field_mapping")
    mapping = dict(nested_mapping) if isinstance(nested_mapping, dict) else None
    if body.field_mapping:
        mapping = {**(mapping or {}), **body.field_mapping}
    if mapping is not None:
        config["field_mapping"] = mapping
    return config


def _normalise_analysis_mapping(raw_mapping: Any) -> tuple[dict[str, str], list[str], list[str]]:
    if raw_mapping is None:
        return {}, [], []
    if not isinstance(raw_mapping, dict):
        return {}, [], ["field_mapping must be an object"]
    mapping: dict[str, str] = {}
    unknown_keys: list[str] = []
    errors: list[str] = []
    for raw_key, raw_value in raw_mapping.items():
        key = str(raw_key).strip().lower()
        canonical = FIELD_MAPPING_ALIASES.get(key)
        if canonical is None:
            unknown_keys.append(str(raw_key))
            continue
        value = str(raw_value).strip() if raw_value is not None else ""
        if not value:
            errors.append(f"field_mapping.{canonical} cannot be empty")
            continue
        mapping[canonical] = value
    if unknown_keys:
        errors.append(f"Unsupported field mapping keys: {', '.join(sorted(unknown_keys))}")
    duplicate_sources = sorted({source for source in mapping.values() if list(mapping.values()).count(source) > 1})
    for source in duplicate_sources:
        errors.append(f"Column '{source}' cannot be mapped to multiple semantic fields")
    return mapping, unknown_keys, errors


def _analysis_config_validation(version: DatasetVersion, analysis_type: str, config: dict[str, Any]) -> dict[str, Any]:
    columns = {column.name for column in version.columns}
    kind = str(analysis_type or "").strip().lower()
    errors: list[str] = []
    missing_mappings: list[str] = []
    invalid_mappings: dict[str, str] = {}
    mapping, unknown_mapping_keys, mapping_errors = _normalise_analysis_mapping(config.get("field_mapping"))
    errors.extend(mapping_errors)
    if kind not in SUPPORTED_ANALYSIS_TYPES:
        return {
            "errors": [f"Unsupported analysis type: {analysis_type}"],
            "field_mapping": mapping,
            "missing_mappings": missing_mappings,
            "invalid_mappings": invalid_mappings,
            "unknown_mapping_keys": unknown_mapping_keys,
            "mapping_errors": mapping_errors,
        }

    for canonical, source in mapping.items():
        if source not in columns:
            invalid_mappings[canonical] = source
            errors.append(f"Missing column: {source}")

    def require_column(key: str, *aliases: str) -> str | None:
        value = next((config.get(candidate) for candidate in (key, *aliases) if config.get(candidate)), None)
        if not value:
            errors.append(f"{key} is required")
            return None
        if str(value) not in columns:
            errors.append(f"Missing column: {value}")
            return None
        return str(value)

    def require_semantic(canonical: str, *config_keys: str) -> str | None:
        source = mapping.get(canonical)
        if source:
            return source if source in columns else None
        configured = next((config.get(key) for key in config_keys if config.get(key)), None)
        if configured:
            source = str(configured)
            if source not in columns:
                errors.append(f"Missing column: {source}")
                invalid_mappings[canonical] = source
                return None
            return source
        if canonical in columns:
            return canonical
        missing_mappings.append(canonical)
        errors.append(f"field_mapping.{canonical} is required")
        return None

    if kind in {"trend", "time_series"}:
        if mapping.get("event_time"):
            require_semantic("event_time", "time_column", "event_time_column", "date_column")
        else:
            require_column("time_column", "event_time_column", "date_column")
        require_column("metric_column")
    elif kind in {"group", "grouped", "segmentation", "group_analysis"}:
        require_column("group_column", "segment_column")
        if config.get("metric_column") and str(config["metric_column"]) not in columns:
            errors.append(f"Missing column: {config['metric_column']}")
    elif kind == "group_comparison":
        require_column("group_column")
        require_column("value_column")
        aggregation = str(config.get("aggregation") or "mean").lower()
        if aggregation not in {"sum", "mean"}:
            errors.append("aggregation must be sum or mean")
    elif kind in {"funnel", "conversion"}:
        require_semantic("user_id", "user_id_column")
        require_semantic("event_time", "event_time_column", "time_column")
        require_semantic("event_name", "event_name_column")
        steps = config.get("steps")
        if not isinstance(steps, list) or len(steps) < 2 or any(not str(step).strip() for step in steps):
            errors.append("steps must contain at least two event names")
    elif kind in {"retention", "retention_analysis"}:
        require_semantic("user_id", "user_id_column")
        require_semantic("event_time", "event_time_column", "time_column")
        periods = config.get("periods", [1, 7, 30])
        if not isinstance(periods, list) or not periods or any(not isinstance(period, int) or period < 0 for period in periods):
            errors.append("periods must contain non-negative integers")
    elif kind in {"anomaly", "anomalies"}:
        require_column("metric_column")
        if config.get("time_column") and str(config["time_column"]) not in columns:
            errors.append(f"Missing column: {config['time_column']}")
        method = str(config.get("method", "iqr")).lower()
        if method not in {"iqr", "zscore", "z_score", "rolling", "rolling_zscore"}:
            errors.append("method must be iqr, zscore or rolling")
    elif kind in {"health", "health_score"} and not columns:
        errors.append("health analysis requires a non-empty dataset")
    elif kind in {"correlation_analysis", "type_profile", "outlier_objects"}:
        # Phase 1 enhanced types need no required mapping: correlation_analysis and
        # outlier_objects fall back to every numeric column, type_profile covers all
        # columns.  ``columns`` is an optional whitelist, so only reject unknown names.
        requested = config.get("columns")
        if requested is not None:
            if not isinstance(requested, list) or any(not str(name).strip() for name in requested):
                errors.append("columns must be a list of column names")
            else:
                for name in requested:
                    if str(name) not in columns:
                        errors.append(f"Missing column: {name}")
        metric_column = config.get("metric_column")
        if metric_column and str(metric_column) not in columns:
            errors.append(f"Missing column: {metric_column}")
    # Preserve stable ordering for deterministic API responses and tests.
    return {
        "errors": list(dict.fromkeys(errors)),
        "field_mapping": mapping,
        "missing_mappings": list(dict.fromkeys(missing_mappings)),
        "invalid_mappings": invalid_mappings,
        "unknown_mapping_keys": unknown_mapping_keys,
        "mapping_errors": mapping_errors,
    }


def _field_mapping_error_message(analysis_type: str) -> str:
    kind = str(analysis_type or "").strip().lower()
    if kind in {"funnel", "conversion"}:
        return "请为漏斗分析指定用户ID、事件时间和事件名称字段"
    if kind in {"retention", "retention_analysis"}:
        return "请为留存分析指定用户ID和事件时间字段"
    return "Please provide the required field mappings for this analysis"


_AUTO_ANALYSIS_LIMIT = 4


def _auto_analysis_plan(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick up to four analyses from the inferred schema, recording *why*.

    Deterministic and inspectable by design.  Each entry carries a ``reason`` and
    the columns it chose so the report can disclose that the selection was
    automatic and offer a re-pick -- nothing downstream can distinguish an
    analyst's choice from a column-order accident unless we say so here.

    ``funnel`` is never auto-selected: ``run_funnel_analysis`` needs an ordered
    ``steps`` list that cannot be inferred, and guessing produces a plausible but
    wrong funnel.  Stage 4 remains the way to run one.  ``group_comparison``
    (batch 12) is the last pick: it turns any low-cardinality category column
    plus a numeric column into a persisted Pareto breakdown, which is what
    gives business tables (no event roles) real evidence material.
    """

    by_role = {
        str(column.get("mapping_role")): str(column["name"])
        for column in schema
        if column.get("mapping_role")
    }
    datetimes = [c for c in schema if str(c.get("inferred_type")) in {"datetime", "date"}]
    numerics = [c for c in schema if str(c.get("inferred_type")) in {"integer", "float"}]
    # A grouping column must actually group: high-cardinality strings (names,
    # free text, ids that escaped _IDENTIFIER_NAME) make a useless breakdown.
    categoricals = [
        c
        for c in schema
        if str(c.get("inferred_type")) == "string" and 0.0 < float(c.get("unique_ratio") or 1.0) <= 0.4
    ]
    numerics = sorted(numerics, key=lambda c: (bool(c.get("nullable")), int(c.get("ordinal") or 0)))

    plan: list[dict[str, Any]] = [
        {
            "analysis_type": "eda",
            "config": {},
            "reason": "always_included",
            "columns": [],
        }
    ]

    if by_role.get("user_id") and by_role.get("event_time"):
        plan.append(
            {
                "analysis_type": "retention",
                "config": {"field_mapping": {"user_id": by_role["user_id"], "event_time": by_role["event_time"]}},
                "reason": "user_id_and_event_time_roles_present",
                "columns": [by_role["user_id"], by_role["event_time"]],
            }
        )
    elif datetimes and numerics:
        time_column = by_role.get("event_time") or str(datetimes[0]["name"])
        metric_column = str(numerics[0]["name"])
        plan.append(
            {
                "analysis_type": "trend",
                "config": {"time_column": time_column, "metric_column": metric_column},
                "reason": "first_datetime_column_and_most_complete_numeric_column",
                "columns": [time_column, metric_column],
            }
        )
    elif categoricals and numerics:
        group_column = str(categoricals[0]["name"])
        metric_column = str(numerics[0]["name"])
        plan.append(
            {
                "analysis_type": "group",
                "config": {"group_column": group_column, "metric_column": metric_column},
                "reason": "low_cardinality_string_column_and_numeric_column",
                "columns": [group_column, metric_column],
            }
        )

    if len(plan) < _AUTO_ANALYSIS_LIMIT and numerics:
        plan.append(
            {
                "analysis_type": "anomaly",
                "config": {"metric_column": str(numerics[0]["name"])},
                "reason": "numeric_column_available_for_outlier_scan",
                "columns": [str(numerics[0]["name"])],
            }
        )

    if len(plan) < _AUTO_ANALYSIS_LIMIT and categoricals and numerics:
        group_column = str(categoricals[0]["name"])
        metric_column = str(numerics[0]["name"])
        plan.append(
            {
                "analysis_type": "group_comparison",
                "config": {"group_column": group_column, "value_column": metric_column, "aggregation": "mean"},
                "reason": "low_cardinality_string_column_and_numeric_column_for_pareto",
                "columns": [group_column, metric_column],
            }
        )

    return plan[:_AUTO_ANALYSIS_LIMIT]


def _prepare_analysis_run(
    db: Session,
    project: Project,
    version: DatasetVersion,
    analysis_type: str,
    config: dict[str, Any],
    actor_id: str | None,
) -> tuple[AnalysisRun | None, dict[str, Any]]:
    """Validate, degrade and create an ``AnalysisRun`` without any HTTP coupling.

    ``create_analysis`` is the HTTP wrapper around this; the automatic pipeline in
    ``_handle_dataset_parse`` calls it directly.  Preconditions are *returned* as a
    ``rejection`` mapping rather than raised, so an unattended run can skip one
    analysis type and still produce the others.  The returned run is flushed but
    not committed -- the caller owns the transaction.
    """

    resolved = dict(config)
    kind = analysis_type
    validation = _analysis_config_validation(version, kind, resolved)
    config_errors = validation["errors"]
    degraded_from: str | None = None
    if (
        config_errors
        and validation["missing_mappings"]
        and not validation["invalid_mappings"]
        and not validation["unknown_mapping_keys"]
        and not validation["mapping_errors"]
    ):
        degraded_from = kind
        resolved = {
            key: value
            for key, value in resolved.items()
            if key not in {"field_mapping", "steps", "periods", "metric_column", "time_column", "group_column"}
        }
        resolved["degraded_from"] = degraded_from
        resolved["degrade_reason"] = "missing_field_mapping"
        resolved["missing_mappings"] = validation["missing_mappings"]
        kind = "descriptive"
        validation = _analysis_config_validation(version, kind, resolved)
        config_errors = validation["errors"]
    if config_errors:
        return None, {
            "reason": "config",
            "analysis_type": kind,
            "validation": validation,
            "errors": config_errors,
        }
    if validation["field_mapping"]:
        resolved["field_mapping"] = validation["field_mapping"]
    if version.status not in {"ready", "confirmed"}:
        return None, {"reason": "not_ready", "analysis_type": kind, "validation": validation, "errors": []}
    if version.quality_report is not None and version.quality_report.status == "failed" and not bool(resolved.get("accept_quality_risk")):
        return None, {
            "reason": "quality",
            "analysis_type": kind,
            "validation": validation,
            "errors": [],
            "quality_status": version.quality_report.status,
        }
    run = AnalysisRun(
        workspace_id=project.workspace_id,
        project_id=project.id,
        dataset_version_id=version.id,
        analysis_type=kind,
        config_json=resolved,
        status="queued",
        requested_by=actor_id,
        result_summary={},
    )
    db.add(run)
    db.flush()
    return run, {
        "analysis_type": kind,
        "degraded_from": degraded_from,
        "field_mapping": validation["field_mapping"],
        "config": resolved,
    }
