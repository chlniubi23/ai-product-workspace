/**
 * Adapts backend analysis chart payloads into echarts options.
 *
 * The engine emits four shapes (engine.py): `line` is already echarts-ready,
 * while `funnel`, `retention` and `line_with_anomalies` carry raw rows that the
 * frontend has to lay out. Returns null when a payload is unrecognised so the
 * caller can fall back to its table view.
 */

import * as echarts from "echarts";

type ChartPayload = Record<string, unknown>;

/** Batch 27 enhanced palette with stronger brand and professional gradients */
const AXIS_LABEL = { color: "#64748b", fontSize: 11 };
const AXIS_LINE = { lineStyle: { color: "#e2e8f0" } };
const SPLIT_LINE = { lineStyle: { color: "#f1f5f9", type: "dashed" } };
const PALETTE = ["#6366f1", "#0891b2", "#f59e0b", "#ef4444", "#10b981"];
const BRAND_COLOR = PALETTE[0];
const BRAND_LIGHT = "#818cf8";
const BRAND_AREA = {
  color: {
    type: "linear",
    x: 0,
    y: 0,
    x2: 0,
    y2: 1,
    colorStops: [
      { offset: 0, color: "rgba(99, 102, 241, 0.15)" },
      { offset: 1, color: "rgba(99, 102, 241, 0)" },
    ],
  },
};

/** Shared tooltip card: white with elevated shadow */
function tooltip(trigger: "item" | "axis", extra: Record<string, unknown> = {}) {
  return {
    trigger,
    backgroundColor: "#ffffff",
    borderColor: "#e2e8f0",
    borderWidth: 1,
    borderRadius: 8,
    padding: [10, 12],
    textStyle: { color: "#0f172a", fontSize: 12 },
    extraCssText: "box-shadow: 0 8px 24px rgba(15,23,42,0.1);",
    ...extra,
  };
}

/** One global motion contract with enhanced easing */
const ANIMATION = { animationDuration: 500, animationEasing: "cubicOut" } as const;
const LEGEND_LABEL = { color: "#64748b", fontSize: 12 };

function rowsOf(chart: ChartPayload, key: string): Record<string, unknown>[] {
  const value = chart[key];
  return Array.isArray(value) ? (value as Record<string, unknown>[]) : [];
}

function num(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value)))
    return Number(value);
  return null;
}

function baseGrid(extra?: Record<string, unknown>) {
  return { left: 52, right: 20, top: 34, bottom: 40, containLabel: true, ...extra };
}

function funnelOption(chart: ChartPayload, title: string) {
  const data = rowsOf(chart, "data")
    .map((row) => ({ name: String(row.name ?? ""), value: num(row.value) ?? 0 }))
    .filter((row) => row.name !== "");
  if (!data.length) return null;
  return {
    color: PALETTE,
    ...ANIMATION,
    tooltip: tooltip("item", { formatter: "{b}: {c}人" }),
    series: [
      {
        type: "funnel",
        name: title,
        left: "8%",
        right: "8%",
        top: 24,
        bottom: 24,
        minSize: "15%",
        maxSize: "100%",
        sort: "none",
        gap: 4,
        label: { position: "inside", formatter: "{b} · {c}", color: "#fff", fontSize: 12 },
        labelLayout: { horizontalAlign: "center", verticalAlign: "middle" },
        data,
      },
    ],
  };
}

function retentionOption(chart: ChartPayload) {
  const rows = rowsOf(chart, "rows");
  if (!rows.length) return null;
  const periods = Array.from(new Set(rows.map((row) => String(row.period ?? "")))).filter(Boolean);
  const cohorts = Array.from(new Set(rows.map((row) => String(row.cohort ?? "")))).filter(Boolean);
  if (!periods.length || !cohorts.length) return null;

  const series = cohorts.map((cohort, index) => ({
    name: cohort,
    type: "line",
    smooth: 0.4,
    symbol: "circle",
    symbolSize: 5,
    itemStyle: { color: PALETTE[index % PALETTE.length], borderWidth: 1 },
    data: periods.map((period) => {
      const match = rows.find((row) => String(row.cohort) === cohort && String(row.period) === period);
      const rate = match ? num(match.retention_rate) : null;
      return rate === null ? null : Number((rate * 100).toFixed(2));
    }),
  }));

  return {
    color: PALETTE,
    ...ANIMATION,
    tooltip: tooltip("axis", {
      valueFormatter: (value: unknown) => (num(value) === null ? "—" : `${num(value)}%`),
    }),
    legend: { type: "scroll", top: 0, textStyle: LEGEND_LABEL },
    grid: baseGrid({ top: 40 }),
    xAxis: {
      type: "category",
      data: periods,
      name: "周期",
      nameTextStyle: AXIS_LABEL,
      axisLabel: AXIS_LABEL,
      axisLine: AXIS_LINE,
    },
    yAxis: {
      type: "value",
      name: "留存率 %",
      max: 100,
      nameTextStyle: AXIS_LABEL,
      axisLabel: { ...AXIS_LABEL, formatter: "{value}%" },
      splitLine: SPLIT_LINE,
    },
    series,
  };
}

