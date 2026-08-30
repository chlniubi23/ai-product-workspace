"use client";

import Link from "next/link";
import { Check, ChevronRight, Lightbulb, Sparkles } from "lucide-react";
import { useState } from "react";
import { apiRequest } from "@/lib/api";
import {
  EvidenceStatus,
  SnapshotMeta,
  WorkflowGate,
  WorkflowHeader,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";

type Claim = { text?: string; evidence?: Array<{ type?: string; id?: string }> };
type AIResult = {
  facts?: Claim[];
  hypotheses?: Claim[];
  recommendations?: Claim[];
  limitations?: string[];
  summary?: string;
};

export default function Stage6InsightPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [output, setOutput] = useState<AIResult | null>(null);
  const [question, setQuestion] = useState("");
  const [manualText, setManualText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const projectId = snapshot?.activeDataset?.project_id;
  const run = snapshot?.analysisRuns.find((item) => item.status === "succeeded");

  const interpret = async () => {
    if (!projectId || !run?.dataset_version_id) return;
    setBusy(true);
    setNotice("");
    try {
      const result = await apiRequest<{ output?: AIResult; structured?: AIResult }>("/ai/interpret", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          dataset_version_id: run.dataset_version_id,
          question: question || "请结合项目目标解读最近一次分析",
          context: { analysis_run_id: run.id },
        }),
      });
      setOutput(result.output || result.structured || null);
      setNotice("AI 草稿已生成，请逐条确认引用后保存");
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "AI 解读失败");
    } finally {
      setBusy(false);
    }
  };

  const confirmClaims = async () => {
    if (!output || !projectId) return;
    const sections: Array<[keyof AIResult, string]> = [
      ["facts", "fact"],
      ["hypotheses", "hypothesis"],
      ["recommendations", "recommendation"],
    ];
    try {
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
              status: "confirmed",
            }),
          });
        }
      }
      setNotice("已确认并保存有证据的洞察");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "洞察保存失败");
    }
  };

  const saveManual = async () => {
    if (!manualText.trim() || !projectId || !run) {
      setNotice("请填写手工结论，并确保分析产物已保存");
      return;
    }
    try {
      const detail = await apiRequest<{ artifacts?: Array<{ id?: string }> }>(`/analysis-runs/${run.id}`);
      const artifactId = detail.artifacts?.[0]?.id;
      if (!artifactId) throw new Error("最近一次分析没有可引用产物");
      await apiRequest("/insights", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          title: manualText.trim().slice(0, 120),
          insight_type: "fact",
          content: manualText.trim(),
          evidence: [{ type: "analysis_artifact", id: artifactId }],
          status: "confirmed",
        }),
      });
      setManualText("");
      setNotice("手工洞察已确认并保存");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "手工洞察保存失败");
    }
  };

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
        step={6}
        title="结论"
        description="AI 只组织已有分析产物，所有结论先是草稿，确认后才能交付。"
        completion={completion}
        loading={loading || busy}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={6} completion={completion} loading={loading}>
        {!run ? (
          <section className="card empty-state">
            <Sparkles size={20} />
            <strong>先完成一次分析</strong>
            <p>AI 只能引用第 5 步保存的分析报告。</p>
            <Link className="btn btn-primary btn-sm" href="/data">
              前往第 4 步·自动分析 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <>
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">生成解读草稿</h2>
                  <div className="card-kicker">原始整表不会发送给 AI</div>
                </div>
                <Sparkles size={17} color="#765ac6" />
              </div>
              <div className="form-group">
                <label htmlFor="interpret-question">本次要回答的问题（可选）</label>
                <textarea
                  id="interpret-question"
                  rows={2}
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  placeholder="默认使用项目目标问题"
                />
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button className="btn btn-primary" disabled={busy} onClick={() => void interpret()}>
                  <Sparkles size={14} />
                  {busy ? "生成中…" : "让 AI 解读"}
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
                      <h2 className="card-title">人工确认</h2>
                      <div className="card-kicker">没有引用的句子不会进入交付</div>
                    </div>
                    <button className="btn btn-primary" onClick={() => void confirmClaims()}>
                      <Check size={14} />
                      确认并保存
                    </button>
                  </div>
                  {output.limitations?.length ? (
                    <p style={{ color: "var(--muted)" }}>{output.limitations.join("；")}</p>
                  ) : (
                    <p style={{ color: "var(--muted)" }}>请检查每条引用是否能回到分析产物。</p>
                  )}
                </section>
              </>
            )}
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">手工结论</h2>
                  <div className="card-kicker">AI 不可用时仍可继续，自动引用最近一次分析产物</div>
                </div>
              </div>
              <textarea
                rows={3}
                value={manualText}
                onChange={(event) => setManualText(event.target.value)}
                placeholder="记录你确认后的事实或下一步建议"
              />
              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
                <button className="btn" onClick={() => void saveManual()}>
                  <Check size={14} />
                  保存手工洞察
                </button>
              </div>
            </section>
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
