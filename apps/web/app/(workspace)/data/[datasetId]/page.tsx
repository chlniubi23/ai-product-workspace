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
  ShieldAlert,
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
  source?: string;
  // Batch 21: LLM field-semantics dictionary (NULL when not interpreted).
  semantic_label?: string | null;
  semantic_description?: string | null;
};
type ExtractionReportRow = { source_column?: string; metric?: string; coverage?: number };
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
  schema_json?: { text_metric_extraction?: ExtractionReportRow[] };
};
type Dataset = { id: string; name: string; project_id?: string; versions?: Version[] };
type PreviewData = { columns: Column[]; rows: Record<string, unknown>[]; total: number };

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

export default function DatasetDetailPage({ params }: { params: { datasetId: string } }) {
  const [remote, setRemote] = useState<Dataset | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [activeTab, setActiveTab] = useState("dictionary");
  const [versionId, setVersionId] = useState<string>();
  const [preview, setPreview] = useState<PreviewData | null>(null);
  const [quality, setQuality] = useState<QualityReport | null>(null);
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
  // Batch 14: coverage per derived column ("{source}__{metric}") from the
  // extraction report persisted at parse time.
  const coverageByColumn = useMemo(() => {
    const rows = selected?.schema_json?.text_metric_extraction || [];
    return Object.fromEntries(
      rows
        .filter((row) => row.source_column && row.metric)
        .map((row) => [`${row.source_column}__${row.metric}`, row.coverage ?? 0]),
    );
  }, [selected?.schema_json]);
  useEffect(() => {
    if (!selected?.id || !accessToken()) return;
    setQuality(selectedReport || null);
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
              进入质量评估
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
          字段字典 <span style={{ color: "var(--faint)" }}>{columns.length}</span>
        </button>
        <button
          className={`tab ${activeTab === "preview" ? "active" : ""}`}
          onClick={() => setActiveTab("preview")}
        >
          数据预览
        </button>
      </div>
      {activeTab === "dictionary" && (
        <Dictionary
          columns={columns}
          confirmedRequiredRoles={confirmedRequiredRoles}
          rolesComplete={rolesComplete}
          coverageByColumn={coverageByColumn}
        />
      )}
      {activeTab === "preview" && <Preview data={preview} fallback={summary?.sample} />}
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
  confirmedRequiredRoles,
  rolesComplete,
  coverageByColumn,
}: {
  columns: Column[];
  confirmedRequiredRoles: number;
  rolesComplete: boolean;
  coverageByColumn: Record<string, number>;
}) {
  return (
    <section className="card table-wrap" style={{ marginTop: 15 }}>
      <div className="card-head" style={{ padding: "15px 17px 0" }}>
        <div>
          <h2 className="card-title">数据字典</h2>
          <div className="card-kicker">字段类型与角色由解析引擎自动推断（上传即确认），此处只读展示。</div>
        </div>
        <span className={`tag ${rolesComplete ? "tag-green" : "tag-amber"}`}>
          {confirmedRequiredRoles}/3 个必需角色
        </span>
      </div>
      {!rolesComplete && (
        <div className="form-hint" style={{ margin: "12px 17px 0" }}>
          <ShieldAlert size={13} />
          <span>缺少用户 ID、事件时间或事件名称时，分析模板会按可用字段自动选择维度。</span>
        </div>
      )}
      {rolesComplete && (
        <div className="form-hint" style={{ margin: "12px 17px 0", justifyContent: "space-between" }}>
          <span>
            <Check size={13} />
            必需字段角色已确认，可以开始质量评估。
          </span>
          <Link className="btn btn-primary btn-sm" href="/data">
            <ArrowRight size={13} />
            进入质量评估
          </Link>
        </div>
      )}
      <table className="data-table">
        <thead>
          <tr>
            <th>字段</th>
            <th>类型</th>
            <th>字段角色</th>
            <th>可空</th>
            <th>唯一率</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {columns.map((column) => {
            const role = roleOptions.find(([value]) => value === (column.mapping_role || ""));
            return (
              <tr key={column.id || column.name}>
                <td>
                  <strong>{column.display_name || column.name}</strong>
                  <small style={{ display: "block", color: "var(--faint)", fontSize: 10, marginTop: 3 }}>
                    {column.name}
                  </small>
                  {column.semantic_description && (
                    <small style={{ display: "block", color: "var(--muted)", fontSize: 10, marginTop: 3 }}>
                      {column.semantic_description}
                    </small>
                  )}
                </td>
                <td>
                  <span className="tag tag-slate">
                    {column.confirmed_type || column.inferred_type || "unknown"}
                  </span>
                  {column.semantic_label && (
                    <span className="tag tag-blue" style={{ marginLeft: 4 }}>
                      {column.semantic_label}
                    </span>
                  )}
                  {column.source === "extracted" && (
                    <small style={{ display: "block", color: "var(--faint)", fontSize: 10, marginTop: 3 }}>
                      抽取列
                      {coverageByColumn[column.name] !== undefined
                        ? ` · 覆盖率 ${Math.round(coverageByColumn[column.name] * 100)}%`
                        : ""}
                    </small>
                  )}
                </td>
                <td>{role?.[1] || "未指定"}</td>
                <td>
                  {column.nullable ? (
                    <span className="tag tag-amber">是</span>
                  ) : (
                    <span className="tag tag-green">否</span>
                  )}
                </td>
                <td>
                  {typeof column.unique_ratio === "number"
                    ? `${Math.round(column.unique_ratio * 100)}%`
                    : "-"}
                </td>
                <td>
                  <MoreHorizontal size={15} color="var(--faint)" />
                </td>
              </tr>
            );
          })}
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
