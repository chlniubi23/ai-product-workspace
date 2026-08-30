/** Shared date/number formatting helpers. */

export function formatDateTime(value?: string | null): string {
  if (!value) return "暂无时间";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(timestamp);
}

export function formatNumber(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString("zh-CN") : value.toFixed(2);
  if (typeof value === "string") return value;
  return "—";
}

export function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