function anomalyOption(chart: ChartPayload) {
  const rows = rowsOf(chart, "rows");
  if (!rows.length) return null;
  const timeKey = ["time", "period", "date", "timestamp"].find((key) => key in rows[0]);
  const valueKey = ["value", "metric", "metric_value"].find((key) => key in rows[0]);
  if (!timeKey || !valueKey) return null;

  const points = rows.map((row) => [String(row[timeKey] ?? ""), num(row[valueKey])]);
  const flagged = rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => row.is_anomaly === true || row.anomaly === true)
    .map(({ row }) => [String(row[timeKey] ?? ""), num(row[valueKey])]);

  return {
    color: PALETTE,
    ...ANIMATION,
    tooltip: tooltip("axis"),
    legend: { top: 0, textStyle: LEGEND_LABEL, data: ["指标", "异常点"] },
    grid: baseGrid({ top: 40 }),
    xAxis: {
      type: "category",
      data: points.map((point) => point[0]),
      axisLabel: AXIS_LABEL,
      axisLine: AXIS_LINE,
    },
    yAxis: { type: "value", nameTextStyle: AXIS_LABEL, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    series: [
      {
        name: "指标",
        type: "line",
        smooth: 0.4,
        symbol: "circle",
        symbolSize: 6,
        showSymbol: true,
        itemStyle: { color: BRAND_COLOR, borderWidth: 2 },
        areaStyle: BRAND_AREA,
        data: points.map((point) => point[1]),
      },
      { 
        name: "异常点", 
        type: "scatter", 
        symbolSize: 12, 
        itemStyle: { color: "#ef4444", borderWidth: 2, borderColor: "#fff" },
        data: flagged 
      },
    ],
  };
}

function lineOption(chart: ChartPayload) {
  const series = rowsOf(chart, "series");
  if (!series.length) return null;
  return {
    color: PALETTE,
    ...ANIMATION,
    tooltip: tooltip("axis"),
    legend: { type: "scroll", top: 0, textStyle: LEGEND_LABEL },
    grid: baseGrid({ top: 40 }),
    xAxis: { ...(chart.xAxis as object), axisLabel: AXIS_LABEL, axisLine: AXIS_LINE },
    yAxis: { ...(chart.yAxis as object), axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    series: series.map((entry, index) => ({
      // A solo series gets visible dots + the brand area gradient; multi
      // series stays dotless so crossing lines do not turn into noise.
      smooth: 0.3,
      symbol: "circle",
      symbolSize: 4,
      showSymbol: series.length === 1,
      areaStyle: series.length === 1 ? BRAND_AREA : undefined,
      itemStyle: { color: PALETTE[index % PALETTE.length] },
      ...entry,
    })),
  };
}

function barOption(chart: ChartPayload, title: string) {
  // group_comparison (batch 12) emits category rows as {name, value} where
  // value is the share percentage; a missing or empty list falls back to the
  // caller's table view.
  const data = rowsOf(chart, "data")
    .map((row) => ({ name: String(row.name ?? ""), value: num(row.value) ?? 0 }))
    .filter((row) => row.name !== "");
  if (!data.length) return null;
  return {
    color: PALETTE,
    ...ANIMATION,
    tooltip: tooltip("axis", { valueFormatter: (value: unknown) => `${num(value) ?? 0}%` }),
    grid: baseGrid({ top: 34, bottom: 68 }),
    xAxis: {
      type: "category",
      data: data.map((row) => row.name),
      name: title,
      nameTextStyle: AXIS_LABEL,
      axisLabel: { ...AXIS_LABEL, rotate: 30 },
      axisLine: AXIS_LINE,
    },
    yAxis: {
      type: "value",
      name: "占比 %",
      nameTextStyle: AXIS_LABEL,
      axisLabel: { ...AXIS_LABEL, formatter: "{value}%" },
      splitLine: SPLIT_LINE,
    },
    series: [
      {
        name: title || "占比",
        type: "bar",
        barMaxWidth: 48,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: BRAND_COLOR },
            { offset: 1, color: BRAND_LIGHT },
          ]),
          borderRadius: [6, 6, 0, 0],
        },
        label: {
          show: true,
          position: "top",
          formatter: "{c}%",
          color: "#64748b",
          fontSize: 11,
          fontFamily: "var(--font-mono), Menlo, monospace",
        },
        data: data.map((row) => row.value),
      },
    ],
  };
}

