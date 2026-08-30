"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Database,
  History,
  MoreHorizontal,
  Save,
  ShieldAlert,
  SlidersHorizontal,
  Table2,
  Trash2,
} from "lucide-react";
import { accessToken, apiRequest } from "@/lib/api";

type Column = {
  id?: string;
  name: string;
  display_name?: string;
  inferred_type?: string;
  confirmed_type?: string;
  mapping_role?: string;
  nullable?: boolean;
  unique_ratio?: number;
};
type QualitySummary = {
  missing_values?: Record<string, number>;
  duplicate_rows?: number;
  type_errors?: Record<string, number>;
  anomalies?: Record<string, number>;
  sample?: Record<string, unknown>[];
  summary_json?: QualitySummary;
};
type QualityReport = QualitySummary & { overall_score?: number; status?: string };
type Version = {
  id: string;
  version_number: number;
  file_name?: string;
  row_count?: number;
  column_count?: number;
  status?: string;
  created_at?: string;
  parent_version_id?: string;
  columns?: Column[];
  quality_report?: QualityReport;
};
type Dataset = { id: string; name: string; project_id?: string; versions?: Version[] };
type PreviewData = { columns: Column[]; rows: Record<string, unknown>[]; total: number };
type CleaningSummary = {
  estimated_deleted_rows?: number;
  affected_fields?: string[];
  sample_before?: Record<string, unknown>[];
  sample_after?: Record<string, unknown>[];
  risks?: string[];
};

const typeOptions = ["string", "integer", "float", "boolean", "datetime", "category"];
const roleOptions = [
  ["", "未指定"],
  ["user_id", "用户 ID"],
  ["event_time", "事件时间"],
  ["event_name", "事件名称"],
  ["group", "分组字段"],
  ["metric", "指标字段"],
  ["text", "文本字段"],
] as const;
const requiredRoles = ["user_id", "event_time", "event_name"] as const;

function editableType(value?: string) {
  if (value === "number" || value === "numeric") return "float";
  if (value === "categorical") return "category";
  return typeOptions.includes(value || "") ? value! : "string";
}

