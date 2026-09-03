"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Archive, ChevronRight, Download, FileText, Sparkles } from "lucide-react";
import { useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import { setActiveProjectId } from "@/lib/workflow";
import {
  SnapshotMeta,
  WorkflowGate,
  WorkflowHeader,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";

type DocumentVersionRow = {
  id?: string;
  version_number?: number;
  content_markdown?: string;
  ai_status?: string | null;
  ai_error_code?: string | null;
};

type DocumentRow = {
  id: string;
  title?: string;
  status?: string;
  current_version?: DocumentVersionRow | null;
};

const DOCUMENT_TYPES = [
  { value: "weekly_report", label: "分析周报", defaultTitle: "产品分析周报" },
  { value: "prd", label: "PRD", defaultTitle: "产品需求文档" },
  { value: "retrospective", label: "复盘", defaultTitle: "产品复盘报告" },
] as const;

const TERMINAL_JOB_STATUS = new Set(["succeeded", "failed", "cancelled"]);
/** Document generation is one AI call plus a version write; generous cap. */
const POLL_LIMIT = 60;

/** Template fallbacks are never silent: map the recorded failure to an
 * actionable Chinese message (batch 8). */
function fallbackNotice(aiErrorCode: string | null | undefined): string {
  switch (aiErrorCode) {
    case "AI_BUDGET_EXCEEDED":
      return "今日 AI 额度剩余不足本次生成所需，请到「设置」调大「每日 token 预算」或明天再试。当前展示的是模板草稿。";
    case "LLM_PROVIDER_ERROR":
      return "模型服务暂时不可用，已回退到模板草稿，可手动编辑后导出，稍后可重新生成。";
    case "LLM_NOT_CONFIGURED":
      return "未配置模型服务，当前展示的是模板草稿，可手动编辑后导出。";
    default:
      return "AI 生成不可用，已回退到模板草稿，可手动编辑后导出。";
  }
}

export default function Stage11PrdPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const router = useRouter();
  const [docType, setDocType] = useState<string>("prd");
  const [title, setTitle] = useState("产品需求文档");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [progressNote, setProgressNote] = useState("");
  const [fallbackBanner, setFallbackBanner] = useState("");
  const [document, setDocument] = useState<DocumentRow | null>(null);
  const [archiving, setArchiving] = useState(false);
  const [editorText, setEditorText] = useState("");
  const projectId = snapshot?.activeDataset?.project_id;
  const confirmed = snapshot?.insights.filter((insight) => insight.status === "confirmed") || [];
  const hasApprovedDecision =
    (snapshot?.decisions || []).some((decision) => decision.status === "approved");

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

  async function loadDocument(docId: string): Promise<DocumentRow> {
    const doc = await apiRequest<DocumentRow>(`/documents/${docId}`);
    setDocument(doc);
    const content = doc.current_version?.content_markdown || "";
    setEditorText(content);
    // Degradation is visible, never silent: a non-succeeded version shows why.
    const aiStatus = doc.current_version?.ai_status;
    if (aiStatus && aiStatus !== "succeeded") {
      setFallbackBanner(fallbackNotice(doc.current_version?.ai_error_code));
    } else {
      setFallbackBanner("");
    }
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
      // Old content stays visible while the new version is being written.
      setProgressNote("正在生成新版本…");
      if (jobId) {
        await waitForJob(jobId);
      }
      await loadDocument(docId);
      setNotice("文档已就绪，可编辑后导出。");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "文档生成失败");
    } finally {
      setProgressNote("");
      setBusy(false);
    }
  }

  async function archiveProject() {
    if (!projectId || !accessToken()) return;
    const confirmed = window.confirm(
      "完成并归档当前项目？归档后项目转为只读历史（可在「历史」中回看全部产出或恢复），工作台将切换到其他活跃项目。",
    );
    if (!confirmed) return;
    setArchiving(true);
    setNotice("");
    try {
      await apiRequest(`/projects/${projectId}/archive`, { method: "POST" });
      setActiveProjectId(null);
      router.push("/history");
    } catch (cause) {
      // 归档后停留在本页的写路径会拿到 409 PROJECT_ARCHIVED，如实展示。
      setNotice(cause instanceof Error ? cause.message : "归档失败");
      setArchiving(false);
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

  return (
    <div className="page">
      <WorkflowHeader
        step={11}
        title="交付"
        description="AI 依据已确认洞察、采访回答、已批准决策与数据聚合撰写文档，生成后可编辑并导出。"
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
                {busy ? progressNote || "生成中…" : "生成文档"}
              </button>
              {document && (
                <button className="btn" onClick={download}>
                  <Download size={14} />
                  导出 Markdown
                </button>
              )}
              {document && (
                <button
                  className="btn btn-primary"
                  disabled={archiving}
                  onClick={() => void archiveProject()}
                >
                  <Archive size={14} />
                  {archiving ? "归档中…" : "完成并归档"}
                </button>
              )}
            </div>

            {fallbackBanner && (
              <div
                role="status"
                style={{
                  marginTop: 14,
                  padding: "10px 12px",
                  border: "1px solid #f0d49a",
                  borderRadius: 8,
                  background: "#fff9eb",
                  color: "#8b5c08",
                  fontSize: 13,
                  lineHeight: 1.6,
                }}
              >
                {fallbackBanner}
              </div>
            )}

            {document && (
              <textarea
                value={editorText}
                onChange={(event) => setEditorText(event.target.value)}
                style={{
                  width: "100%",
                  minHeight: 400,
                  marginTop: 14,
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
            )}
          </section>
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
