"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import {
  Check,
  Database,
  FileSpreadsheet,
  FileUp,
  MoreHorizontal,
  Search,
  Target,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { accessToken, apiRequest } from "@/lib/api";
import { getActiveProjectId } from "@/lib/workflow";
import { UPLOAD_ACCEPT_ATTR, UPLOAD_FORMAT_HINT } from "@/lib/upload";

type DatasetVersion = {
  version_number?: number;
  row_count?: number;
  column_count?: number;
  created_at?: string;
  status?: string;
  quality_report?: { overall_score?: number; status?: string };
};
type UploadResult = { dataset?: { id?: string }; id?: string };
type DatasetRow = {
  id: string;
  name: string;
  project: string;
  rows: string;
  columns: number;
  version: string;
  quality: number;
  status: string;
  updated: string;
  kind: string;
  projectId?: string;
  versionCount: number;
  versions: DatasetVersion[];
};

function toDatasetRow(
  dataset: { id: string; name: string; project_id?: string; versions?: DatasetVersion[] },
  projectNames: Record<string, string>,
): DatasetRow {
  const versions = [...(dataset.versions || [])].sort(
    (a, b) => (a.version_number || 0) - (b.version_number || 0),
  );
  const version = versions[versions.length - 1];
  const quality = Math.round(version?.quality_report?.overall_score ?? 0);
  const reportStatus = version?.quality_report?.status;
  return {
    id: dataset.id,
    name: dataset.name,
    project: projectNames[dataset.project_id || ""] || "当前项目",
    rows: (version?.row_count ?? 0).toLocaleString(),
    columns: version?.column_count ?? 0,
    version: `v${version?.version_number ?? 1}`,
    quality,
    status:
      reportStatus === "passed" || (!reportStatus && quality >= 95)
        ? "可分析"
        : reportStatus === "needs_review" || (!reportStatus && quality >= 80)
          ? "有风险"
          : "待确认",
    updated: version?.created_at ? new Date(version.created_at).toLocaleDateString("zh-CN") : "刚刚",
    kind: "上传数据",
    projectId: dataset.project_id,
    versionCount: versions.length,
    versions,
  };
}

