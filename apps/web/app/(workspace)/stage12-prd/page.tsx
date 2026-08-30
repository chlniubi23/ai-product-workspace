"use client";

import Link from "next/link";
import { ChevronRight, Download, FileText, Sparkles } from "lucide-react";
import { useState } from "react";
import { apiRequest } from "@/lib/api";
import {
  SnapshotMeta,
  WorkflowGate,
  WorkflowHeader,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";

type DocumentRow = {
  id: string;
  title?: string;
  status?: string;
  current_version?: { content_markdown?: string } | null;
};

export default function Stage12PrdPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [title, setTitle] = useState("产品分析周报");
  const [document, setDocument] = useState<DocumentRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const projectId = snapshot?.activeDataset?.project_id;
  const confirmed = snapshot?.insights.filter((insight) => insight.status === "confirmed") || [];
  // The delivery gate follows the decision chain, not raw insight count: an
  // approved decision is what proves stages 9-11 actually happened.
  const hasApprovedDecision =
    (snapshot?.decisions || []).some((decision) => decision.status === "approved");

  const generate = async () => {
    if (!projectId || !confirmed.length) return;
    setBusy(true);
    setNotice("");
    try {
      const result = await apiRequest<{ document?: DocumentRow }>("/ai/draft-document", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          document_type: "weekly_report",
          title,
          source_refs: confirmed.map((insight) => ({ type: "insight", id: insight.id })),
        }),
      });
      setDocument(result.document || null);
      setNotice("文档草稿已生成，末尾包含证据清单");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "文档生成失败");
    } finally {
      setBusy(false);
    }
  };

  const download = () => {
    const content = document?.current_version?.content_markdown;
    if (!content) return;
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = `${title || "weekly-report"}.md`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="page">
      <WorkflowHeader
        step={12}
        title="交付"
        description="选择已确认洞察生成可编辑的 Markdown，并附上证据清单。"
        completion={completion}
        loading={loading || busy}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={12} completion={completion} loading={loading}>
        {!hasApprovedDecision ? (
          <section className="card empty-state">
            <FileText size={20} />
            <strong>还没有已批准的决策</strong>
            <p>先在第 11 步完成产品决策。</p>
            <Link className="btn btn-primary btn-sm" href="/stage11-decision">
              前往第 11 步·产品决策 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <section className="card card-pad" style={{ marginTop: 16 }}>
            <div className="card-head">
              <div>
                <h2 className="card-title">生成交付草稿</h2>
                <div className="card-kicker">已选择 {confirmed.length} 条确认洞察</div>
              </div>
              <Sparkles size={17} color="#765ac6" />
            </div>
            <div className="form-group">
              <label htmlFor="document-title">文档标题</label>
              <input id="document-title" value={title} onChange={(event) => setTitle(event.target.value)} />
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button
                className="btn btn-primary"
                disabled={busy || !title.trim()}
                onClick={() => void generate()}
              >
                <Sparkles size={14} />
                {busy ? "生成中…" : "生成周报草稿"}
              </button>
              {document && (
                <button className="btn" onClick={download}>
                  <Download size={14} />
                  导出 Markdown
                </button>
              )}
            </div>
            {document && (
              <pre
                style={{
                  marginTop: 18,
                  maxHeight: 420,
                  overflow: "auto",
                  whiteSpace: "pre-wrap",
                  padding: 16,
                  background: "#f7f9fc",
                  border: "1px solid var(--line)",
                  fontFamily: "inherit",
                  fontSize: 12,
                }}
              >
                {document.current_version?.content_markdown || "文档已创建，正在同步内容。"}
              </pre>
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
