import { API_BASE, apiRequest } from "@/lib/api";

export type WorkspaceRole = "owner" | "editor" | "viewer";

export type WorkspaceSettings = {
  timezone: string;
  data_retention_days: number | null;
  analysis_threshold: number;
  ai_model_id: string;
  ai_max_output_tokens: number;
  ai_per_request_token_budget: number;
  ai_daily_token_budget: number;
  feature_flags?: Record<string, boolean>;
};

export type Usage = {
  calls?: number;
  call_count?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  by_feature?: Record<string, { calls?: number; total_tokens?: number }>;
};

type SettingsResponse = Partial<WorkspaceSettings> & { settings?: Partial<WorkspaceSettings> };

export type HealthState = {
  ai: "configured" | "not_configured" | "unknown";
  dataRoot: boolean | null;
  database: boolean | null;
  databaseFallback: boolean | null;
  databaseBackend: string;
};

export const defaultSettings: WorkspaceSettings = {
  timezone: "Asia/Shanghai",
  data_retention_days: 90,
  analysis_threshold: 3,
  ai_model_id: "deepseek-chat",
  ai_max_output_tokens: 4096,
  ai_per_request_token_budget: 16000,
  ai_daily_token_budget: 200000,
};

const apiOrigin = API_BASE.replace(/\/api\/v1\/?$/, "");

export function numberValue(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function formatNumber(value: unknown): string {
  return new Intl.NumberFormat("zh-CN").format(numberValue(value));
}

export function errorText(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback;
}

function settingsFromResponse(payload: SettingsResponse): Partial<WorkspaceSettings> {
  if (!payload || typeof payload !== "object") return {};
  return payload.settings && typeof payload.settings === "object" ? payload.settings : payload;
}

export async function getSettings(workspaceId: string): Promise<Partial<WorkspaceSettings>> {
  try {
    const payload = await apiRequest<SettingsResponse>(
      `/settings?workspace_id=${encodeURIComponent(workspaceId)}`,
    );
    return settingsFromResponse(payload);
  } catch {
    const payload = await apiRequest<SettingsResponse>(`/workspaces/${workspaceId}/settings`);
    return settingsFromResponse(payload);
  }
}

export async function patchSettings(workspaceId: string, body: Record<string, unknown>): Promise<void> {
  try {
    await apiRequest(`/settings?workspace_id=${encodeURIComponent(workspaceId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  } catch {
    await apiRequest(`/workspaces/${workspaceId}/settings`, { method: "PATCH", body: JSON.stringify(body) });
  }
}

export async function healthJson(path: string): Promise<Record<string, unknown>> {
  try {
    const response = await fetch(`${apiOrigin}${path}`, { cache: "no-store" });
    const body = await response.json().catch(() => ({}));
    if (!body || typeof body !== "object") return {};
    const record = body as Record<string, unknown>;
    const payload = record.data && typeof record.data === "object" ? record.data : record;
    if (response.ok) return payload as Record<string, unknown>;
    const detail = record.detail || record.error;
    return detail && typeof detail === "object"
      ? (detail as Record<string, unknown>)
      : (payload as Record<string, unknown>);
  } catch {
    return {};
  }
}

export function healthFromResponses(
  ai: Record<string, unknown>,
  ready: Record<string, unknown>,
): HealthState {
  const checks =
    ready.checks && typeof ready.checks === "object" ? (ready.checks as Record<string, unknown>) : {};
  const booleanValue = (...values: unknown[]) =>
    values.find((value): value is boolean => typeof value === "boolean") ?? null;
  const stringValue = (...values: unknown[]) =>
    values.find((value): value is string => typeof value === "string") || "unknown";
  return {
    ai:
      ai.status === "configured"
        ? "configured"
        : ai.status === "not_configured"
          ? "not_configured"
          : "unknown",
    dataRoot: booleanValue(checks.data_root, checks.dataRoot),
    database: booleanValue(checks.database),
    databaseFallback: booleanValue(checks.database_fallback, checks.databaseFallback),
    databaseBackend: stringValue(checks.database_backend, checks.databaseBackend),
  };
}

export function statusLabel(health: HealthState): { label: string; tone: string } {
  if (health.ai === "configured") return { label: "已配置", tone: "tag-green" };
  if (health.ai === "not_configured") return { label: "未配置", tone: "tag-amber" };
  return { label: "无法检查", tone: "tag-slate" };
}
