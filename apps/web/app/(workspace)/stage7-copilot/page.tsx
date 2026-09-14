"use client";

import Link from "next/link";
import { Check, ChevronRight, Lightbulb, Pencil, Sparkles, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import { JobProgress } from "@/components/common/JobProgress";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  EvidenceStatus,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate, type WorkflowInsight } from "@/lib/workflow";

export default function Stage7CopilotPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");
  const [distilling, setDistilling] = useState(false);
  // Batch 25: the distill request is synchronous (no job row) -- the bar runs
  // in indeterminate mode with a timer while the call is in flight.
  const [distillStartedAt, setDistillStartedAt] = useState<number | null>(null);
  // Batch 18: inline edit state per insight (null = not editing).
  const [editingId, setEditingId] = useState("");
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [confirmingRest, setConfirmingRest] = useState(false);

  // 手工结论
  const [manualText, setManualText] = useState("");
  const projectId = snapshot?.activeDataset?.project_id;
  const insights = snapshot?.insights || [];
  const draftInsights = insights.filter((item) => item.status === "draft");

  async function distill() {
    if (!projectId || !accessToken()) return;
    setDistilling(true);
    setDistillStartedAt(Date.now());
    setNotice("");
    try {
      const result = await apiRequest<{
        status?: string;
        error_code?: string | null;
        created?: WorkflowInsight[];
      }>("/ai/distill-interview", { method: "POST", body: JSON.stringify({ project_id: projectId }) });
      if (result?.status === "succeeded") {
        await refresh();
        const count = result.created?.length || 0;
        setNotice(
          count > 0
            ? `AI 已基于证据生成 ${count} 条草稿；请逐条裁决——确认、修改或弃用。`
            : "AI 未能产出有证据支撑的结论，可补充采访回答后重试，或手写结论。",
        );
      } else if (result?.error_code === "LLM_TRUNCATED") {
        setNotice("AI 输出过长被截断，已自动重试仍失败。可稍后再试，或把采访回答写得更精炼。");
      } else if (result?.error_code === "INVALID_AI_OUTPUT") {
        setNotice("AI 返回格式异常，请重试。");
      } else {
        setNotice("AI 蒸馏暂不可用，可手写结论或稍后再试。");
      }
    } catch (distillError) {
      setNotice(distillError instanceof Error ? distillError.message : "蒸馏失败");
    } finally {
      setDistilling(false);
      setDistillStartedAt(null);
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
      // The insight carries its own evidence; confirming keeps it as-is.
      await apiRequest(`/insights/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
      setNotice(status === "confirmed" ? "已确认。" : "已弃用；该操作已记录审计日志。");
      await refresh();
    } catch (patchError) {
      setNotice(patchError instanceof Error ? patchError.message : "操作失败");
    } finally {
      setBusyId("");
    }
  }

  function startEdit(insight: WorkflowInsight) {
    setEditingId(insight.id);
    setEditTitle(insight.title || "");
    setEditContent(insight.content || "");
  }

  async function saveEdit(id: string) {
    if (!accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      await apiRequest(`/insights/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ title: editTitle.trim(), content: editContent.trim() }),
      });
      setEditingId("");
      setNotice("已保存修改。");
      await refresh();
    } catch (editError) {
      setNotice(editError instanceof Error ? editError.message : "保存失败");
    } finally {
      setBusyId("");
    }
  }

  // Batch 18: "confirm the rest" -- sequentially confirm every still-draft
  // insight.  A failure on one card is reported but the rest carry on.
  async function confirmRest() {
    if (!accessToken() || confirmingRest) return;
    setConfirmingRest(true);
    setNotice("");
    const failures: string[] = [];
    for (const insight of draftInsights) {
      try {
        await apiRequest(`/insights/${insight.id}`, {
          method: "PATCH",
          body: JSON.stringify({ status: "confirmed" }),
        });
      } catch {
        failures.push(insight.title || insight.id.slice(0, 8));
      }
    }
    await refresh();
    setConfirmingRest(false);
    if (failures.length) {
      setNotice(`部分确认失败（${failures.join("、")}），通常是缺少证据引用；其余已完成。`);
    } else {
      setNotice(`已确认 ${draftInsights.length} 条草稿。`);
    }
  }

  // The snapshot's analysis-run list does not carry artifacts (relationship
  // field), so fetch them once per succeeded run to label artifact references.
  const [artifactTitles, setArtifactTitles] = useState<Map<string, string>>(new Map());
  const artifactRunsFetched = useRef<Set<string>>(new Set());
  useEffect(() => {
    const succeeded = (snapshot?.analysisRuns || []).filter((run) => run.status === "succeeded" && run.id);
    const pending = succeeded.filter((run) => !artifactRunsFetched.current.has(run.id));
    if (!pending.length || !accessToken()) return;
    let cancelled = false;
    const next = new Map(artifactTitles);
    void (async () => {
      for (const run of pending) {
        try {
          const artifacts = await apiRequest<Array<{ id?: string; title?: string }>>(
            `/analysis-runs/${run.id}/artifacts`,
          );
          for (const artifact of artifacts || []) {
            if (artifact.id && !next.has(artifact.id))
              next.set(artifact.id, `分析产物 · ${artifact.title || artifact.id.slice(0, 8)}`);
          }
        } catch {
          // labeling only; a failed fetch falls back to the short-uuid label
        }
        // mark only after resolution so StrictMode's discarded first mount
        // does not poison the dedupe set
        artifactRunsFetched.current.add(run.id);
      }
      if (!cancelled) setArtifactTitles(new Map(next));
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot]);

  // id -> readable label for evidence references (interview answers first,
  // then artifacts of succeeded analysis runs; unknown ids fall back to a
  // shortened uuid).
  const evidenceLabels = useMemo(() => {
    const labels = new Map<string, string>();
    for (const question of snapshot?.interviewQuestions || []) {
      labels.set(question.id, `采访回答 · ${question.topic || (question.question_text || "").slice(0, 18)}`);
    }
    for (const [id, title] of artifactTitles) {
      if (!labels.has(id)) labels.set(id, title);
    }
    return labels;
  }, [snapshot, artifactTitles]);

  function evidenceLabel(reference: { type?: string; id?: string }): string {
    const label = reference.id ? evidenceLabels.get(reference.id) : undefined;
    return label || `引用 ${(reference.id || "").slice(0, 8)}…`;
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={7}
        title="洞察蒸馏"
        description="AI 基于证据生成洞察草稿；逐条裁决——确认、修改或弃用。确认须引用至少一条证据。"
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
              <div className="card-kicker">
                蒸馏结果自动落库为草稿（条数由证据决定）；没有证据的句子不会落库。
              </div>
            </div>
            <button className="btn btn-primary" disabled={distilling} onClick={() => void distill()}>
              <Sparkles size={14} />
              {distilling ? "蒸馏中…" : draftInsights.length ? "重新蒸馏" : "从采访生成洞察草稿"}
            </button>
            {distilling && distillStartedAt && (
              <JobProgress indeterminate currentStep="正在蒸馏洞察…" startedAt={distillStartedAt} />
            )}
          </div>
          {draftInsights.length > 0 && (
            <p style={{ color: "var(--muted)", marginTop: 8, marginBottom: 0 }}>
              再次蒸馏会刷新仍是草稿的条目；已确认/已弃用的不受影响。
            </p>
          )}
        </section>

        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">待裁决洞察</h2>
              <div className="card-kicker">
                共 {insights.length} 条 · 已确认{" "}
                {insights.filter((item) => item.status === "confirmed").length} · 已弃用{" "}
                {insights.filter((item) => item.status === "rejected").length} · 待裁决 {draftInsights.length}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {draftInsights.length > 1 && (
                <button
                  className="btn btn-primary btn-sm"
                  disabled={confirmingRest}
                  onClick={() => void confirmRest()}
                >
                  <Check size={13} />
                  {confirmingRest ? "确认中…" : `确认其余 ${draftInsights.length} 条`}
                </button>
              )}
              <span className="tag tag-blue">人工做减法</span>
            </div>
          </div>
          {insights.length === 0 ? (
            <p style={{ color: "var(--muted)" }}>还没有洞察草稿。先从采访蒸馏，或写一条手工结论。</p>
          ) : (
            <div className="list" style={{ marginTop: 8 }}>
              {insights.map((insight) => {
                const evidenceCount = insight.evidence_json?.length || 0;
                const settled = insight.status === "confirmed" || insight.status === "rejected";
                const editing = editingId === insight.id;
                return (
                  <div className="card card-pad" key={insight.id} style={{ marginBottom: 12 }}>
                    <div className="card-head">
                      <div>
                        <strong>{insight.title || "未命名洞察"}</strong>
                        <div className="card-kicker">
                          {formatWorkflowDate(insight.created_at)}
                          {insight.insight_type ? ` · ${insight.insight_type}` : ""}
                        </div>
                      </div>
                      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        <EvidenceStatus count={evidenceCount} />
                        <span
                          className={`tag ${insight.status === "confirmed" ? "tag-green" : insight.status === "rejected" ? "tag-rose" : "tag-amber"}`}
                        >
                          {insight.status === "confirmed"
                            ? "已确认"
                            : insight.status === "rejected"
                              ? "已弃用"
                              : insight.status || "draft"}
                        </span>
                      </div>
                    </div>
                    {editing ? (
                      <>
                        <label className="field" style={{ marginTop: 8 }}>
                          <span className="field-label">标题</span>
                          <input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} />
                        </label>
                        <label className="field">
                          <span className="field-label">正文</span>
                          <textarea
                            rows={3}
                            value={editContent}
                            onChange={(event) => setEditContent(event.target.value)}
                          />
                        </label>
                        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 8 }}>
                          <button
                            className="btn btn-subtle btn-sm"
                            disabled={busyId === insight.id}
                            onClick={() => setEditingId("")}
                          >
                            取消
                          </button>
                          <button
                            className="btn btn-primary btn-sm"
                            disabled={busyId === insight.id || !editTitle.trim() || !editContent.trim()}
                            onClick={() => void saveEdit(insight.id)}
                          >
                            保存修改
                          </button>
                        </div>
                      </>
                    ) : (
                      <>
                        <p style={{ color: "var(--muted)", lineHeight: 1.6 }}>
                          {insight.content || "这条洞察没有正文。"}
                        </p>
                        <p style={{ color: "var(--muted)", fontSize: 13, margin: "4px 0 0" }}>
                          证据：
                          {(insight.evidence_json || [])
                            .map((item) => evidenceLabel(item as { type?: string; id?: string }))
                            .join("、") || "无数据支撑"}
                        </p>
                        {!settled && (
                          <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
                            <button
                              className="btn btn-primary btn-sm"
                              disabled={busyId === insight.id || evidenceCount === 0}
                              onClick={() => void decide(insight.id, "confirmed")}
                            >
                              <Check size={13} />
                              确认
                            </button>
                            <button
                              className="btn btn-subtle btn-sm"
                              disabled={busyId === insight.id}
                              onClick={() => void decide(insight.id, "rejected")}
                            >
                              <X size={13} />
                              弃用
                            </button>
                            <button
                              className="btn btn-subtle btn-sm"
                              disabled={busyId === insight.id}
                              onClick={() => startEdit(insight)}
                            >
                              <Pencil size={13} />
                              编辑
                            </button>
                            {evidenceCount === 0 && (
                              <span style={{ color: "var(--muted)", fontSize: 13, alignSelf: "center" }}>
                                缺少证据引用，无法确认
                              </span>
                            )}
                          </div>
                        )}
                      </>
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

        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">手工结论</h2>
              <div className="card-kicker">自动引用最近一次分析产物，保存后同为草稿。</div>
            </div>
            <Lightbulb size={16} color="var(--faint)" />
          </div>
          <label className="field">
            <span className="field-label">手工结论</span>
            <textarea
              rows={3}
              value={manualText}
              onChange={(event) => setManualText(event.target.value)}
              placeholder="记录你观察到的事实或下一步建议"
            />
          </label>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
            <button className="btn" onClick={() => void saveManual()}>
              <Check size={14} />
              保存手工洞察
            </button>
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
