"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ArchiveRestore, ChevronLeft, Download } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { apiRequest, pagedItems } from "@/lib/api";
import { ReportMarkdown } from "@/components/analysis/ReportMarkdown";
import { formatWorkflowDate } from "@/lib/workflow";

type DatasetRow = { id: string; name?: string; versions?: Array<{ row_count?: number; version_number?: number }> };
type AnalysisRunRow = { id: string; status?: string; analysis_type?: string };
type InsightRow = { id: string; title?: string; content?: string; status?: string; evidence_json?: unknown[] };
type InterviewRow = { id: string; topic?: string; question_text?: string; answer_text?: string; status?: string };
type ProblemRow = { id: string; title?: string; statement?: string; status?: string };
type SolutionRow = { id: string; problem_id?: string; title?: string; approach?: string; status?: string; reject_reason?: string };
type DecisionRow = { id: string; title?: string; proposed_action?: string; status?: string };
type DocumentRow = {
  id: string;
  title?: string;
  document_type?: string;
  current_version?: { content_markdown?: string } | null;
};
type AutoReportRow = { id: string; title?: string; status?: string; markdown?: string; content_markdown?: string };

export default function HistoryDetailPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params?.projectId;
  const [project, setProject] = useState<{ name?: string; status?: string; archived_at?: string } | null>(null);
  const [datasets, setDatasets] = useState<DatasetRow[]>([]);
  const [runs, setRuns] = useState<AnalysisRunRow[]>([]);
  const [insights, setInsights] = useState<InsightRow[]>([]);
  const [interview, setInterview] = useState<InterviewRow[]>([]);
  const [problems, setProblems] = useState<ProblemRow[]>([]);
  const [solutions, setSolutions] = useState<SolutionRow[]>([]);
  const [decisions, setDecisions] = useState<DecisionRow[]>([]);
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [autoReport, setAutoReport] = useState<AutoReportRow | null>(null);
  const [openDocId, setOpenDocId] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [restoring, setRestoring] = useState(false);
  const [restored, setRestored] = useState(false);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError("");
    try {
      const scope = `?project_id=${projectId}&page_size=100`;
      const get = async <T,>(path: string): Promise<T[]> => pagedItems<T>(await apiRequest<unknown>(path + scope));
      const [datasetsRows, runsRows, insightsRows, interviewRows, problemRows, solutionRows, decisionRows, documentRows] =
        await Promise.all([
          get<DatasetRow>("/datasets"),
          get<AnalysisRunRow>("/analysis-runs"),
          get<InsightRow>("/insights"),
          get<InterviewRow>("/interview-questions"),
          get<ProblemRow>("/problems"),
          get<SolutionRow>("/solutions"),
          get<DecisionRow>("/decision-proposals"),
          get<DocumentRow>("/documents"),
        ]);
      setDatasets(datasetsRows);
      setRuns(runsRows);
      setInsights(insightsRows);
      setInterview(interviewRows.filter((row) => row.status === "answered"));
      setProblems(problemRows);
      setSolutions(solutionRows);
      setDecisions(decisionRows);
      setDocuments(documentRows);

      const reports = pagedItems<AutoReportRow>(
        await apiRequest<unknown>(`/projects/${projectId}/auto-reports?page_size=5&page=1`),
      );
      setAutoReport(reports[0] ?? null);

      const projectDetail = await apiRequest<{ name?: string; status?: string; archived_at?: string }>(
        `/projects/${projectId}`,
      );
      setProject(projectDetail);
      setRestored(projectDetail.status !== "archived");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "历史数据加载失败");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function restore() {
    if (!projectId || restoring) return;
    setRestoring(true);
    try {
      await apiRequest(`/projects/${projectId}/unarchive`, { method: "POST" });
      setRestored(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "恢复失败");
    } finally {
      setRestoring(false);
    }
  }

  function download(title: string, content: string) {
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = `${title || "document"}.md`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const sectionTitle = { fontSize: 15, margin: "0 0 8px" } as const;

  return (
    <div className="page">
      <header className="workflow-header" style={{ marginBottom: 4 }}>
        <div className="workflow-header-main">
          <div className="workflow-eyebrow">
            <span>历史 · 只读回看</span>
          </div>
          <h1>{project?.name || "工作流回看"}</h1>
          <p>归档于 {formatWorkflowDate(project?.archived_at)}。以下为该次工作流的全部产出，只读展示。</p>
        </div>
        <div className="workflow-header-side">
          <span className="tag tag-slate">只读回看</span>
          {!restored ? (
            <button className="btn btn-subtle btn-sm" disabled={restoring} onClick={() => void restore()}>
              <ArchiveRestore size={13} />
              恢复到活跃
            </button>
          ) : (
            <span className="tag tag-green">已恢复到活跃</span>
          )}
        </div>
      </header>
      <Link className="btn btn-subtle btn-sm" href="/history" style={{ marginBottom: 12 }}>
        <ChevronLeft size={13} />
        返回历史列表
      </Link>
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      {loading ? (
        <section className="card" style={{ marginTop: 16, padding: 24 }}>正在加载工作流产出…</section>
      ) : (
        <>
          <section className="card card-pad" style={{ marginTop: 16 }}>
            <h2 style={sectionTitle}>数据与报告</h2>
            <p style={{ color: "var(--muted)" }}>
              数据集 {datasets.length} 个（
              {datasets.map((d) => `${d.name || "未命名"} ${d.versions?.length ?? 0} 版本`).join("、") || "无"}）
              · 分析运行 {runs.filter((r) => r.status === "succeeded").length} 次成功
            </p>
            {autoReport && (
              <>
                <div className="card-kicker" style={{ margin: "10px 0 6px" }}>
                  分析报告：{autoReport.title}（{autoReport.status}）
                </div>
                <ReportMarkdown markdown={autoReport.markdown || autoReport.content_markdown || ""} />
              </>
            )}
          </section>

          <section className="card card-pad" style={{ marginTop: 16 }}>
            <h2 style={sectionTitle}>洞察与裁决</h2>
            <p style={{ color: "var(--muted)" }}>
              共 {insights.length} 条 · 已采纳 {insights.filter((i) => i.status === "confirmed").length} · 已否决{" "}
              {insights.filter((i) => i.status === "rejected").length}
            </p>
            <div className="list" style={{ marginTop: 8 }}>
              {insights.map((insight) => (
                <div className="card card-pad" key={insight.id} style={{ marginBottom: 8 }}>
                  <strong>{insight.title || "未命名洞察"}</strong>
                  <p style={{ color: "var(--muted)", margin: "4px 0 0" }}>
                    {insight.content} · 证据 {insight.evidence_json?.length || 0} 条 · {insight.status}
                  </p>
                </div>
              ))}
            </div>
          </section>

          <section className="card card-pad" style={{ marginTop: 16 }}>
            <h2 style={sectionTitle}>采访记录（已回答）</h2>
            {interview.length === 0 ? (
              <p style={{ color: "var(--muted)" }}>无采访记录。</p>
            ) : (
              <div className="list" style={{ marginTop: 8 }}>
                {interview.map((row) => (
                  <div className="card card-pad" key={row.id} style={{ marginBottom: 8 }}>
                    <strong>{row.question_text}</strong>
                    <p style={{ margin: "4px 0 0", lineHeight: 1.6 }}>{row.answer_text}</p>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card card-pad" style={{ marginTop: 16 }}>
            <h2 style={sectionTitle}>决策链</h2>
            {problems.length === 0 && solutions.length === 0 && decisions.length === 0 ? (
              <p style={{ color: "var(--muted)" }}>无问题/方案/决策记录。</p>
            ) : (
              <div className="list" style={{ marginTop: 8 }}>
                {problems.map((problem) => (
                  <div className="card card-pad" key={problem.id} style={{ marginBottom: 8 }}>
                    <strong>问题：{problem.title}</strong>
                    <p style={{ color: "var(--muted)", margin: "4px 0" }}>{problem.statement}</p>
                    {solutions
                      .filter((s) => s.problem_id === problem.id)
                      .map((solution) => (
                        <div key={solution.id} style={{ borderTop: "1px solid var(--line)", padding: "6px 0" }}>
                          <strong>方案：{solution.title}</strong>
                          <span
                            className={`tag ${solution.status === "selected" ? "tag-green" : "tag-rose"}`}
                            style={{ marginLeft: 8 }}
                          >
                            {solution.status === "selected" ? "已选定" : "未采纳"}
                          </span>
                          <p style={{ color: "var(--muted)", margin: "4px 0 0" }}>{solution.approach}</p>
                          {solution.reject_reason && (
                            <p style={{ color: "var(--muted)", fontSize: 12, margin: "2px 0 0" }}>
                              落选理由：{solution.reject_reason}
                            </p>
                          )}
                        </div>
                      ))}
              </div>
                ))}
                {decisions.map((decision) => (
                  <div className="card card-pad" key={decision.id} style={{ marginBottom: 8 }}>
                    <strong>决策：{decision.title}</strong>
                    <span className={`tag ${decision.status === "approved" ? "tag-green" : "tag-amber"}`} style={{ marginLeft: 8 }}>
                      {decision.status}
                    </span>
                    <p style={{ color: "var(--muted)", margin: "4px 0 0" }}>{decision.proposed_action}</p>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card card-pad" style={{ marginTop: 16 }}>
            <h2 style={sectionTitle}>交付文档</h2>
            {documents.length === 0 ? (
              <p style={{ color: "var(--muted)" }}>无交付文档。</p>
            ) : (
              documents.map((doc) => {
                const content = doc.current_version?.content_markdown || "";
                return (
                  <div className="card card-pad" key={doc.id} style={{ marginBottom: 10 }}>
                    <div className="card-head">
                      <div>
                        <strong>{doc.title || "未命名文档"}</strong>
                        <div className="card-kicker">{doc.document_type}</div>
                      </div>
                      <button
                        className="btn btn-subtle btn-sm"
                        onClick={() => download(doc.title || "document", content)}
                        disabled={!content}
                      >
                        <Download size={13} />
                        导出
                      </button>
                    </div>
                    {openDocId === doc.id ? (
                      <>
                        <ReportMarkdown markdown={content} />
                        <button className="btn btn-subtle btn-sm" style={{ marginTop: 8 }} onClick={() => setOpenDocId("")}>
                          收起
                        </button>
                      </>
                    ) : (
                      <button className="btn btn-subtle btn-sm" onClick={() => setOpenDocId(doc.id)}>
                        展开文档
                      </button>
                    )}
                  </div>
                );
              })
            )}
          </section>
        </>
      )}
    </div>
  );
}