export default function DataPage() {
  const router = useRouter();
  const [items, setItems] = useState<DatasetRow[]>([]);
  const [activeProject, setActiveProject] = useState<{
    id: string;
    name: string;
    goal_statement?: string;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [showUpload, setShowUpload] = useState(false);
  const [file, setFile] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");
  const loadDatasets = async () => {
    if (!accessToken()) return;
    setLoadError("");
    try {
      const [rows, projects] = await Promise.all([
        apiRequest<Array<{ id: string; name: string; project_id?: string; versions?: DatasetVersion[] }>>(
          "/datasets",
        ),
        apiRequest<Array<{ id: string; name: string; goal_statement?: string }>>("/projects"),
      ]);
      // 上传绑定当前活跃项目（batch 11：项目切换统一在工作台完成）；
      // 失效的持久化 id 回退到第一个项目，与快照的解析规则一致。
      const storedId = getActiveProjectId();
      setActiveProject(projects.find((project) => project.id === storedId) || projects[0] || null);
      const names = Object.fromEntries(projects.map((project) => [project.id, project.name]));
      setItems(rows.map((row) => toDatasetRow(row, names)));
    } catch (cause) {
      setItems([]);
      setLoadError(cause instanceof Error ? cause.message : "数据集加载失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    const token = accessToken();
    if (token) {
      void loadDatasets();
    } else {
      setLoading(false);
    }
    // 项目切换（工作台）后数据页跟随刷新。
    const handler = () => void loadDatasets();
    window.addEventListener("apw-project-changed", handler);
    return () => window.removeEventListener("apw-project-changed", handler);
  }, []);
  const upload = async () => {
    if (!selectedFile) return;
    try {
      if (!activeProject?.id) throw new Error("请先到工作台创建并选择一个项目");
      const body = new FormData();
      body.append("project_id", activeProject.id);
      body.append("dataset_name", selectedFile.name.replace(/\.[^.]+$/, ""));
      body.append("file", selectedFile);
      const result = await apiRequest<UploadResult>("/datasets/upload", { method: "POST", body });
      setShowUpload(false);
      setSelectedFile(null);
      setFile(null);
      const datasetId = result.dataset?.id || result.id;
      if (datasetId) {
        // Continue in the dataset detail view so the user can confirm roles
        // before the workflow advances to the quality check.
        router.push(`/data/${datasetId}`);
        return;
      }
      await loadDatasets();
      setNotice("数据集已上传，正在等待解析");
      window.setTimeout(() => setNotice(""), 2200);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "上传失败");
    }
  };
  const filtered = items.filter((dataset) => dataset.name.includes(query) || dataset.project.includes(query));
  const versionCount = useMemo(
    () => items.reduce((total, item) => total + (item.versionCount || 0), 0),
    [items],
  );
  const analyzableCount = useMemo(
    () =>
      items.reduce(
        (total, item) =>
          total +
          (item.versions.length
            ? item.versions.filter(
                (version) =>
                  version.quality_report?.status === "passed" ||
                  (!version.quality_report?.status && (version.quality_report?.overall_score || 0) >= 95),
              ).length
            : item.status === "可分析"
              ? item.versionCount
              : 0),
        0,
      ),
    [items],
  );
  const monthImports = useMemo(() => {
    const start = new Date();
    start.setDate(1);
    start.setHours(0, 0, 0, 0);
    return items.reduce(
      (total, item) =>
        total +
        item.versions.filter((version) => version.created_at && new Date(version.created_at) >= start).length,
      0,
    );
  }, [items]);
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <h1>接数据</h1>
          <p>先确定要回答的问题，再上传 CSV/XLSX——字段类型由解析引擎自动推断。</p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowUpload(true)}>
          <UploadCloud size={15} />
          上传数据
        </button>
      </div>
      <section className="card card-pad" style={{ marginBottom: 17, borderColor: "var(--brand-border)" }}>
        <div className="card-head">
          <div>
            <h2 className="card-title">项目上下文</h2>
            <div className="card-kicker">
              数据、分析和后续解读都会沿用这个目标问题；切换或新建项目请到工作台。
            </div>
          </div>
          <Target size={17} color="var(--faint)" />
        </div>
        {activeProject ? (
          <div>
            <strong>{activeProject.name}</strong>
            <div className="form-hint" style={{ marginTop: 8 }}>
              <Target size={13} />
              <span>{activeProject.goal_statement || "该项目尚未填写目标问题，可到工作台补充。"}</span>
            </div>
          </div>
        ) : (
          <div className="empty-state" style={{ minHeight: 90 }}>
            <Target size={17} />
            <strong>暂无活跃项目</strong>
            <p>请先到工作台创建并选择一个项目，再上传数据。</p>
          </div>
        )}
      </section>
      <div className="grid grid-3">
        <div className="card metric-card">
          <div className="metric-label">数据集</div>
          <div className="metric-value">{loading ? "-" : items.length}</div>
          <div className="metric-change change-neutral">
            {activeProject ? `当前项目：${activeProject.name}` : "未关联项目"}
          </div>
        </div>
        <div className="card metric-card">
          <div className="metric-label">可分析版本</div>
          <div className="metric-value">{loading ? "-" : analyzableCount}</div>
          <div className="metric-change change-up">
            <Check size={12} />
            {versionCount ? Math.round((analyzableCount / versionCount) * 100) : 0}% 质量达标
          </div>
        </div>
        <div className="card metric-card">
          <div className="metric-label">本月导入版本</div>
          <div className="metric-value">{loading ? "-" : monthImports}</div>
          <div className="metric-change change-neutral">基于服务端创建时间</div>
        </div>
      </div>
      {loadError && (
        <div className="card card-pad" role="status" style={{ marginTop: 16, color: "var(--danger)" }}>
          {loadError}
        </div>
      )}
      <>
        <div className="toolbar" style={{ marginTop: 18 }}>
          <div className="toolbar-left">
            <div className="search">
              <Search size={15} />
              <input
                aria-label="搜索数据集"
                placeholder="搜索数据集或项目"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
          </div>
          <span style={{ color: "var(--faint)", fontSize: 12 }}>{filtered.length} 个数据集</span>
        </div>
        <section className="card table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>数据集</th>
                <th>项目</th>
                <th>规模</th>
                <th>版本</th>
                <th>质量</th>
                <th>状态</th>
                <th>最近更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filtered.map((dataset) => (
                <tr key={dataset.id}>
                  <td>
                    <Link
                      href={`/data/${dataset.id}`}
                      style={{ display: "flex", alignItems: "center", gap: 9 }}
                    >
                      <span className="bullet-icon" style={{ width: 28, height: 28 }}>
                        <Database size={14} />
                      </span>
                      <span>
                        <strong>{dataset.name}</strong>
                        <small
                          style={{ display: "block", marginTop: 3, color: "var(--faint)", fontSize: 10 }}
                        >
                          {dataset.kind}
                        </small>
                      </span>
                    </Link>
                  </td>
                  <td>{dataset.project}</td>
                  <td>
                    {dataset.rows} 行 · {dataset.columns} 列
                  </td>
                  <td>
                    <span className="tag tag-slate">{dataset.version}</span>
                  </td>
                  <td>
                    <span
                      style={{
                        color:
                          dataset.quality > 90
                            ? "var(--success)"
                            : dataset.quality > 80
                              ? "var(--warning)"
                              : "var(--danger)",
                        fontWeight: 700,
                      }}
                    >
                      {dataset.quality}
                    </span>
                    <span style={{ color: "var(--faint)" }}>/100</span>
                  </td>
                  <td>
                    <span
                      className={`tag ${dataset.status === "可分析" ? "tag-green" : dataset.status === "有风险" ? "tag-rose" : "tag-amber"}`}
                    >
                      {dataset.status}
                    </span>
                  </td>
                  <td>{dataset.updated}</td>
                  <td>
                    <button className="icon-btn" onClick={() => undefined} aria-label="更多操作">
                      <MoreHorizontal size={15} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {filtered.length === 0 && (
            <div className="empty-state">
              <Database size={19} />
              <strong>{loading ? "正在加载数据集" : "没有匹配的数据集"}</strong>
              <p>
                {loading
                  ? "正在从服务端读取当前工作空间的数据。"
                  : "尝试调整搜索关键词，或上传一个新的 CSV/XLSX 文件。"}
              </p>
            </div>
          )}
        </section>
      </>
      {showUpload && (
        <UploadModal
          activeProjectName={activeProject?.name}
          file={file}
          setFile={(name) => setFile(name)}
          onFile={setSelectedFile}
          onUpload={upload}
          onClose={() => setShowUpload(false)}
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

function UploadModal({
  activeProjectName,
  file,
  setFile,
  onFile,
  onUpload,
  onClose,
}: {
  activeProjectName?: string;
  file: string | null;
  setFile: (name: string | null) => void;
  onFile: (file: File | null) => void;
  onUpload: () => void;
  onClose: () => void;
}) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "grid",
        placeItems: "center",
        padding: 18,
        background: "rgba(16,28,52,.32)",
      }}
    >
      <div
        className="card"
        style={{ width: "min(520px, 100%)", padding: 23, boxShadow: "0 22px 60px rgba(16,28,52,.22)" }}
      >
        <div className="card-head">
          <div>
            <h2 className="card-title">上传数据集</h2>
            <div className="card-kicker">{UPLOAD_FORMAT_HINT}</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </div>
        <div className="form-hint" style={{ marginTop: 0 }}>
          <Target size={13} />
          <span>关联项目：{activeProjectName || "暂无活跃项目（请先到工作台选择）"}</span>
        </div>
        <label className="dropzone" style={{ cursor: "pointer", marginTop: 12 }}>
          <input
            type="file"
            accept={UPLOAD_ACCEPT_ATTR}
            hidden
            onChange={(event) => {
              const picked = event.target.files?.[0] ?? null;
              onFile(picked);
              setFile(picked?.name ?? null);
            }}
          />
          <div>
            <div className="dropzone-icon" style={{ margin: "0 auto 9px" }}>
              {file ? <FileSpreadsheet size={19} /> : <FileUp size={19} />}
            </div>
            <strong>{file ?? "选择或拖入文件"}</strong>
            <p>{file ? "已选择，继续后将创建数据集版本" : "自动识别 UTF-8、UTF-8-SIG、GBK 编码"}</p>
          </div>
        </label>
        <div className="form-hint" style={{ marginTop: 14 }}>
          <Trash2 size={13} />
          <span>原始文件保存在受控本地目录，数据版本不可变。</span>
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 18 }}>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn btn-primary" disabled={!file || !activeProjectName} onClick={onUpload}>
            <UploadCloud size={14} />
            开始解析
          </button>
        </div>
      </div>
    </div>
  );
}

function HistoryIcon() {
  return (
    <span className="empty-icon">
      <Database size={19} />
    </span>
  );
}