export default function DatasetDetailPage({ params }: { params: { datasetId: string } }) {
  const [remote, setRemote] = useState<Dataset | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [activeTab, setActiveTab] = useState("dictionary");
  const [versionId, setVersionId] = useState<string>();
  const [preview, setPreview] = useState<PreviewData | null>(null);
  const [quality, setQuality] = useState<QualityReport | null>(null);
  const [cleaning, setCleaning] = useState<CleaningSummary | null>(null);
  const [operation, setOperation] = useState("drop_duplicates");
  const [operationColumn, setOperationColumn] = useState("");
  const [operationValue, setOperationValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const load = useCallback(async () => {
    if (!accessToken()) return;
    try {
      const data = await apiRequest<Dataset>(`/datasets/${params.datasetId}`);
      setRemote(data);
      const rows = [...(data.versions || [])].sort((a, b) => a.version_number - b.version_number);
      setVersionId((current) =>
        current && rows.some((row) => row.id === current) ? current : rows[rows.length - 1]?.id,
      );
      setLoadError("");
    } catch (cause) {
      setRemote(null);
      setVersionId(undefined);
      setLoadError(cause instanceof Error ? cause.message : "数据集加载失败");
    } finally {
      setLoading(false);
    }
  }, [params.datasetId]);
  useEffect(() => {
    const token = accessToken();
    if (token) void load();
    else setLoading(false);
  }, [load]);
  useEffect(() => {
    const current = remote?.versions?.find((version) => version.id === versionId) || remote?.versions?.at(-1);
    if (!current || current.status !== "processing" || !accessToken()) return;
    const timer = window.setInterval(() => {
      void load();
    }, 1200);
    return () => window.clearInterval(timer);
  }, [load, remote, versionId]);
  const versions = useMemo(
    () => [...(remote?.versions || [])].sort((a, b) => a.version_number - b.version_number),
    [remote],
  );
  const selected = versions.find((version) => version.id === versionId) || versions[versions.length - 1];
  const score = Math.round(quality?.overall_score ?? selected?.quality_report?.overall_score ?? 0);
  const columns = selected?.columns?.length ? selected.columns : [];
  const summary = (quality?.summary_json ||
    quality ||
    selected?.quality_report?.summary_json ||
    selected?.quality_report) as QualitySummary | undefined;
  const qualityStatus = quality?.status || selected?.quality_report?.status;
  const risk = qualityStatus ? qualityStatus !== "passed" : score < 95;
  const confirmedRoles = new Set(
    columns.map((column) => column.mapping_role).filter((role): role is string => Boolean(role)),
  );
  const confirmedRequiredRoles = requiredRoles.filter((role) => confirmedRoles.has(role)).length;
  const rolesComplete = Boolean(selected?.id && confirmedRequiredRoles === requiredRoles.length);
  const selectedReport = selected?.quality_report;
  useEffect(() => {
    if (!selected?.id || !accessToken()) return;
    setQuality(selectedReport || null);
    if (activeTab === "quality")
      void apiRequest<QualityReport>(`/dataset-versions/${selected.id}/quality-report`)
        .then(setQuality)
        .catch(() => undefined);
    if (activeTab === "preview")
      void apiRequest<PreviewData>(`/dataset-versions/${selected.id}/preview?page_size=12`)
        .then(setPreview)
        .catch(() => setPreview(null));
  }, [activeTab, selected?.id, selectedReport]);
  const notify = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 2400);
  };
  const selectVersion = (id: string) => {
    setVersionId(id);
    setPreview(null);
    setCleaning(null);
  };
  const saveColumn = async (column: Column, field: "mapping_role" | "confirmed_type", value: string) => {
    if (!selected?.id || !accessToken()) {
      notify("当前数据集没有可编辑版本");
      return;
    }
    try {
      await apiRequest(`/dataset-versions/${selected.id}/schema`, {
        method: "PATCH",
        body: JSON.stringify({ columns: [{ id: column.id, name: column.name, [field]: value || null }] }),
      });
      setRemote((current) =>
        current
          ? {
              ...current,
              versions: current.versions?.map((version) =>
                version.id === selected.id
                  ? {
                      ...version,
                      columns: version.columns?.map((item) =>
                        (item.id && column.id ? item.id === column.id : item.name === column.name)
                          ? { ...item, [field]: value || undefined }
                          : item,
                      ),
                    }
                  : version,
              ),
            }
          : current,
      );
      notify("字段定义已保存");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "字段保存失败");
    }
  };
  const operations = () => {
    if (operation === "drop_duplicates") return [{ operation }];
    if (!operationColumn) return [];
    if (operation === "fill_missing")
      return [{ operation, columns: [operationColumn], value: operationValue }];
    if (operation === "coerce_type")
      return [{ operation, column: operationColumn, target_type: operationValue || "numeric" }];
    return [{ operation, columns: [operationColumn] }];
  };
  const previewCleaning = async () => {
    if (!selected?.id || !accessToken()) {
      notify("登录后可预览清洗规则");
      return;
    }
    const body = operations();
    if (!body.length) {
      notify("请选择受影响的字段");
      return;
    }
    setBusy(true);
    try {
      setCleaning(
        await apiRequest<CleaningSummary>(`/dataset-versions/${selected.id}/cleaning-preview`, {
          method: "POST",
          body: JSON.stringify({ operations: body }),
        }),
      );
      notify("清洗预览已生成");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "清洗预览失败");
    } finally {
      setBusy(false);
    }
  };
  const applyCleaning = async () => {
    if (!selected?.id || !accessToken()) {
      notify("登录后可应用清洗规则");
      return;
    }
    const body = operations();
    if (!body.length) {
      notify("请选择受影响的字段");
      return;
    }
    setBusy(true);
    try {
      const result = await apiRequest<{ dataset_version?: Version }>(
        `/dataset-versions/${selected.id}/cleaning-operations`,
        { method: "POST", body: JSON.stringify({ operations: body }) },
      );
      if (result.dataset_version?.id) setVersionId(result.dataset_version.id);
      await load();
      setCleaning(null);
      setActiveTab("versions");
      notify("清洗完成，已创建新的不可变版本");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "清洗应用失败");
    } finally {
      setBusy(false);
    }
  };
  const removeDataset = async () => {
    if (!accessToken()) {
      notify("登录后可归档数据集");
      return;
    }
    if (!window.confirm("确定要归档这个数据集吗？原始版本不会被直接修改。")) return;
    try {
      await apiRequest(`/datasets/${params.datasetId}?confirm=${encodeURIComponent(params.datasetId)}`, {
        method: "DELETE",
      });
      window.location.href = "/data";
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "数据集归档失败");
    }
  };
  const displayName = remote?.name || (loading ? "正在加载数据集" : "数据集不可用");
  const versionDescription = selected
    ? `${selected.file_name || remote?.name || "数据版本"} · v${selected.version_number} · ${(selected.row_count ?? 0).toLocaleString()} 行 · ${selected.column_count ?? 0} 列`
    : "尚无可用数据版本";
  if (!loading && loadError && !remote) {
    return (
      <div className="page">
        <div style={{ marginBottom: 19 }}>
          <Link href="/data" className="btn btn-subtle btn-sm">
            <ArrowLeft size={13} />
            返回数据中心
          </Link>
        </div>
        <div className="card empty-state" role="status">
          <ShieldAlert size={19} />
          <strong>无法打开数据集</strong>
          <p>{loadError}</p>
        </div>
      </div>
    );
  }
  return (
    <div className="page">
      <div style={{ marginBottom: 19 }}>
        <Link href="/data" className="btn btn-subtle btn-sm">
          <ArrowLeft size={13} />
          返回数据中心
        </Link>
      </div>
      <div className="page-heading">
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 8 }}>
            <span className="bullet-icon" style={{ width: 31, height: 31 }}>
              <Database size={16} />
            </span>
            <span className={`tag ${risk ? "tag-amber" : "tag-green"}`}>
              {loading
                ? "加载中"
                : qualityStatus === "passed"
                  ? "可分析"
                  : qualityStatus === "needs_review"
                    ? "有风险"
                    : qualityStatus === "failed"
                      ? "待确认"
                      : selected?.status || (risk ? "待确认" : "可分析")}
            </span>
          </div>
          <h1>{displayName}</h1>
          <p>{versionDescription} · 数据版本不可变</p>
        </div>
        <div className="heading-actions">
          <button className="btn" onClick={() => setActiveTab("preview")} disabled={!selected}>
            <Table2 size={15} />
            预览数据
          </button>
          {rolesComplete && (
            <Link className="btn btn-primary" href="/data">
              <ArrowRight size={15} />
              进入体检
            </Link>
          )}
          <button className="btn" onClick={removeDataset} disabled={!remote}>
            <Trash2 size={15} />
            归档
          </button>
        </div>
      </div>
      <div
        className="card card-pad"
        style={{ marginBottom: 17, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}
      >
        <div>
          <div className="card-kicker">当前版本</div>
          <select
            aria-label="选择数据版本"
            value={selected?.id || ""}
            onChange={(event) => selectVersion(event.target.value)}
            disabled={!versions.length}
          >
            <option value="">{versions.length ? "选择版本" : "暂无版本"}</option>
            {versions.map((version) => (
              <option value={version.id} key={version.id}>
                v{version.version_number} · {version.file_name || "数据版本"}
              </option>
            ))}
          </select>
        </div>
        <div className="version-meta">
          <History size={14} />
          {selected?.created_at ? new Date(selected.created_at).toLocaleString("zh-CN") : "暂无版本"}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <span className={`tag ${score >= 95 ? "tag-green" : "tag-amber"}`}>质量 {score}/100</span>
          <span className="tag tag-slate">{selected?.status || (loading ? "loading" : "unavailable")}</span>
        </div>
      </div>
      <div className="tabs" style={{ width: "fit-content" }}>
        <button
          className={`tab ${activeTab === "dictionary" ? "active" : ""}`}
          onClick={() => setActiveTab("dictionary")}
        >
          字段字典 <span style={{ color: "#9aa6b7" }}>{columns.length}</span>
        </button>
        <button
          className={`tab ${activeTab === "preview" ? "active" : ""}`}
          onClick={() => setActiveTab("preview")}
        >
          数据预览
        </button>
        <button
          className={`tab ${activeTab === "quality" ? "active" : ""}`}
          onClick={() => setActiveTab("quality")}
        >
          质量报告
        </button>
        <button
          className={`tab ${activeTab === "versions" ? "active" : ""}`}
          onClick={() => setActiveTab("versions")}
        >
          版本记录 <span style={{ color: "#9aa6b7" }}>{versions.length}</span>
        </button>
        <button
          className={`tab ${activeTab === "cleaning" ? "active" : ""}`}
          onClick={() => setActiveTab("cleaning")}
        >
          清洗操作
        </button>
      </div>
      {activeTab === "dictionary" && (
        <Dictionary
          columns={columns}
          onSave={saveColumn}
          confirmedRequiredRoles={confirmedRequiredRoles}
          rolesComplete={rolesComplete}
        />
      )}
      {activeTab === "preview" && <Preview data={preview} fallback={summary?.sample} />}
      {activeTab === "quality" && <Quality summary={summary} score={score} />}
      {activeTab === "versions" && (
        <Versions rows={versions} selected={selected?.id} onSelect={selectVersion} />
      )}
      {activeTab === "cleaning" && (
        <Cleaning
          columns={columns}
          operation={operation}
          setOperation={setOperation}
          column={operationColumn}
          setColumn={setOperationColumn}
          value={operationValue}
          setValue={setOperationValue}
          preview={cleaning}
          busy={busy}
          onPreview={previewCleaning}
          onApply={applyCleaning}
        />
      )}
      {notice && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
    </div>
  );
}
function Dictionary({
  columns,
  onSave,
  confirmedRequiredRoles,
  rolesComplete,
}: {
  columns: Column[];
  onSave: (column: Column, field: "mapping_role" | "confirmed_type", value: string) => void;
  confirmedRequiredRoles: number;
  rolesComplete: boolean;
}) {
  return (
    <section className="card table-wrap" style={{ marginTop: 15 }}>
      <div className="card-head" style={{ padding: "15px 17px 0" }}>
        <div>
          <h2 className="card-title">数据字典</h2>
          <div className="card-kicker">确认类型和字段角色后，分析模板会自动复用这些映射。</div>
        </div>
        <span className={`tag ${rolesComplete ? "tag-green" : "tag-amber"}`}>
          {confirmedRequiredRoles}/3 个必需角色
        </span>
      </div>
      {!rolesComplete && (
        <div className="form-hint" style={{ margin: "12px 17px 0" }}>
          <ShieldAlert size={13} />
          <span>请确认用户 ID、事件时间和事件名称后进入体检。</span>
        </div>
      )}
      {rolesComplete && (
        <div className="form-hint" style={{ margin: "12px 17px 0", justifyContent: "space-between" }}>
          <span>
            <Check size={13} />
            必需字段角色已确认，可以开始体检。
          </span>
          <Link className="btn btn-primary btn-sm" href="/data">
            <ArrowRight size={13} />
            进入体检
          </Link>
        </div>
      )}
      <table className="data-table">
        <thead>
          <tr>
            <th>字段</th>
            <th>推断类型</th>
            <th>确认类型</th>
            <th>字段角色</th>
            <th>可空</th>
            <th>唯一率</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {columns.map((column) => (
            <tr key={column.id || column.name}>
              <td>
                <strong>{column.display_name || column.name}</strong>
                <small style={{ display: "block", color: "#8793a5", fontSize: 9, marginTop: 3 }}>
                  {column.name}
                </small>
              </td>
              <td>
                <span className="tag tag-slate">{column.inferred_type || "unknown"}</span>
              </td>
              <td>
                <select
                  aria-label={`${column.name}确认类型`}
                  value={editableType(column.confirmed_type || column.inferred_type)}
                  onChange={(event) => onSave(column, "confirmed_type", event.target.value)}
                >
                  {typeOptions.map((value) => (
                    <option value={value} key={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <select
                  aria-label={`${column.name}字段角色`}
                  value={column.mapping_role || ""}
                  onChange={(event) => onSave(column, "mapping_role", event.target.value)}
                >
                  {roleOptions.map(([value, label]) => (
                    <option value={value} key={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                {column.nullable ? (
                  <span className="tag tag-amber">是</span>
                ) : (
                  <span className="tag tag-green">否</span>
                )}
              </td>
              <td>
                {typeof column.unique_ratio === "number" ? `${Math.round(column.unique_ratio * 100)}%` : "-"}
              </td>
              <td>
                <MoreHorizontal size={15} color="#a0aaba" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!columns.length && (
        <div className="empty-state" style={{ minHeight: 150 }}>
          <Table2 size={18} />
          <strong>暂无字段定义</strong>
          <p>当前数据集版本没有可用字段。</p>
        </div>
      )}
    </section>
  );
}
function Preview({ data, fallback }: { data: PreviewData | null; fallback?: Record<string, unknown>[] }) {
  const rows = data?.rows || fallback || [];
  const columns: Column[] = data?.columns || (rows[0] ? Object.keys(rows[0]).map((name) => ({ name })) : []);
  return (
    <section className="card table-wrap" style={{ marginTop: 15 }}>
      <div className="card-head" style={{ padding: "15px 17px 0" }}>
        <div>
          <h2 className="card-title">数据预览</h2>
          <div className="card-kicker">仅展示当前版本的分页样本，不会修改原始文件。</div>
        </div>
        <span className="tag tag-slate">{data ? `${data.total.toLocaleString()} 行` : "等待服务端样本"}</span>
      </div>
      {rows.length ? (
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.name}>{column.display_name || column.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td key={column.name}>{String(row[column.name] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="empty-state" style={{ minHeight: 180 }}>
          <Table2 size={19} />
          <strong>暂无预览样本</strong>
          <p>当前版本没有可展示样本，或预览请求尚未完成。</p>
        </div>
      )}
    </section>
  );
}
function Quality({ summary, score }: { summary?: QualitySummary; score: number }) {
  const report = summary || {};
  const missing: Record<string, number> = report.missing_values || report.summary_json?.missing_values || {};
  const duplicate = report.duplicate_rows ?? report.summary_json?.duplicate_rows ?? 0;
  const typeErrors: Record<string, number> = report.type_errors || report.summary_json?.type_errors || {};
  const anomalies: Record<string, number> = report.anomalies || report.summary_json?.anomalies || {};
  const items = [
    { label: "缺失值", value: Object.values(missing).reduce((sum, value) => sum + value, 0) },
    { label: "重复行", value: duplicate },
    { label: "类型错误", value: Object.values(typeErrors).reduce((sum, value) => sum + value, 0) },
    { label: "异常值", value: Object.values(anomalies).reduce((sum, value) => sum + value, 0) },
  ];
  return (
    <section className="grid grid-2" style={{ marginTop: 15 }}>
      <div className="card card-pad">
        <div className="card-head">
          <div>
            <h2 className="card-title">质量报告</h2>
            <div className="card-kicker">由服务端 Pandas 规则计算，可在清洗前复核。</div>
          </div>
          <span className={`tag ${score >= 95 ? "tag-green" : "tag-amber"}`}>{score}/100</span>
        </div>
        <div className="list">
          {items.map((item) => (
            <div className="list-row" key={item.label}>
              <div className="list-main">
                <strong>{item.label}</strong>
                <small>当前版本检测结果</small>
              </div>
              <span className={`tag ${item.value ? "tag-amber" : "tag-green"}`}>{item.value}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="card card-pad">
        <div className="empty-state" style={{ minHeight: 170 }}>
          <ShieldAlert size={19} />
          <strong>{score >= 95 ? "质量达标，可进入分析" : "存在风险，等待人工确认"}</strong>
          <p>原始版本不可修改，应用清洗会创建带父版本的新版本。</p>
        </div>
      </div>
    </section>
  );
}
function Versions({
  rows,
  selected,
  onSelect,
}: {
  rows: Version[];
  selected?: string;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="card table-wrap" style={{ marginTop: 15 }}>
      <div className="card-head" style={{ padding: "15px 17px 0" }}>
        <div>
          <h2 className="card-title">版本记录</h2>
          <div className="card-kicker">每次上传或清洗都会生成不可变版本，可回溯父版本。</div>
        </div>
        <span className="tag tag-blue">{rows.length || 1} 个版本</span>
      </div>
      {rows.length ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>版本</th>
              <th>文件</th>
              <th>规模</th>
              <th>状态</th>
              <th>来源</th>
              <th>创建时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((version) => (
              <tr key={version.id}>
                <td>
                  <strong>v{version.version_number}</strong>
                </td>
                <td>{version.file_name || "-"}</td>
                <td>
                  {version.row_count?.toLocaleString() || 0} 行 · {version.column_count || 0} 列
                </td>
                <td>
                  <span
                    className={`tag ${version.status === "ready" || version.status === "confirmed" ? "tag-green" : "tag-amber"}`}
                  >
                    {version.status || "unknown"}
                  </span>
                </td>
                <td>
                  {version.parent_version_id ? `基于 ${version.parent_version_id.slice(0, 8)}` : "原始上传"}
                </td>
                <td>{version.created_at ? new Date(version.created_at).toLocaleString("zh-CN") : "-"}</td>
                <td>
                  <button className="btn btn-subtle btn-sm" onClick={() => onSelect(version.id)}>
                    {selected === version.id ? (
                      <>
                        <Check size={12} />
                        当前
                      </>
                    ) : (
                      "查看"
                    )}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="empty-state" style={{ minHeight: 160 }}>
          <History size={19} />
          <strong>暂无服务端版本</strong>
          <p>登录并上传数据后，版本记录会显示在这里。</p>
        </div>
      )}
    </section>
  );
}
function Cleaning({
  columns,
  operation,
  setOperation,
  column,
  setColumn,
  value,
  setValue,
  preview,
  busy,
  onPreview,
  onApply,
}: {
  columns: Column[];
  operation: string;
  setOperation: (value: string) => void;
  column: string;
  setColumn: (value: string) => void;
  value: string;
  setValue: (value: string) => void;
  preview: CleaningSummary | null;
  busy: boolean;
  onPreview: () => void;
  onApply: () => void;
}) {
  const requiresColumn = operation !== "drop_duplicates";
  return (
    <div style={{ marginTop: 15 }}>
      <section className="card card-pad">
        <div className="card-head">
          <div>
            <h2 className="card-title">清洗操作</h2>
            <div className="card-kicker">先预览影响，再应用并创建新版本。原始版本保持不变。</div>
          </div>
          <SlidersHorizontal size={17} color="#4968d6" />
        </div>
        <div className="form-row">
          <div className="form-group">
            <label htmlFor="cleaning-operation">操作</label>
            <select
              id="cleaning-operation"
              value={operation}
              onChange={(event) => setOperation(event.target.value)}
            >
              <option value="drop_duplicates">删除重复行</option>
              <option value="drop_missing">删除字段缺失行</option>
              <option value="fill_missing">填充字段缺失值</option>
              <option value="coerce_type">转换字段类型</option>
              <option value="strip_strings">清理字符串空格</option>
            </select>
          </div>
          <div className="form-group">
            <label htmlFor="cleaning-column">字段</label>
            <select
              id="cleaning-column"
              value={column}
              onChange={(event) => setColumn(event.target.value)}
              disabled={!requiresColumn}
            >
              <option value="">{requiresColumn ? "选择字段" : "全部字段"}</option>
              {columns.map((item) => (
                <option value={item.name} key={item.name}>
                  {item.display_name || item.name}
                </option>
              ))}
            </select>
          </div>
          {(operation === "fill_missing" || operation === "coerce_type") && (
            <div className="form-group">
              <label htmlFor="cleaning-value">{operation === "fill_missing" ? "填充值" : "目标类型"}</label>
              {operation === "coerce_type" ? (
                <select id="cleaning-value" value={value} onChange={(event) => setValue(event.target.value)}>
                  <option value="numeric">numeric</option>
                  <option value="datetime">datetime</option>
                  <option value="boolean">boolean</option>
                  <option value="categorical">categorical</option>
                </select>
              ) : (
                <input
                  id="cleaning-value"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  placeholder="例如：unknown"
                />
              )}
            </div>
          )}
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
          <button className="btn" disabled={busy} onClick={onPreview}>
            <Table2 size={14} />
            {busy ? "处理中…" : "预览影响"}
          </button>
          <button className="btn btn-primary" disabled={busy || !preview} onClick={onApply}>
            <Save size={14} />
            应用并创建版本
          </button>
        </div>
      </section>
      {preview && (
        <section className="card card-pad" style={{ marginTop: 15 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">预览结果</h2>
              <div className="card-kicker">服务端返回预计删除行数、影响字段和样本变化。</div>
            </div>
            <span className={`tag ${preview.risks?.length ? "tag-amber" : "tag-green"}`}>
              {preview.estimated_deleted_rows || 0} 行将被删除
            </span>
          </div>
          <div className="grid grid-3">
            <div>
              <small className="metric-label">影响字段</small>
              <strong>{preview.affected_fields?.join("、") || "-"}</strong>
            </div>
            <div>
              <small className="metric-label">风险提示</small>
              <strong>{preview.risks?.join("；") || "未发现"}</strong>
            </div>
            <div>
              <small className="metric-label">样本对比</small>
              <strong>{preview.sample_after?.length || 0} 行</strong>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
