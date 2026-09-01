"use client";

import Link from "next/link";
import { Check, ChevronRight, Lightbulb, Sparkles, X } from "lucide-react";
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

type Claim = { text?: string; evidence?: Array<{ type?: string; id?: string }> };
type DistillResult = {
  facts?: Claim[];
  hypotheses?: Claim[];
  recommendations?: Claim[];
  limitations?: string[];
  summary?: string;
};

export default function Stage7CopilotPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");
  const [distilling, setDistilling] = useState(false);
  const [output, setOutput] = useState<DistillResult | null>(null);

  // 手工结论
  const [manualText, setManualText] = useState("");
  const projectId = snapshot?.activeDataset?.project_id;
  const insights = snapshot?.insights || [];

  async function distill() {
    if (!projectId || !accessToken()) return;
    setDistilling(true);
    setNotice("");
    try {
      const result = await apiRequest<{ status?: string; output?: DistillResult; interview_answer_count?: number }>(
        "/ai/distill-interview",
        { method: "POST", body: JSON.stringify({ project_id: projectId }) },
      );
      if (result?.status === "succeeded" && result.output) {
        setOutput(result.output);
        setNotice("洞察草稿已生成，请逐条检查引用后保存为草稿。");
      } else {
        setOutput(null);
        setNotice("AI 蒸馏暂不可用，可手写结论或稍后再试。");
      }
    } catch (distillError) {
      setNotice(distillError instanceof Error ? distillError.message : "蒸馏失败");
    } finally {
      setDistilling(false);
    }
  }

  async function saveDraftClaims() {
    if (!output || !projectId) return;
    const sections: Array<[keyof DistillResult, string]> = [
      ["facts", "fact"],
      ["hypotheses", "hypothesis"],
      ["recommendations", "recommendation"],
    ];
    setNotice("");
    try {
      let saved = 0;
      for (const [section, type] of sections) {
        for (const claim of (output[section] as Claim[] | undefined) || []) {
          if (!claim.text || !(claim.evidence || []).length) continue;
          await apiRequest("/insights", {
            method: "POST",
            body: JSON.stringify({
              project_id: projectId,
              title: claim.text.slice(0, 120),
              insight_type: type,
              content: claim.text,
              evidence: claim.evidence,
            }),
          });
          saved += 1;
        }
      }
      setNotice(`已保存 ${saved} 条洞察草稿，请在下方逐条裁决。`);
      setOutput(null);
      await refresh();
    } catch (saveError) {
      setNotice(saveError instanceof Error ? saveError.message : "洞察保存失败");
    }
  }

  async function saveManual() {
    if (!manualText.trim() || !projectId) {
      setNotice("请填写手工结论。");
      return;
    }
    setNotice("");
    try {
      // 引用最近一次成功分析的产物；没有分析产物时以数据版本为证据。
      let evidence: Array<{ type: string; id: string }> = [];
      const run = snapshot?.analysisRuns.find((item) => item.status === "succeeded");
      if (run?.id) {
        try {
          const detail = await apiRequest<{ artifacts?: Array<{ id?: string }> }>(`/analysis-runs/${run.id}`);
          const artifactId = detail.artifacts?.[0]?.id;
          if (artifactId) evidence = [{ type: "analysis_artifact", id: artifactId }];
        } catch {
          evidence = [];
        }
      }
      if (!evidence.length && snapshot?.activeVersion?.id) {
        evidence = [{ type: "dataset_version", id: snapshot.activeVersion.id }];
      }
      if (!evidence.length) {
        setNotice("没有可引用的分析产物，先在工作台完成一次分析。");
        return;
      }
      await apiRequest("/insights", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          title: manualText.trim().slice(0, 120),
          insight_type: "fact",
          content: manualText.trim(),
          evidence,
        }),
      });
      setManualText("");
      setNotice("手工洞察已存为草稿，请在下方裁决。");
      await refresh();
    } catch (manualError) {
      setNotice(manualError instanceof Error ? manualError.message : "手工洞察保存失败");
    }
  }

  async function decide(id: string, status: "confirmed" | "rejected") {
    if (!accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      await apiRequest(`/insights/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
      setNotice(status === "confirmed" ? "已采纳，可以进入产品问题。" : "已否决，这条洞察不会进入后续步骤。");
      await refresh();
    } catch (patchError) {
      setNotice(patchError instanceof Error ? patchError.message : "操作失败");
    } finally {
      setBusyId("");
    }
  }

  const renderClaims = (title: string, claims: Claim[] | undefined) => (
    <section className="card card-pad">
      <div className="card-head">
        <h2 className="card-title">{title}</h2>
        <EvidenceStatus
          count={(claims || []).reduce((total, claim) => total + (claim.evidence?.length || 0), 0)}
        />
      </div>
      {claims?.length ? (
        <div className="list">
          {claims.map((claim, index) => (
            <div className="list-row" key={`${title}-${index}`}>
              <div className="list-main">
                <strong>{claim.text || "未返回内容"}</strong>
                <small>
                  {claim.evidence?.length
                    ? `引用 ${claim.evidence.map((item) => item.id).join("、")}`
                    : "无数据支撑，不能确认"}
                </small>
              </div>
              {claim.evidence?.length ? (
                <Check size={15} color="#0f9f91" />
              ) : (
                <span className="tag tag-rose">待补证据</span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-state" style={{ minHeight: 110 }}>
          <Lightbulb size={17} />
          <strong>暂无内容</strong>
        </div>
      )}
    </section>
  );

  return (
    <div className="page">
      <WorkflowHeader
        step={7}
        title="决策副驾"
        description="把第 6 步的采访问答与数据结论蒸馏成洞察草稿，逐条裁决：采纳需要证据引用，AI 只提供建议。"
        completion={completion}
        loading={loading || distilling}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={7} completion={completion} loading={loading}>
        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">从采访生成洞察草稿</h2>
              <div className="card-kicker">AI 把采访问答 + 数据结论蒸馏成草稿；没有引用的句子不会落库。</div>
            </div>
            <button className="btn btn-primary" disabled={distilling} onClick={() => void distill()}>
              <Sparkles size={14} />
              {distilling ? "蒸馏中…" : "从采访生成洞察草稿"}
            </button>
          </div>
        </section>

        {output && (
          <>
            <div className="grid grid-3" style={{ marginTop: 16 }}>
              {renderClaims("事实", output.facts)}
              {renderClaims("推断", output.hypotheses)}
              {renderClaims("建议", output.recommendations)}
            </div>
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">保存洞察草稿</h2>
                  <div className="card-kicker">保存后仍是草稿，采纳与否在下方裁决。</div>
                </div>
                <button className="btn btn-primary" onClick={() => void saveDraftClaims()}>
                  <Check size={14} />
                  保存为草稿
                </button>
              </div>
              {output.limitations?.length ? (
                <p style={{ color: "var(--muted)" }}>{output.limitations.join("；")}</p>
              ) : (
                <p style={{ color: "var(--muted)" }}>请检查每条引用是否真实可溯。</p>
              )}
            </section>
          </>
        )}

        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">手工结论</h2>
              <div className="card-kicker">自动引用最近一次分析产物，保存后同为草稿。</div>
            </div>
          </div>
          <textarea
            rows={2}
            value={manualText}
            onChange={(event) => setManualText(event.target.value)}
            placeholder="记录你观察到的事实或下一步建议"
          />
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
            <button className="btn" onClick={() => void saveManual()}>
              <Check size={14} />
              保存手工洞察
            </button>
          </div>
        </section>

        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">待判断洞察</h2>
              <div className="card-kicker">
                共 {insights.length} 条 · 已采纳 {insights.filter((item) => item.status === "confirmed").length} 条
              </div>
            </div>
            <span className="tag tag-blue">AI 辅助 · 人工裁决</span>
          </div>
          {insights.length === 0 ? (
            <p style={{ color: "var(--muted)" }}>还没有洞察草稿。先从采访生成，或写一条手工结论。</p>
          ) : (
            <div className="list" style={{ marginTop: 8 }}>
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
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
            <Link className="btn btn-primary btn-sm" href="/stage8-problem">
              下一步·产品问题 <ChevronRight size={13} />
            </Link>
          </div>
        </section>
      </WorkflowGate>
      {notice && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
    </div>
  );
}
