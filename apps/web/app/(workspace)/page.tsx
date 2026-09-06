"use client";

import Link from "next/link";
import {
  BarChart3,
  Check,
  ChevronRight,
  FileSpreadsheet,
  FileUp,
  History,
  LoaderCircle,
  Plus,
  Sparkles,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiRequest, pagedItems } from "@/lib/api";
import { ChartRenderer } from "@/components/analysis/ChartRenderer";
import { ReportMarkdown } from "@/components/analysis/ReportMarkdown";
import { toChartOption } from "@/lib/chartOption";
import { getActiveProjectId, setActiveProjectId } from "@/lib/workflow";
import { formatDateTime, formatFileSize } from "@/lib/format";
import { UPLOAD_ACCEPT_ATTR, UPLOAD_FORMAT_HINT, UPLOAD_MAX_MB } from "@/lib/upload";

type Project = { id: string; name?: string; goal_statement?: string; status?: string };

type AutoReport = {
  id: string;
  title: string;
  status: string;
  summary: string;
  markdown?: string;
  content_markdown?: string;
  sections_json?: Array<{ heading?: string; content?: string }>;
  key_findings?: string[];
  recommendations?: string[];
  limitations?: string[];
  dataset_version_ids?: string[];
  error_code?: string | null;
  created_at?: string;
  confirmed_at?: string | null;
  narration_job_id?: string | null;
};

type BatchUploadResult = {
  uploads: Array<{ file_name?: string; version?: { id?: string }; job?: { id?: string } }>;
  failures: Array<{ file_name?: string; code?: string; message?: string }>;
};

type JobRow = { id?: string; status?: string; error_message?: string };

type AnalysisRun = {
  id: string;
  status?: string;
  analysis_type?: string;
  dataset_version_id?: string;
  artifacts?: Array<Record<string, unknown>>;
};

type ReportChart = { id: string; title: string; option: Record<string, unknown> };

const TERMINAL_JOB_STATUS = new Set(["succeeded", "failed", "cancelled"]);
/** Generous on purpose: a parse can run quality + up to three analyses. */
const POLL_LIMIT = 150;

const STATUS_META: Record<string, { label: string; tone: string }> = {
  succeeded: { label: "AI 生成 · 草稿", tone: "tag-amber" },
  // Batch 21: narration is manual now -- this status means "numbers ready,
  // waiting for the user to click 开始 AI 解读".
  not_configured: { label: "待 AI 解读 · 数据已就绪", tone: "tag-blue" },
  failed: { label: "AI 失败 · 仅统计", tone: "tag-rose" },
  confirmed: { label: "已确认", tone: "tag-green" },
  draft: { label: "草稿", tone: "tag-slate" },
};

/** Narration degradation is never silent: map the recorded error_code to an
 * actionable Chinese message (batch 10). */
function narrationFailureNotice(errorCode?: string | null, outcome?: string): string {
  switch (errorCode) {
    case "AI_BUDGET_EXCEEDED":
      return "AI 解读未能生成：今日 AI 额度剩余不足本次解读所需，请到「设置」调大「每日 token 预算」或明天再试。";
    case "LLM_NOT_CONFIGURED":
      return "AI 解读未能生成：未配置模型服务（DEEPSEEK_API_KEY）。";
    case "AI_FEATURE_DISABLED":
      return "AI 解读未能生成：工作空间已关闭自动报告的 AI 功能。";
    case "LLM_PROVIDER_ERROR":
      return "AI 解读未能生成：模型服务暂时不可用，请稍后重试。";
    case "LLM_TRUNCATED":
    case "INVALID_AI_OUTPUT":
      return "AI 解读未能生成：模型输出异常，请重试。";
    default:
      return outcome === "timeout"
        ? "AI 解读耗时较长，可稍后重试或刷新页面查看最新状态。"
        : "AI 解读未能生成，请重试。当前报告已包含确定性统计。";
  }
}

