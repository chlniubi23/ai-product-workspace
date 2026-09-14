"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Archive, ChevronRight, Download, FileText, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiRequest, accessToken, pagedItems } from "@/lib/api";
import { JobProgress } from "@/components/common/JobProgress";
import { formatWorkflowDate, setActiveProjectId } from "@/lib/workflow";
import {
  SnapshotMeta,
  WorkflowGate,
  WorkflowHeader,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";

type DocumentVersionRow = {
  id?: string;
  version_number?: number;
  created_at?: string;
  content_markdown?: string;
  ai_status?: string | null;
  ai_error_code?: string | null;
};

type DocumentRow = {
  id: string;
  title?: string;
  status?: string;
  document_type?: string;
  created_at?: string;
  current_version?: DocumentVersionRow | null;
  generation_job_id?: string | null;
  generation_progress?: { progress?: number | null; current_step?: string | null } | null;
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
  // Batch 25: numeric progress + step from the job row, driving JobProgress.
  const [generationProgress, setGenerationProgress] = useState<number | null>(null);
  const [generationStartedAt, setGenerationStartedAt] = useState<number | null>(null);
  const [fallbackBanner, setFallbackBanner] = useState("");
  const [document, setDocument] = useState<DocumentRow | null>(null);
  const [archiving, setArchiving] = useState(false);
  const [editorText, setEditorText] = useState("");
  // Non-null when the editor content was restored from the persisted version
  // (mount/type switch) rather than freshly generated in this mount.
  const [hydratedAt, setHydratedAt] = useState<string | null>(null);
  const hydrateSeq = useRef(0);
  const projectId = snapshot?.activeDataset?.project_id;
  const confirmed = snapshot?.insights.filter((insight) => insight.status === "confirmed") || [];
  const approvedDecision = [...(snapshot?.decisions || [])]
    .filter((decision) => decision.status === "approved")
    .at(-1);
  const hasApprovedDecision = Boolean(approvedDecision);

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
        // Batch 17: two-pass generation reports per-section progress through
        // the job's current_step -- surface it verbatim ("正在撰写 第 N/M 节…").
        const job = await apiRequest<{ status?: string; current_step?: string; progress?: number }>(
          `/jobs/${jobId}`,
        );
        const status = String(job.status || "");
        if (TERMINAL_JOB_STATUS.has(status)) return status;
        if (job.current_step) setProgressNote(job.current_step);
        if (typeof job.progress === "number") setGenerationProgress(job.progress);
      } catch {
        /* transient read failure: keep polling */
      }
    }
    return "timeout";
  }

  /** Hydrate the page from the persisted (project, document_type) document.
   * generate_document is find-or-create, so at most one document exists per
   * type; the latest created_at wins if legacy data ever doubled up.  The
   * not-found branch resets to the "未生成" state with generation available. */
  const hydrateDocument = useCallback(
    async (fromGeneration = false) => {
      if (!projectId || !accessToken()) return;
      const seq = ++hydrateSeq.current;
      try {
        const docs = pagedItems<DocumentRow>(
          await apiRequest<unknown>(`/documents?project_id=${projectId}&page_size=100`),
        );
        if (seq !== hydrateSeq.current) return; // a newer hydration superseded us
        const matching = docs
          .filter((doc) => doc.document_type === docType)
          .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
        const doc = matching[0];
        if (doc) {
          setDocument(doc);
          setEditorText(doc.current_version?.content_markdown || "");
          setTitle((current) => doc.title || current);
          // Degradation stays visible across remounts.
          const aiStatus = doc.current_version?.ai_status;
          setFallbackBanner(
            aiStatus && aiStatus !== "succeeded" ? fallbackNotice(doc.current_version?.ai_error_code) : "",
          );
          setHydratedAt(fromGeneration ? null : doc.current_version?.created_at || doc.created_at || null);
        } else {
          setDocument(null);
          setEditorText("");
          setFallbackBanner("");
          setHydratedAt(null);
        }
      } catch {
        /* 列表读取失败时保留现状：生成按钮仍可用 */
      }
    },
    [projectId, docType],
  );

  // Re-hydrate whenever the page mounts onto a (project, document_type) pair:
  // client-side navigation unmounts this component and loses local state, but
  // the document is persisted server-side.
  useEffect(() => {
    void hydrateDocument();
  }, [projectId, docType, hydrateDocument]);

  // Batch 16: resume a generation job after a page switch. The hydrated
  // document carries generation_job_id while a generation job is queued or
  // running; entering it re-uses the exact generate() flow (progress note,
  // poll, re-hydrate) without another click or a duplicate queueing.
  useEffect(() => {
    const jobId = document?.generation_job_id;
    if (!jobId || busy) return;
    setProgressNote("正在生成新版本…");
    setGenerationProgress(document.generation_progress?.progress ?? null);
    setGenerationStartedAt(Date.now());
    void (async () => {
      setBusy(true);
      try {
        await waitForJob(jobId);
        await hydrateDocument(true);
        setNotice("文档已就绪，可编辑后导出。");
        await refresh();
      } finally {
        setProgressNote("");
        setGenerationProgress(null);
        setGenerationStartedAt(null);
        setBusy(false);
      }
    })();
    // generation_progress is only read as the bar's seed value; the effect
    // re-runs are harmless (busy guard) and keep the lint contract whole.
  }, [document?.generation_job_id, document?.generation_progress, busy, hydrateDocument, refresh]);

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
            // Batch 17: the approved decision joins the evidence list so the
            // generated document treats it as the narrative axis.
            source_refs: [
              ...confirmed.map((insight) => ({ type: "insight", id: insight.id })),
              ...(approvedDecision ? [{ type: "decision_proposal", id: approvedDecision.id }] : []),
            ],
          }),
        },
      );
      const docId = result?.document?.id;
      const jobId = result?.job?.id;
      if (!docId) throw new Error("文档创建失败");
      // Old content stays visible while the new version is being written.
      setProgressNote("正在生成新版本…");
      setGenerationStartedAt(Date.now());
      if (jobId) {
        await waitForJob(jobId);
      }
      // Same fill path as hydration: after the inline job the persisted
      // latest version is authoritative.
      await hydrateDocument(true);
      setNotice("文档已就绪，可编辑后导出。");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "文档生成失败");
    } finally {
      setProgressNote("");
      setGenerationProgress(null);
      setGenerationStartedAt(null);
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
        title="交付文档"
        description="基于已确认洞察、采访回答与已批准决策生成交付文档；支持编辑并导出 Markdown。"
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
              {busy && generationStartedAt && (
                <div style={{ flex: 1, minWidth: 0 }}>
                  <JobProgress
                    progress={generationProgress}
                    currentStep={progressNote || "正在生成新版本…"}
                    startedAt={generationStartedAt}
                  />
                </div>
              )}
              {document && (
                <button className="btn" onClick={download} disabled={!editorText.trim()}>
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
                  border: "1px solid var(--warning-border)",
                  borderRadius: 8,
                  background: "var(--warning-soft)",
                  color: "var(--warning)",
                  fontSize: 13,
                  lineHeight: 1.6,
                }}
              >
                {fallbackBanner}
              </div>
            )}

            {/* Batch 17: a failed generation leaves a version-less shell --
                tell the user plainly instead of showing an empty editor. */}
            {document?.status === "generation_failed" &&
              !document.generation_job_id &&
              !document.current_version && (
                <div
                  role="alert"
                  style={{
                    marginTop: 14,
                    padding: "10px 12px",
                    border: "1px solid var(--danger-border)",
                    borderRadius: 8,
                    background: "var(--danger-soft)",
                    color: "var(--danger)",
                    fontSize: 13,
                    lineHeight: 1.6,
                  }}
                >
                  上次生成失败，请重新生成。
                </div>
              )}

            {document &&
              !document.current_version &&
              !document.generation_job_id &&
              document.status !== "generation_failed" && (
                <p style={{ color: "var(--muted)", fontSize: 13, margin: "14px 0 0" }}>
                  尚未生成文档；点击「生成文档」开始，生成期间可离开本页。
                </p>
              )}

            {document && (
              <>
                {hydratedAt && (
                  <p style={{ color: "var(--muted)", fontSize: 12, margin: "10px 0 0" }}>
                    内容恢复自最近生成的版本 {formatWorkflowDate(hydratedAt)}；编辑后请导出保存。
                  </p>
                )}
                <textarea
                  value={editorText}
                  onChange={(event) => setEditorText(event.target.value)}
                  style={{
                    width: "100%",
                    minHeight: 400,
                    marginTop: 8,
                    padding: 14,
                    border: "1px solid var(--line)",
                    borderRadius: 8,
                    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                    fontSize: 12.5,
                    lineHeight: 1.7,
                    resize: "vertical",
                    background: "var(--panel)",
                  }}
                  placeholder="文档内容"
                />
              </>
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