function countBarOption(chart: ChartPayload, title: string) {
  const data = rowsOf(chart, "data")
    .map((row) => ({ name: String(row.name ?? ""), value: num(row.value) ?? 0 }))
    .filter((row) => row.name !== "");
  if (!data.length) return null;
  const unit = String(chart.unit ?? "");
  return {
    color: PALETTE,
    ...ANIMATION,
    tooltip: tooltip("axis", { valueFormatter: (value: unknown) => `${num(value) ?? 0}${unit}` }),
    grid: baseGrid({ top: 34, bottom: 68 }),
    xAxis: {
      type: "category",
      data: data.map((row) => row.name),
      name: title,
      nameTextStyle: AXIS_LABEL,
      axisLabel: { ...AXIS_LABEL, rotate: 28 },
      axisLine: AXIS_LINE,
    },
    yAxis: { type: "value", name: unit, nameTextStyle: AXIS_LABEL, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    series: [{
      name: title,
      type: "bar",
      barMaxWidth: 48,
      itemStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: BRAND_COLOR },
          { offset: 1, color: BRAND_LIGHT },
        ]),
        borderRadius: [6, 6, 0, 0],
      },
      data: data.map((row) => row.value),
    }],
  };
}

function correlationHeatmapOption(chart: ChartPayload) {
  const labels = Array.isArray(chart.labels) ? chart.labels.map(String) : [];
  const data = Array.isArray(chart.data) ? chart.data : [];
  if (!labels.length || !data.length) return null;
  return {
    ...ANIMATION,
    tooltip: tooltip("item", {
      formatter: (params: unknown) => {
        if (!params || typeof params !== "object") return "";
        const item = params as { value?: unknown };
        const values = Array.isArray(item.value) ? item.value : [];
        const x = Number(values[0]);
        const y = Number(values[1]);
        const value = num(values[2]);
        return `${labels[x] ?? ""} × ${labels[y] ?? ""}<br/>Pearson r: ${value ?? "—"}`;
      },
    }),
    grid: { left: 104, right: 24, top: 20, bottom: 64 },
    xAxis: { type: "category", data: labels, axisLabel: AXIS_LABEL, axisLine: AXIS_LINE },
    yAxis: { type: "category", data: labels, axisLabel: AXIS_LABEL, axisLine: AXIS_LINE },
    visualMap: { min: -1, max: 1, calculable: true, orient: "horizontal", left: "center", bottom: 0 },
    series: [{ name: "Pearson r", type: "heatmap", data, label: { show: true, formatter: "{@[2]}" } }],
  };
}

/** Build an echarts option from an artifact payload, or null if not chartable. */
export function toChartOption(payload: unknown, title = ""): Record<string, unknown> | null {
  if (!payload || typeof payload !== "object") return null;
  const chart = (payload as ChartPayload).chart;
  if (!chart || typeof chart !== "object") return null;
  const shape = chart as ChartPayload;

  switch (String(shape.type ?? "")) {
    case "funnel":
      return funnelOption(shape, title);
    case "retention":
      return retentionOption(shape);
    case "line_with_anomalies":
      return anomalyOption(shape);
    case "bar":
      return barOption(shape, title);
    case "count_bar":
      return countBarOption(shape, title);
    case "correlation_heatmap":
      return correlationHeatmapOption(shape);
    case "line":
      return lineOption(shape);
    default:
      return null;
  }
}
