/**
 * Adapts backend analysis chart payloads into echarts options.
 *
 * The engine emits four shapes (engine.py): `line` is already echarts-ready,
 * while `funnel`, `retention` and `line_with_anomalies` carry raw rows that the
 * frontend has to lay out. Returns null when a payload is unrecognised so the
 * caller can fall back to its table view.
 */

type ChartPayload = Record<string, unknown>;

// Batch 22 palette: single brand blue leads, then restrained categorical
// supports; axes/split lines follow the neutral tokens (#e4e4e7 / #71717a).
const AXIS_LABEL = { color: "#71717a", fontSize: 11 };
const SPLIT_LINE = { lineStyle: { color: "#e4e4e7" } };
const PALETTE = ["#2563eb", "#0891b2", "#d97706", "#dc2626", "#64748b"];
const LEGEND_LABEL = { color: "#71717a", fontSize: 12 };

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
    tooltip: { trigger: "item", formatter: "{b}: {c} 人" },
    series: [
      {
        type: "funnel",
        name: title,
        left: "8%",
        right: "8%",
        top: 24,
        bottom: 24,
        minSize: "18%",
        sort: "none",
        gap: 3,
        label: { position: "inside", formatter: "{b} · {c}", color: "#fff", fontSize: 12 },
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
    smooth: true,
    showSymbol: rows.length <= 60,
    symbolSize: 5,
    itemStyle: { color: PALETTE[index % PALETTE.length] },
    data: periods.map((period) => {
      const match = rows.find((row) => String(row.cohort) === cohort && String(row.period) === period);
      const rate = match ? num(match.retention_rate) : null;
      return rate === null ? null : Number((rate * 100).toFixed(2));
    }),
  }));

  return {
    color: PALETTE,
    tooltip: {
      trigger: "axis",
      valueFormatter: (value: unknown) => (num(value) === null ? "—" : `${num(value)}%`),
    },
    legend: { type: "scroll", top: 0, textStyle: LEGEND_LABEL },
    grid: baseGrid({ top: 40 }),
    xAxis: {
      type: "category",
      data: periods,
      name: "周期",
      nameTextStyle: AXIS_LABEL,
      axisLabel: AXIS_LABEL,
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
    tooltip: { trigger: "axis" },
    legend: { top: 0, textStyle: LEGEND_LABEL, data: ["指标", "异常点"] },
    grid: baseGrid({ top: 40 }),
    xAxis: { type: "category", data: points.map((point) => point[0]), axisLabel: AXIS_LABEL },
    yAxis: { type: "value", nameTextStyle: AXIS_LABEL, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    series: [
      {
        name: "指标",
        type: "line",
        smooth: true,
        showSymbol: false,
        itemStyle: { color: PALETTE[0] },
        data: points.map((point) => point[1]),
      },
      { name: "异常点", type: "scatter", symbolSize: 11, itemStyle: { color: "#dc2626" }, data: flagged },
    ],
  };
}

function lineOption(chart: ChartPayload) {
  const series = rowsOf(chart, "series");
  if (!series.length) return null;
  return {
    color: PALETTE,
    tooltip: { trigger: "axis" },
    legend: { type: "scroll", top: 0, textStyle: LEGEND_LABEL },
    grid: baseGrid({ top: 40 }),
    xAxis: { ...(chart.xAxis as object), axisLabel: AXIS_LABEL },
    yAxis: { ...(chart.yAxis as object), axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    series: series.map((entry, index) => ({
      smooth: true,
      showSymbol: false,
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
    tooltip: { trigger: "axis", valueFormatter: (value: unknown) => `${num(value) ?? 0}%` },
    grid: baseGrid({ top: 34, bottom: 72 }),
    xAxis: {
      type: "category",
      data: data.map((row) => row.name),
      name: title,
      nameTextStyle: AXIS_LABEL,
      axisLabel: { ...AXIS_LABEL, rotate: 28 },
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
        barMaxWidth: 42,
        itemStyle: { color: PALETTE[0], borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: "top", formatter: "{c}%", color: "#71717a", fontSize: 11 },
        data: data.map((row) => row.value),
      },
    ],
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
    case "line":
      return lineOption(shape);
    default:
      return null;
  }
}