export default function WorkbenchPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [phase, setPhase] = useState<"idle" | "running">("idle");
  const [progressNote, setProgressNote] = useState("");
  const [failure, setFailure] = useState("");
  const [notice, setNotice] = useState("");
  const [report, setReport] = useState<AutoReport | null>(null);
  const [history, setHistory] = useState<AutoReport[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [charts, setCharts] = useState<ReportChart[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [deletingProject, setDeletingProject] = useState(false);
  const [narratingId, setNarratingId] = useState<string | null>(null);
  const [narrationNotice, setNarrationNotice] = useState("");
  // Batch 21: a "hint" notice invites the manual AI narration; an "error"
  // notice reports a failed narration and carries the retry button.
  const [narrationNoticeTone, setNarrationNoticeTone] = useState<"hint" | "error">("hint");
  const reportAnchor = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // --------------------------------------------------------------- loading
  const loadProjects = useCallback(async () => {
    try {
      const payload = await apiRequest<unknown>("/projects?include_archived=true");
      const active = pagedItems<Project>(payload).filter((project) => project.status !== "archived");
      setProjects(active);
      // Restore the persisted choice when it is still an active project;
      // otherwise fall back to the first one (batch 9).
      const stored = getActiveProjectId();
      const initial = (stored && active.find((project) => project.id === stored)?.id) || active[0]?.id || "";
      setProjectId(initial);
      setActiveProjectId(initial || null);
      return active;
    } catch {
      return [];
    }
  }, []);

  const loadHistory = useCallback(async (targetProjectId: string) => {
    if (!targetProjectId) {
      setHistory([]);
      return;
    }
    setHistoryLoading(true);
    try {
      const payload = await apiRequest<unknown>(
        `/projects/${targetProjectId}/auto-reports?page_size=10&page=1`,
      );
      const items = pagedItems<AutoReport>(payload);
      setHistory(items);
      setReport((current) => current ?? items[0] ?? null);
    } catch {
      setHistory([]);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    if (projectId) void loadHistory(projectId);
  }, [projectId, loadHistory]);

  const loadCharts = useCallback(async (targetProjectId: string, versions: string[]) => {
    if (!targetProjectId) return;
    try {
      const runs = pagedItems<AnalysisRun>(
        await apiRequest<unknown>(`/analysis-runs?project_id=${targetProjectId}&page_size=50`),
      );
      const wanted = new Set(versions);
      const relevant = runs
        .filter(
          (run) => run.status === "succeeded" && (!wanted.size || wanted.has(run.dataset_version_id || "")),
        )
        .slice(0, 4);
      const next: ReportChart[] = [];
      for (const run of relevant) {
        for (const [index, artifact] of (run.artifacts || []).entries()) {
          const title = String(artifact.title ?? `图表 ${index + 1}`);
          const option = toChartOption(artifact.payload_json ?? artifact.payload, title);
          if (option) next.push({ id: `${run.id}-${index}`, title, option });
        }
      }
      setCharts(next);
    } catch {
      setCharts([]);
    }
  }, []);

  useEffect(() => {
    if (report?.dataset_version_ids?.length && projectId) {
      void loadCharts(projectId, report.dataset_version_ids);
    } else {
      setCharts([]);
    }
  }, [report, projectId, loadCharts]);

  // --------------------------------------------------------------- actions
  const createProject = async () => {
    const name = newProjectName.trim();
    if (!name || creatingProject) return;
    setCreatingProject(true);
    try {
      const project = await apiRequest<Project>("/projects", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setNewProjectName("");
      setProjects((current) => [...current, project]);
      setProjectId(project.id);
      setActiveProjectId(project.id);
      setNotice(`已创建项目「${project.name || name}」`);
    } catch (cause) {
      setFailure(cause instanceof Error ? cause.message : "创建项目失败");
    } finally {
      setCreatingProject(false);
    }
  };

  const deleteProject = async () => {
    if (!projectId || deletingProject) return;
    const target = projects.find((project) => project.id === projectId);
    const confirmed = window.confirm(
      `确定删除项目「${target?.name || projectId}」？\n\n该项目下的数据集、分析产物和报告会一并删除，此操作不可恢复。`,
    );
    if (!confirmed) return;
    setDeletingProject(true);
    setFailure("");
    try {
      await apiRequest(`/projects/${projectId}?confirm=${projectId}`, { method: "DELETE" });
      const remaining = projects.filter((project) => project.id !== projectId);
      setProjects(remaining);
      const nextId = remaining[0]?.id || "";
      setProjectId(nextId);
      setActiveProjectId(nextId || null);
      setReport(null);
      setHistory([]);
      setCharts([]);
      setFiles([]);
      setNotice(`项目「${target?.name || projectId}」已删除`);
    } catch (cause) {
      setFailure(cause instanceof Error ? cause.message : "删除项目失败（需要项目 Owner 角色）");
    } finally {
      setDeletingProject(false);
    }
  };

  // Accepts plain File objects only. Callers MUST copy FileList -> Array in
  // the same synchronous tick: resetting input.value clears the live FileList,
  // and a React setState updater that runs later would read an empty list.
  const addFiles = (incoming: File[]) => {
    if (!incoming.length) return;
    setFailure("");
    setFiles((current) => {
      const next = [...current];
      for (const item of incoming) {
        if (!next.some((existing) => existing.name === item.name && existing.size === item.size)) {
          next.push(item);
        }
      }
      return next;
    });
  };

  const waitForJob = useCallback(async (jobId: string) => {
    for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      try {
        const job = await apiRequest<JobRow>(`/jobs/${jobId}`);
        const status = String(job.status || "");
        if (TERMINAL_JOB_STATUS.has(status)) return status;
        setProgressNote((note) => (note.includes("…") ? note : note));
      } catch {
        /* transient read failure: keep polling */
      }
    }
    return "timeout";
  }, []);

  // Instant deterministic half (batch 10): the numbers are on screen in
  // seconds, before any AI call happens.
  const computeReport = useCallback(async (targetProjectId: string): Promise<AutoReport | null> => {
    const result = await apiRequest<{ report?: AutoReport; status?: string }>(
      `/projects/${targetProjectId}/auto-report/compute`,
      { method: "POST" },
    );
    const computed = result.report ?? null;
    if (computed?.id) {
      setReport(computed);
      // compute 服务端已删除全部旧报告（唯一化语义），本地历史整体替换，不做合并。
      setHistory([computed]);
      setNarrationNotice("");
      setNarrationNoticeTone("hint");
    }
    return computed;
  }, []);

  const refreshReport = useCallback(async (reportId: string): Promise<AutoReport | null> => {
    try {
      const fresh = await apiRequest<AutoReport>(`/auto-reports/${reportId}`);
      setReport((current) => (current?.id === fresh.id ? fresh : current));
      setHistory((current) => current.map((item) => (item.id === fresh.id ? fresh : item)));
      return fresh;
    } catch {
      return null;
    }
  }, []);

  // Async AI half (batch 10): queue a narration job and poll it. Leaving the
  // page does not affect the server-side job; coming back, the effect below
  // resumes polling from the payload's narration_job_id (batch 16).
  const pollNarration = useCallback(
    async (targetReportId: string, jobId: string) => {
      setNarratingId(targetReportId);
      setNarrationNotice("");
      setNarrationNoticeTone("hint");
      const outcome = jobId ? await waitForJob(jobId) : "failed";
      if (!mountedRef.current) return;
      const fresh = await refreshReport(targetReportId);
      if (!fresh || fresh.status !== "succeeded") {
        setNarrationNotice(narrationFailureNotice(fresh?.error_code, outcome));
        setNarrationNoticeTone("error");
      }
      if (mountedRef.current) setNarratingId(null);
    },
    [refreshReport, waitForJob],
  );

  const narrateReport = useCallback(
    async (targetReportId: string) => {
      setNarratingId(targetReportId);
      setNarrationNotice("");
      setNarrationNoticeTone("hint");
      try {
        const result = await apiRequest<{ job?: { id?: string } }>(
          `/auto-reports/${targetReportId}/narrate`,
          {
            method: "POST",
          },
        );
        await pollNarration(targetReportId, result.job?.id || "");
      } catch (cause) {
        const message = cause instanceof Error ? cause.message : "AI 解读失败，请重试。";
        if (message.includes("NARRATION_IN_PROGRESS")) {
          // 另一处已在生成：刷新拿到 job id，恢复 effect 接管轮询，不报错。
          await refreshReport(targetReportId);
          return;
        }
        if (mountedRef.current) {
          setNarrationNotice(message);
          setNarrationNoticeTone("error");
          setNarratingId(null);
        }
      }
    },
    [pollNarration, refreshReport],
  );

  // Batch 16: resume narration polling after a page switch. The payload's
  // narration_job_id is non-null exactly while a job is queued/running, so
  // remounting this page re-enters the in-progress state automatically
  // (no button click, no duplicate queueing -- the 409 guard backs this up).
  useEffect(() => {
    const jobId = report?.narration_job_id;
    if (!jobId || !report?.id || narratingId) return;
    void pollNarration(report.id, jobId);
  }, [report, narratingId, pollNarration]);

  const startUpload = async () => {
    if (!files.length || !projectId) return;
    setPhase("running");
    setFailure("");
    setNotice("");
    setProgressNote(`正在上传 ${files.length} 个文件…`);
    try {
      const body = new FormData();
      body.append("project_id", projectId);
      for (const file of files) body.append("files", file);
      const result = await apiRequest<BatchUploadResult>("/datasets/upload-batch", { method: "POST", body });

      if (result.failures.length) {
        const rejected = result.failures
          .map((item) => `${item.file_name || "文件"}：${item.message || item.code || "不被支持"}`)
          .join("；");
        if (!result.uploads.length) {
          setFailure(rejected);
          setPhase("idle");
          return;
        }
        setNotice(`部分文件被跳过：${rejected}`);
      }

      const jobs = result.uploads.map((row) => ({ name: row.file_name || "文件", jobId: row.job?.id || "" }));
      const failedNames: string[] = [];
      let finished = 0;
      for (const entry of jobs) {
        setProgressNote(`正在解析与自动分析（${finished}/${jobs.length} 完成）…`);
        if (!entry.jobId) {
          failedNames.push(entry.name);
          finished += 1;
          continue;
        }
        const outcome = await waitForJob(entry.jobId);
        finished += 1;
        if (outcome !== "succeeded") {
          failedNames.push(outcome === "timeout" ? `${entry.name}（超时）` : `${entry.name}（解析失败）`);
        }
      }
      if (failedNames.length === jobs.length) {
        setFailure(`所有文件都解析失败：${failedNames.join("；")}`);
        setPhase("idle");
        return;
      }

      setProgressNote("分析完成，正在计算数据概况…");
      const computed = await computeReport(projectId);
      setPhase("idle");
      setFiles([]);
      if (computed) {
        // Batch 21: narration waits for the user -- show the numbers, let
        // them read, then click 开始 AI 解读.  No automatic narrate call.
        setNarrationNotice("数据概况已生成，请先查看数据，再点击「开始 AI 解读」。");
        setNarrationNoticeTone("hint");
        window.setTimeout(() => reportAnchor.current?.scrollIntoView({ behavior: "smooth" }), 120);
      } else {
        setFailure((current) => current || "报告生成失败，请稍后重试。");
      }
    } catch (cause) {
      setFailure(cause instanceof Error ? cause.message : "上传失败");
      setPhase("idle");
    }
  };

  const regenerate = async () => {
    if (!projectId || regenerating) return;
    setRegenerating(true);
    setNotice("");
    try {
      const computed = await computeReport(projectId);
      if (computed) {
        // Batch 21: same manual narration contract as the upload flow.
        setNarrationNotice("数据概况已生成，请先查看数据，再点击「开始 AI 解读」。");
        setNarrationNoticeTone("hint");
        window.setTimeout(() => reportAnchor.current?.scrollIntoView({ behavior: "smooth" }), 120);
      }
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "重新生成失败");
    } finally {
      setRegenerating(false);
    }
  };

  const confirmReport = async () => {
    if (!report || confirming) return;
    setConfirming(true);
    try {
      const confirmed = await apiRequest<AutoReport>(`/auto-reports/${report.id}/confirm`, {
        method: "POST",
      });
      setReport(confirmed);
      setHistory((current) => current.map((item) => (item.id === confirmed.id ? confirmed : item)));
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "确认失败");
    } finally {
      setConfirming(false);
    }
  };

  // ------------------------------------------------------------------ view
  const totalSize = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files]);
  const statusMeta = report ? STATUS_META[report.status] || STATUS_META.draft : null;
  const markdown = report?.markdown || report?.content_markdown || "";

  return (
    <div className="page">
      {/* 无活跃项目时的引导（batch 9：一个项目 = 一次工作流） */}
      {!projectId && (
        <section className="card empty-state" style={{ marginBottom: 16 }}>
          <h1 style={{ fontSize: 20, margin: 0 }}>新建项目，开始一次完整的数据分析工作流</h1>
          <p style={{ color: "var(--muted)" }}>
            上传 → 自动分析 → AI 采访 → 洞察裁决 → 问题 → 方案 → 决策 → 交付，全程围绕一个项目沉淀。
          </p>
          <Link className="btn btn-primary btn-sm" href="/history">
            查看历史工作流
          </Link>
        </section>
      )}
      {/* hero */}
      <section
        className="card card-pad"
        style={{ marginTop: 16, background: "linear-gradient(135deg, #f6f8ff 0%, #ffffff 60%)" }}
      >
        <div className="card-head">
          <div>
            <h1 style={{ fontSize: 22, margin: 0 }}>上传数据，直接得到分析报告</h1>
            <p className="card-kicker" style={{ marginTop: 6 }}>
              一次可传多个文件。解析、体检、挑分析维度、写报告全部自动完成——数字由 Pandas 计算，报告由 AI
              依据这些数字撰写，每一句都可追溯到统计结果。
            </p>
          </div>
          <span className="tag tag-blue">计算 + AI 报告</span>
        </div>

        <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap", marginTop: 8 }}>
          <div className="form-group" style={{ marginBottom: 0, minWidth: 220 }}>
            <label htmlFor="workbench-project">关联项目</label>
            <div style={{ display: "flex", gap: 6 }}>
              <select
                id="workbench-project"
                value={projectId}
                onChange={(event) => {
                  setProjectId(event.target.value);
                  setActiveProjectId(event.target.value || null);
                }}
                disabled={!projects.length || phase === "running"}
                style={{ flex: 1 }}
              >
                <option value="">{projects.length ? "选择项目" : "暂无项目"}</option>
                {projects.map((project) => (
                  <option value={project.id} key={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn btn-subtle btn-sm"
                onClick={() => void deleteProject()}
                disabled={!projectId || deletingProject || phase === "running"}
                title="删除当前项目"
                aria-label="删除当前项目"
              >
                <Trash2 size={13} />
                {deletingProject ? "删除中…" : "删除"}
              </button>
            </div>
          </div>
          <div className="form-group" style={{ marginBottom: 0, minWidth: 200 }}>
            <label htmlFor="workbench-new-project">或新建项目</label>
            <div style={{ display: "flex", gap: 6 }}>
              <input
                id="workbench-new-project"
                value={newProjectName}
                placeholder="项目名称"
                onChange={(event) => setNewProjectName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void createProject();
                }}
              />
              <button
                type="button"
                className="btn btn-subtle btn-sm"
                onClick={() => void createProject()}
                disabled={!newProjectName.trim() || creatingProject}
              >
                <Plus size={13} />
                创建
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* upload */}
      {phase !== "running" && (
        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">上传数据集</h2>
              <div className="card-kicker">{UPLOAD_FORMAT_HINT}</div>
            </div>
            {files.length > 0 && (
              <button type="button" className="btn btn-subtle btn-sm" onClick={() => setFiles([])}>
                <X size={13} />
                清空
              </button>
            )}
          </div>
          {/* Native invisible overlay: the input covers the whole zone, so
              clicking the zone IS a native click on the input. No JS click()
              chain involved — immune to the event-bubbling quirks that made
              the dialog silently fail. Dropping a file onto the input is also
              handled natively by the browser. */}
          <div
            className={`dropzone ${dragOver ? "drag-over" : ""}`}
            style={{ position: "relative", cursor: "pointer" }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragOver(false);
              addFiles(Array.from(event.dataTransfer.files));
            }}
          >
            <input
              type="file"
              accept={UPLOAD_ACCEPT_ATTR}
              multiple
              aria-label="选择文件"
              onChange={(event) => {
                // Snapshot the FileList before resetting the input, or the
                // deferred setState updater would see an emptied list.
                addFiles(Array.from(event.target.files ?? []));
                event.target.value = "";
              }}
              style={{
                position: "absolute",
                inset: 0,
                width: "100%",
                height: "100%",
                opacity: 0,
                cursor: "pointer",
              }}
            />
            <div style={{ pointerEvents: "none" }}>
              <div className="dropzone-icon" style={{ margin: "0 auto 9px" }}>
                <FileUp size={19} />
              </div>
              <strong>
                {files.length
                  ? `已选择 ${files.length} 个文件（共 ${formatFileSize(totalSize)}）`
                  : "点击选择或拖入文件，可多选"}
              </strong>
              <p>
                {files.length ? "点击下方按钮开始，无需再做别的操作" : "自动识别 UTF-8、UTF-8-SIG、GBK 编码"}
              </p>
            </div>
          </div>
          {files.length > 0 && (
            <div style={{ marginTop: 10, display: "grid", gap: 6 }}>
              {files.map((file) => (
                <div
                  key={`${file.name}-${file.size}`}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    fontSize: 12,
                    color: "var(--muted)",
                  }}
                >
                  <FileSpreadsheet size={13} />
                  <span
                    style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  >
                    {file.name}
                  </span>
                  <span>{formatFileSize(file.size)}</span>
                  <button
                    type="button"
                    aria-label={`移除 ${file.name}`}
                    onClick={() => setFiles((current) => current.filter((item) => item !== file))}
                    style={{ cursor: "pointer", display: "inline-flex", color: "inherit" }}
                  >
                    <X size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}
          {failure && (
            <div className="form-error" role="alert" style={{ marginTop: 12 }}>
              {failure}
            </div>
          )}
          {notice && (
            <p className="card-kicker" role="status" style={{ marginTop: 10 }}>
              {notice}
            </p>
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 18 }}>
            <button className="btn btn-primary" disabled={!files.length || !projectId} onClick={startUpload}>
              <UploadCloud size={14} />
              {files.length > 1 ? `上传 ${files.length} 个文件并自动分析` : "上传并自动分析"}
            </button>
          </div>
        </section>
      )}

      {/* progress */}
      {phase === "running" && (
        <section className="card workflow-loading-card" style={{ marginTop: 16 }} aria-live="polite">
          <div className="workflow-loading-dot" />
          <strong>{progressNote || "正在处理…"}</strong>
          <p>这一步不需要你点任何按钮。完成后报告会直接出现在下方。</p>
        </section>
      )}

      {/* report */}
      <div ref={reportAnchor} />
      {report && (
        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">{report.title || "数据分析报告"}</h2>
              <div className="card-kicker">
                {formatDateTime(report.created_at)}
                {report.dataset_version_ids?.length
                  ? ` · 覆盖 ${report.dataset_version_ids.length} 个数据版本`
                  : ""}
              </div>
            </div>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <span className={`tag ${statusMeta?.tone || "tag-slate"}`}>
                {statusMeta?.label || report.status}
              </span>
              {(report.status === "not_configured" || report.status === "failed") &&
                !narratingId &&
                narrationNoticeTone !== "error" &&
                !report.narration_job_id && (
                  <button
                    className="btn btn-primary btn-sm"
                    onClick={() => void narrateReport(report.id)}
                    title="对当前确定性报告做一次 AI 解读"
                  >
                    <Sparkles size={13} />
                    开始 AI 解读
                  </button>
                )}
              {report.status !== "confirmed" && (
                <button className="btn btn-subtle btn-sm" onClick={confirmReport} disabled={confirming}>
                  <Check size={13} />
                  {confirming ? "确认中…" : "确认报告"}
                </button>
              )}
              <button className="btn btn-subtle btn-sm" onClick={regenerate} disabled={regenerating}>
                <LoaderCircle size={13} />
                {regenerating ? "生成中…" : "重新生成"}
              </button>
            </div>
          </div>
          <div style={{ display: "grid", gap: 4 }}>
            <ReportMarkdown markdown={markdown} />
            {narratingId === report.id && (
              <div
                className="card-kicker"
                role="status"
                style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}
              >
                <LoaderCircle size={13} className="animate-spin" />
                AI 解读生成中…页面可以正常操作，离开本页不影响后台生成；返回后报告会显示最新状态。
              </div>
            )}
            {narrationNotice && !narratingId && narrationNoticeTone === "error" && (
              <div
                role="alert"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  flexWrap: "wrap",
                  marginTop: 8,
                  color: "#b4443c",
                }}
              >
                <span>{narrationNotice}</span>
                <button className="btn btn-subtle btn-sm" onClick={() => void narrateReport(report.id)}>
                  <LoaderCircle size={13} />
                  重试 AI 解读
                </button>
              </div>
            )}
            {narrationNotice && !narratingId && narrationNoticeTone === "hint" && (
              <div className="card-kicker" role="status" style={{ marginTop: 8 }}>
                {narrationNotice}
              </div>
            )}
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
            <Link className="btn btn-primary btn-sm" href="/stage6-interview">
              下一步·AI 采访 <ChevronRight size={13} />
            </Link>
          </div>
        </section>
      )}

      {/* deterministic charts behind the report */}
      {charts.length > 0 && (
        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">报告背后的计算图表</h2>
              <div className="card-kicker">
                共 {charts.length} 张，全部由 Pandas 计算结果直接绘制，可复现。
              </div>
            </div>
            <span className="tag tag-green">计算层 · 无 AI</span>
          </div>
          <div style={{ display: "grid", gap: 16 }}>
            {charts.map((chart) => (
              <div key={chart.id}>
                <div className="card-kicker" style={{ marginBottom: 6 }}>
                  {chart.title}
                </div>
                <ChartRenderer option={chart.option} title={chart.title} height={300} />
              </div>
            ))}
          </div>
        </section>
      )}

      {/* history */}
      <section className="card card-pad" style={{ marginTop: 16 }}>
        <div className="card-head">
          <div>
            <h2 className="card-title">报告历史</h2>
            <div className="card-kicker">当前项目的分析报告（重新生成会替换旧报告）。</div>
          </div>
          <History size={16} color="#8e9ab0" />
        </div>
        {historyLoading ? (
          <p className="card-kicker">加载中…</p>
        ) : history.length === 0 ? (
          <div className="empty-state">
            <BarChart3 size={18} />
            <strong>还没有报告</strong>
            <p>上传数据后，报告会出现在这里。</p>
          </div>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {history.map((item) => (
              <button
                key={item.id}
                type="button"
                className="card"
                style={{
                  textAlign: "left",
                  padding: "12px 14px",
                  cursor: "pointer",
                  borderColor: item.id === report?.id ? "#4a6cf7" : undefined,
                }}
                onClick={() => {
                  setReport(item);
                  setNarrationNotice("");
                  window.setTimeout(() => reportAnchor.current?.scrollIntoView({ behavior: "smooth" }), 80);
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Sparkles size={13} color={item.status === "confirmed" ? "#1f9d63" : "#8e9ab0"} />
                  <strong
                    style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  >
                    {item.title || "数据分析报告"}
                  </strong>
                  <span className={`tag ${(STATUS_META[item.status] || STATUS_META.draft).tone}`}>
                    {(STATUS_META[item.status] || STATUS_META.draft).label}
                  </span>
                  <span className="card-kicker">{formatDateTime(item.created_at)}</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
