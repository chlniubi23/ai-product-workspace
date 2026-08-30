"use client";

import Link from "next/link";
import { Check, ChevronRight, Lightbulb, X } from "lucide-react";
import { useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  EvidenceStatus,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate } from "@/lib/workflow";

export default function Stage7CopilotPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");
  const insights = snapshot?.insights || [];

  async function decide(id: string, status: "confirmed" | "rejected") {
    if (!accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      await apiRequest(`/insights/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
      setNotice(status === "confirmed" ? "已采纳，可以进入人机讨论。" : "已否决，这条洞察不会进入后续步骤。");
      await refresh();
    } catch (patchError) {
      setNotice(patchError instanceof Error ? patchError.message : "操作失败");
    } finally {
      setBusyId("");
    }
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={7}
        title="决策副驾"
        description="逐条判断第 6 步的洞察草稿。采纳需要证据引用，AI 只提供建议，采纳与否由你决定。"
        completion={completion}
        loading={loading}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={7} completion={completion} loading={loading}>
        {insights.length === 0 ? (
          <section className="card empty-state">
            <Lightbulb size={20} />
            <strong>还没有洞察草稿</strong>
            <p>先在第 6 步生成洞察。</p>
            <Link className="btn btn-primary btn-sm" href="/stage6-insight">
              前往第 6 步·洞察引擎 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <section className="card card-pad" style={{ marginTop: 16 }}>
            <div className="card-head">
              <div>
                <h2 className="card-title">待判断洞察</h2>
                <div className="card-kicker">
                  共 {insights.length} 条 · 已采纳{" "}
                  {insights.filter((item) => item.status === "confirmed").length} 条
                </div>
              </div>
              <span className="tag tag-blue">AI 辅助 · 人工裁决</span>
            </div>
            <div className="list">
              {insights.map((insight) => {
                const evidenceCount = insight.evidence_json?.length || 0;
                const settled = insight.status === "confirmed" || insight.status === "rejected";
                return (
                  <div className="card card-pad" key={insight.id} style={{ marginBottom: 12 }}>
                    <div className="card-head">
                      <div>
                        <strong>{insight.title || "未命名洞察"}</strong>
                        <div className="card-kicker">
                          {formatWorkflowDate(insight.created_at)} · 置信度 {insight.confidence || "未标注"}
                        </div>
                      </div>
                      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        <EvidenceStatus count={evidenceCount} />
                        <span
                          className={`tag ${insight.status === "confirmed" ? "tag-green" : insight.status === "rejected" ? "tag-rose" : "tag-amber"}`}
                        >
                          {insight.status || "draft"}
                        </span>
                      </div>
                    </div>
                    <p style={{ color: "var(--muted)", lineHeight: 1.6 }}>
                      {insight.content || "这条洞察没有正文。"}
                    </p>
                    {!settled && (
                      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
                        <button
                          className="btn btn-primary btn-sm"
                          disabled={busyId === insight.id || evidenceCount === 0}
                          onClick={() => void decide(insight.id, "confirmed")}
                        >
                          <Check size={13} />
                          采纳
                        </button>
                        <button
                          className="btn btn-subtle btn-sm"
                          disabled={busyId === insight.id}
                          onClick={() => void decide(insight.id, "rejected")}
                        >
                          <X size={13} />
                          否决
                        </button>
                        {evidenceCount === 0 && (
                          <span style={{ color: "var(--muted)", fontSize: 13, alignSelf: "center" }}>
                            缺少证据引用，无法采纳
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
              <Link className="btn btn-primary btn-sm" href="/stage8-discussion">
                下一步·人机讨论 <ChevronRight size={13} />
              </Link>
            </div>
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
