"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  Database,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Save,
  Server,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { apiRequest } from "@/lib/api";
import {
  defaultSettings,
  errorText,
  formatNumber,
  getSettings,
  healthFromResponses,
  healthJson,
  numberValue,
  patchSettings,
  statusLabel,
  type HealthState,
  type Usage,
  type WorkspaceRole,
  type WorkspaceSettings,
} from "@/lib/settings";

export default function SettingsPage() {
  const [workspaceId, setWorkspaceId] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("viewer");
  const [settings, setSettings] = useState<WorkspaceSettings>(defaultSettings);
  const [usage, setUsage] = useState<Usage>({});
  const [health, setHealth] = useState<HealthState>({
    ai: "unknown",
    dataRoot: null,
    database: null,
    databaseFallback: null,
    databaseBackend: "unknown",
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const [meResult, aiResult, readyResult] = await Promise.allSettled([
      apiRequest<{
        user?: { name?: string };
        workspaces?: Array<{ id?: string; name?: string; role?: string }>;
      }>("/me"),
      healthJson("/health/ai"),
      healthJson("/health/ready"),
    ]);
    try {
      if (meResult.status !== "fulfilled") throw meResult.reason;
      const workspace = meResult.value.workspaces?.[0];
      if (!workspace?.id) throw new Error("当前账号没有可用工作空间");
      const id = workspace.id;
      setWorkspaceId(id);
      setWorkspaceName(workspace.name || "当前工作空间");
      setRole(workspace.role === "owner" || workspace.role === "editor" ? workspace.role : "viewer");
      const [settingsResult, usageResult] = await Promise.allSettled([
        getSettings(id),
        apiRequest<Usage>(`/ai/usage?workspace_id=${encodeURIComponent(id)}`),
      ]);
      if (settingsResult.status === "fulfilled") setSettings({ ...defaultSettings, ...settingsResult.value });
      if (usageResult.status === "fulfilled") setUsage(usageResult.value || {});
      if (settingsResult.status === "rejected" && usageResult.status === "rejected")
        throw settingsResult.reason;
    } catch (cause) {
      setError(errorText(cause, "设置加载失败"));
    } finally {
      const ai = aiResult.status === "fulfilled" ? aiResult.value : {};
      const ready = readyResult.status === "fulfilled" ? readyResult.value : {};
      setHealth(healthFromResponses(ai, ready));
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const update = (field: keyof WorkspaceSettings, value: string | number) => {
    setSettings((current) => ({ ...current, [field]: value }));
    setNotice("");
  };

  const save = async () => {
    if (!workspaceId || role !== "owner" || saving) return;
    setSaving(true);
    setError("");
    try {
      await patchSettings(workspaceId, {
        ai_model_id: settings.ai_model_id.trim() || "deepseek-chat",
        ai_max_output_tokens: Math.max(1, Math.round(settings.ai_max_output_tokens)),
        ai_per_request_token_budget: Math.max(1, Math.round(settings.ai_per_request_token_budget)),
        ai_daily_token_budget: Math.max(1, Math.round(settings.ai_daily_token_budget)),
      });
      setNotice("设置已保存");
      window.setTimeout(() => setNotice(""), 2600);
      await load();
    } catch (cause) {
      setError(errorText(cause, "设置保存失败"));
    } finally {
      setSaving(false);
    }
  };

  const totalTokens = numberValue(
    usage.total_tokens,
    numberValue(usage.prompt_tokens) + numberValue(usage.completion_tokens),
  );
  const dailyBudget = Math.max(1, numberValue(settings.ai_daily_token_budget, 1));
  const usagePercent = Math.min(100, Math.round((totalTokens / dailyBudget) * 100));
  const healthStatus = statusLabel(health);
  const featureRows = useMemo(
    () =>
      Object.entries(usage.by_feature || {}).sort(
        ([, left], [, right]) => numberValue(right.total_tokens) - numberValue(left.total_tokens),
      ),
    [usage.by_feature],
  );

  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <h1>设置</h1>
          <p>管理服务端模型配置、数据目录状态和 Token 用量。</p>
        </div>
        <button className="btn btn-subtle" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={14} className={loading ? "spin" : undefined} />
          刷新状态
        </button>
      </div>
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      {loading ? (
        <div className="card empty-state" style={{ minHeight: 260 }}>
          <LoaderCircle size={20} className="spin" />
          <strong>正在加载设置</strong>
          <p>读取服务端状态和当前工作空间预算。</p>
        </div>
      ) : (
        <>
          <section className="card card-pad" aria-labelledby="deepseek-title">
            <div className="card-head">
              <div>
                <h2 className="card-title" id="deepseek-title">
                  DeepSeek 配置
                </h2>
                <div className="card-kicker">密钥只在 API 服务端环境中读取，浏览器不会接触或回显。</div>
              </div>
              <span className={`tag ${healthStatus.tone}`}>{healthStatus.label}</span>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label htmlFor="settings-model">模型标识</label>
                <input
                  id="settings-model"
                  value={settings.ai_model_id}
                  onChange={(event) => update("ai_model_id", event.target.value)}
                  disabled={role !== "owner"}
                />
              </div>
              <div className="form-group">
                <label htmlFor="settings-output">单次最大输出 Token</label>
                <input
                  id="settings-output"
                  type="number"
                  min={1}
                  value={settings.ai_max_output_tokens}
                  onChange={(event) => update("ai_max_output_tokens", numberValue(event.target.value))}
                  disabled={role !== "owner"}
                />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label htmlFor="settings-request-budget">单次预算 Token</label>
                <input
                  id="settings-request-budget"
                  type="number"
                  min={1}
                  value={settings.ai_per_request_token_budget}
                  onChange={(event) => update("ai_per_request_token_budget", numberValue(event.target.value))}
                  disabled={role !== "owner"}
                />
              </div>
              <div className="form-group">
                <label htmlFor="settings-daily-budget">每日预算 Token</label>
                <input
                  id="settings-daily-budget"
                  type="number"
                  min={1}
                  value={settings.ai_daily_token_budget}
                  onChange={(event) => update("ai_daily_token_budget", numberValue(event.target.value))}
                  disabled={role !== "owner"}
                />
              </div>
            </div>
            <div className="form-hint">
              <KeyRound size={13} />
              <span>
                当前状态：
                {health.ai === "configured"
                  ? "服务端已配置模型密钥"
                  : health.ai === "not_configured"
                    ? "尚未配置模型密钥，前 3 步仍可正常使用"
                    : "暂时无法读取模型状态"}
                。当前角色：{role === "owner" ? "Owner，可编辑预算" : "只读"}。
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 15 }}>
              <button
                className="btn btn-primary"
                onClick={() => void save()}
                disabled={role !== "owner" || saving}
              >
                <Save size={14} />
                {saving ? "保存中…" : "保存配置"}
              </button>
            </div>
          </section>

          <div className="grid grid-2" style={{ marginTop: 15 }}>
            <section className="card card-pad" aria-labelledby="data-root-title">
              <div className="card-head">
                <div>
                  <h2 className="card-title" id="data-root-title">
                    数据目录
                  </h2>
                  <div className="card-kicker">文件路径由服务端 `DATA_ROOT` 管理，页面不接收本地路径。</div>
                </div>
                <Database size={17} color="#4968d6" />
              </div>
              <div className="list">
                <div className="list-row">
                  <div className="list-main">
                    <strong>目录可用性</strong>
                    <small>上传、处理和导出目录</small>
                  </div>
                  <span
                    className={`tag ${health.dataRoot === true ? "tag-green" : health.dataRoot === false ? "tag-rose" : "tag-slate"}`}
                  >
                    {health.dataRoot === true ? (
                      <>
                        <Check size={11} />
                        可用
                      </>
                    ) : health.dataRoot === false ? (
                      <>
                        <TriangleAlert size={11} />
                        不可用
                      </>
                    ) : (
                      "未检查"
                    )}
                  </span>
                </div>
                <div className="list-row">
                  <div className="list-main">
                    <strong>数据库</strong>
                    <small>{health.databaseBackend}</small>
                  </div>
                  <span
                    className={`tag ${health.databaseFallback === true ? "tag-rose" : health.database === true ? "tag-green" : "tag-slate"}`}
                  >
                    {health.databaseFallback === true
                      ? "SQLite fallback"
                      : health.database === true
                        ? "已连接"
                        : "未就绪"}
                  </span>
                </div>
              </div>
            </section>
            <section className="card card-pad" aria-labelledby="usage-title">
              <div className="card-head">
                <div>
                  <h2 className="card-title" id="usage-title">
                    Token 用量
                  </h2>
                  <div className="card-kicker">按当前工作空间统计已记录的模型调用。</div>
                </div>
                <Server size={17} color="#198b80" />
              </div>
              <div className="grid grid-2">
                <div>
                  <small className="metric-label">累计 Token</small>
                  <strong className="metric-value" style={{ display: "block" }}>
                    {formatNumber(totalTokens)}
                  </strong>
                </div>
                <div>
                  <small className="metric-label">调用次数</small>
                  <strong className="metric-value" style={{ display: "block" }}>
                    {formatNumber(usage.calls ?? usage.call_count)}
                  </strong>
                </div>
              </div>
              <div className="progress-wrap" style={{ marginTop: 16 }}>
                <div className="progress-line">
                  <span className="progress-label">每日预算</span>
                  <div className="progress-bar">
                    <div
                      className="progress-fill"
                      style={{
                        width: `${usagePercent}%`,
                        background: usagePercent >= 90 ? "#d46767" : undefined,
                      }}
                    />
                  </div>
                  <span className="progress-value">{usagePercent}%</span>
                </div>
                <small style={{ color: "var(--muted)", fontSize: 10 }}>
                  {formatNumber(totalTokens)} / {formatNumber(dailyBudget)} Token
                </small>
              </div>
            </section>
          </div>

          <section className="card card-pad" style={{ marginTop: 15 }} aria-labelledby="feature-usage-title">
            <div className="card-head">
              <div>
                <h2 className="card-title" id="feature-usage-title">
                  按功能用量
                </h2>
                <div className="card-kicker">用于定位高消耗调用；不会展示提示词或密钥。</div>
              </div>
              <ShieldCheck size={17} color="#4968d6" />
            </div>
            {featureRows.length ? (
              <div className="list">
                {featureRows.map(([feature, row]) => (
                  <div className="list-row" key={feature}>
                    <div className="list-main">
                      <strong>{feature}</strong>
                      <small>{formatNumber(row.calls)} 次调用</small>
                    </div>
                    <span className="tag tag-blue">{formatNumber(row.total_tokens)} Token</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-state" style={{ minHeight: 100 }}>
                <ShieldCheck size={18} />
                <strong>暂无用量记录</strong>
                <p>完成一次模型调用后，这里会显示聚合统计。</p>
              </div>
            )}
          </section>
        </>
      )}
      {notice && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
      <p style={{ marginTop: 16, color: "var(--muted)", fontSize: 10 }}>
        工作空间：{workspaceName || "-"} · 配置保存到服务端；不会写入浏览器存储。
      </p>
    </div>
  );
}
