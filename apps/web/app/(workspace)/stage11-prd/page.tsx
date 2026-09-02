"use client";

import Link from "next/link";
import { ChevronRight, Download, FileText, Save, Sparkles } from "lucide-react";
import { useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  SnapshotMeta,
  WorkflowGate,
  WorkflowHeader,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate } from "@/lib/workflow";

type DocumentVersionRow = {
  id?: string;
  version_number?: number;
  created_at?: string;
  content_markdown?: string;
};

type DocumentRow = {
  id: string;
  title?: string;
  status?: string;
  document_type?: string;
  current_version?: DocumentVersionRow | null;
  versions?: DocumentVersionRow[];
};

const DOCUMENT_TYPES = [
  { value: "weekly_report", label: "分析周报", defaultTitle: "产品分析周报" },
  { value: "prd", label: "PRD", defaultTitle: "产品需求文档" },
  { value: "retrospective", label: "复盘", defaultTitle: "产品复盘报告" },
] as const;

const TERMINAL_JOB_STATUS = new Set(["succeeded", "failed", "cancelled"]);
/** Document generation is one AI call plus a version write; generous cap. */
const POLL_LIMIT = 60;

export default function Stage11PrdPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [docType, setDocType] = useState<string>("prd");
  const [title, setTitle] = useState("产品需求文档");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [progressNote, setProgressNote] = useState("");
  const [document, setDocument] = useState<DocumentRow | null>(null);
  const [versions, setVersions] = useState<DocumentVersionRow[]>([]);
  const [editorText, setEditorText] = useState("");
  const projectId = snapshot?.activeDataset?.project_id;
  const confirmed = snapshot?.insights.filter((insight) => insight.status === "confirmed") || [];
  const hasApprovedDecision =
    (snapshot?.decisions || []).some((decision) => decision.status === "approved");

  const projectDocuments = snapshot?.documents || [];

  function switchType(value: string) {
    setDocType(value);
    const preset = DOCUMENT_TYPES.find((item) => item.value === value);
    if (preset && (!title.trim() || DOCUMENT_TYPES.some((t) => t.defaultTitle === title))) {
      setTitle(preset.defaultTitle);
    }
  }

  async function waitForJob(jobId: string): Promise<string> {
    for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      try {
        const job = await apiRequest<{ status?: string }>(`/jobs/${jobId}`);
        const status = String(job.status || "");
        if (TERMINAL_JOB_STATUS.has(status)) return status;
      } catch {
        /* transient read failure: keep polling */
      }
    }
    return "timeout";
  }

  async function loadDocument(docId: string) {
    const doc = await apiRequest<DocumentRow>(`/documents/${docId}`);
    setDocument(doc);
    setVersions(doc.versions || []);
    setEditorText(doc.current_version?.content_markdown || "");
    return doc;
  }

  async function generate() {
    if (!projectId || !confirmed.length || !accessToken()) return;
    setBusy(true);
    setNotice("");
    setProgressNote("正在创建文档与证据清单…");
    try {
      const result = await apiRequest<{ document?: DocumentRow; job?: { id?: string } }>(
        "/ai/draft-document",
        {
          method: "POST",
          body: JSON.stringify({
            project_id: projectId,
            document_type: docType,
            title: title.trim() || "未命名文档",
            source_refs: confirmed.map((insight) => ({ type: "insight", id: insight.id })),
          }),
        },
      );
      const docId = result?.document?.id;
      const jobId = result?.job?.id;
      if (!docId) throw new Error("文档创建失败");
      setProgressNote("AI 正在依据证据撰写文档…");
      let finalDoc: DocumentRow | null = null;
      if (jobId) {
        const jobStatus = await waitForJob(jobId);
        if (jobStatus !== "succeeded") {
          setNotice(`文档生成任务${jobStatus === "timeout" ? "超时" : "失败"}，可重试或手动编辑。`);
        }
      }
      finalDoc = await loadDocument(docId);
      const content = finalDoc.current_version?.content_markdown || "";
      const looksLikeTemplate = content.includes("Draft summary of confirmed metrics") || content.includes("pending review");
      if (looksLikeTemplate) {
        setNotice("AI 生成不可用，已回退到模板草稿，可手动编辑后保存。");
      } else {
        setNotice("文档已生成，可编辑后保存为新版本。");
      }
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "文档生成失败");
    } finally {
      setProgressNote("");
      setBusy(false);
    }
  }

  async function saveNewVersion() {
    if (!document || !editorText.trim() || !accessToken()) return;
    setBusy(true);
    setNotice("");
    try {
      await apiRequest(`/documents/${document.id}/versions`, {
        method: "POST",
        body: JSON.stringify({ content_markdown: editorText }),
      });
      await loadDocument(document.id);
      setNotice("已保存为新版本。");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  function download() {
    const content = editorText;
    if (!content) return;
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = `${(title || "document").trim()}.md`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function showVersion(version: DocumentVersionRow) {
    setEditorText(version.content_markdown || "");
    setNotice(`正在查看版本 v${version.version_number ?? "?"}，编辑后保存会生成新版本。`);
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={11}
        title="交付"
        description="AI 依据已确认洞察、采访回答、已批准决策与数据聚合撰写文档；生成后可编辑、保存新版本并导出。"
        completion={completion}
        loading={loading || busy}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={11} completion={completion} loading={loading}>
        {!hasApprovedDecision ? (
          <section className="card empty-state">
            <FileText size={20} />
            <strong>还没有已批准的决策</strong>
            <p>先在第 10 步完成产品决策。</p>
            <Link className="btn btn-primary btn-sm" href="/stage10-decision">
              前往第 10 步·产品决策 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <>
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">生成交付文档</h2>
                  <div className="card-kicker">已选择 {confirmed.length} 条确认洞察作为证据来源</div>
                </div>
                <Sparkles size={17} color="#765ac6" />
              </div>
              <label className="field">
                <span className="field-label">文档类型</span>
                <select value={docType} onChange={(event) => switchType(event.target.value)}>
                  {DOCUMENT_TYPES.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">文档标题</span>
                <input value={title} onChange={(event) => setTitle(event.target.value)} />
              </label>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button
                  className="btn btn-primary"
                  disabled={busy || !title.trim()}
                  onClick={() => void generate()}
                >
                  <Sparkles size={14} />
                  {busy ? (progressNote || "生成中…") : "生成文档"}
                </button>
                {document && (
                  <button className="btn" onClick={download}>
                    <Download size={14} />
                    导出 Markdown
                  </button>
                )}
              </div>
              {progressNote && !busy && <p style={{ color: "var(--muted)", marginTop: 8 }}>{progressNote}</p>}
            </section>

            {document && (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">{document.title || "未命名文档"}</h2>
                    <div className="card-kicker">
                      当前 v{document.current_version?.version_number ?? "?"} · 共 {versions.length} 个版本
                    </div>
                  </div>
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={busy || !editorText.trim()}
                    onClick={() => void saveNewVersion()}
                  >
                    <Save size={13} />
                    保存为新版本
                  </button>
                </div>
                <textarea
                  value={editorText}
                  onChange={(event) => setEditorText(event.target.value)}
                  style={{
                    width: "100%",
                    minHeight: 400,
                    marginTop: 12,
                    padding: 14,
                    border: "1px solid var(--line)",
                    borderRadius: 8,
                    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                    fontSize: 12.5,
                    lineHeight: 1.7,
                    resize: "vertical",
                    background: "#fbfcfe",
                  }}
                  placeholder="文档内容"
                />
              </section>
            )}

            {projectDocuments.length > 0 && (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">项目文档</h2>
                    <div className="card-kicker">共 {projectDocuments.length} 份 · 点击切换查看</div>
                  </div>
                </div>
                <div className="list" style={{ marginTop: 8 }}>
                  {projectDocuments.map((doc) => (
                    <button
                      key={doc.id}
                      className="list-row"
                      style={{
                        width: "100%",
                        textAlign: "left",
                        background: "transparent",
                        border: "none",
                        cursor: "pointer",
                        padding: "8px 0",
                      }}
                      onClick={() => void loadDocument(doc.id)}
                    >
                      <div className="list-main">
                        <strong style={{ whiteSpace: "normal" }}>{doc.title || "未命名文档"}</strong>
                        <small style={{ whiteSpace: "normal" }}>
                          {doc.document_type || "document"} · v{doc.current_version?.version_number ?? "?"}
                          {doc.id === document?.id ? " · 当前查看" : ""}
                        </small>
                      </div>
                      {doc.status === "confirmed" ? (
                        <span className="tag tag-green">已确认</span>
                      ) : (
                        <span className="tag tag-amber">草稿</span>
                      )}
                    </button>
                  ))}
                </div>
              </section>
            )}

            {versions.length > 1 && (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">版本历史</h2>
                    <div className="card-kicker">点击回看内容</div>
                  </div>
                </div>
                <div className="list" style={{ marginTop: 8 }}>
                  {versions.map((version) => (
                    <button
                      key={version.id || version.version_number}
                      className="list-row"
                      style={{
                        width: "100%",
                        textAlign: "left",
                        background: "transparent",
                        border: "none",
                        cursor: "pointer",
                        padding: "6px 0",
                      }}
                      onClick={() => showVersion(version)}
                    >
                      <div className="list-main">
                        <strong>v{version.version_number ?? "?"}</strong>
                        <small>{formatWorkflowDate(version.created_at)}</small>
                      </div>
                      {document?.current_version?.id === version.id ? (
                        <span className="tag tag-green">当前</span>
                      ) : null}
                    </button>
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </WorkflowGate>
      {notice && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
    </div>
  );
}
