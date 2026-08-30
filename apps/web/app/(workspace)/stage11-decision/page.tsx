"use client";

import Link from "next/link";
import { ChevronRight, Gavel, Send, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  EvidenceStatus,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate } from "@/lib/workflow";

const PRIORITIES = ["P0", "P1", "P2", "P3"];

export default function Stage11DecisionPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [problemId, setProblemId] = useState("");
  const [title, setTitle] = useState("");
  const [action, setAction] = useState("");
  const [validation, setValidation] = useState("");
  const [impact, setImpact] = useState("");
  const [risk, setRisk] = useState("");
  const [priority, setPriority] = useState("P2");
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");

  const confirmedProblems = useMemo(
    () => (snapshot?.problems || []).filter((item) => item.status === "confirmed"),
    [snapshot],
  );
  const activeProblemId = problemId || confirmedProblems[0]?.id || "";
  const activeProblem = confirmedProblems.find((item) => item.id === activeProblemId);
  const selectedSolution = useMemo(
    () =>
      (snapshot?.solutions || []).find(
        (item) => item.problem_id === activeProblemId && item.status === "selected",
      ),
    [snapshot, activeProblemId],
  );
  const decisions = snapshot?.decisions || [];
  const projectId = snapshot?.activeDataset?.project_id;

  const evidence = useMemo(() => {
    const refs = (activeProblem?.source_insight_ids || []).map((id) => ({ type: "insight", id }));
    return refs;
  }, [activeProblem]);

  function prefill() {
    if (!selectedSolution || !activeProblem) return;
    setTitle(selectedSolution.title || "");
    setAction(selectedSolution.approach || "");
    setImpact(activeProblem.impact_scope || "");
    setRisk((selectedSolution.cons || []).join("；"));
    setPriority(activeProblem.priority || "P2");
  }

  async function create() {
    if (!projectId || !accessToken() || !title.trim() || !action.trim() || !validation.trim()) return;
    setBusy(true);
    setNotice("");
    try {
      await apiRequest("/decision-proposals", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          title: title.trim(),
          problem_statement: activeProblem?.statement || "",
          proposed_action: action.trim(),
          expected_impact: impact.trim(),
          risk_summary: risk.trim(),
          validation_plan: validation.trim(),
          priority,
          evidence,
        }),
      });
      setTitle("");
      setAction("");
      setValidation("");
      setImpact("");
      setRisk("");
      setNotice("决策已存为草稿。提交后需要你自己确认一次，作为落笔前的最后检查。");
      await refresh();
    } catch (createError) {
      setNotice(createError instanceof Error ? createError.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function submit(id: string) {
    if (!accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      const result = await apiRequest<{ approval_request?: { id: string; version?: number } }>(
        `/decision-proposals/${id}/submit`,
        { method: "POST" },
      );
      const approvalId = result?.approval_request?.id;
      const version = result?.approval_request?.version ?? 1;
      if (approvalId) {
        await apiRequest(`/approval-requests/${approvalId}/approve`, {
          method: "POST",
          body: JSON.stringify({ version, decision_note: "本人确认" }),
        });
      }
      setNotice("决策已确认，可以生成 PRD。");
      await refresh();
    } catch (submitError) {
      setNotice(submitError instanceof Error ? submitError.message : "提交失败");
    } finally {
      setBusyId("");
    }
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={11}
        title="产品决策"
        description="把选定方案写成一条可追溯的决策：做什么、预期什么、怎么验证。证据自动继承问题引用的洞察。"
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
        {!selectedSolution ? (
          <section className="card empty-state">
            <Gavel size={20} />
            <strong>还没有选定的方案</strong>
            <p>先在第 10 步选定一个方案。</p>
            <Link className="btn btn-primary btn-sm" href="/stage10-solution">
              前往第 10 步·方案讨论 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <>
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">写一条决策</h2>
                  <div className="card-kicker">基于「{selectedSolution.title || "选定方案"}」</div>
                </div>
                <EvidenceStatus count={evidence.length} />
              </div>

              {confirmedProblems.length > 1 && (
                <label className="field">
                  <span className="field-label">对应问题</span>
                  <select value={activeProblemId} onChange={(event) => setProblemId(event.target.value)}>
                    {confirmedProblems.map((problem) => (
                      <option key={problem.id} value={problem.id}>
                        {problem.title || problem.id.slice(0, 8)}
                      </option>
                    ))}
                  </select>
                </label>
              )}

              <label className="field">
                <span className="field-label">决策标题</span>
                <input
                  value={title}
                  placeholder="例如：验证码环节增加短信兜底"
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">决定做什么</span>
                <textarea
                  rows={3}
                  value={action}
                  placeholder="具体动作，越明确越好"
                  onChange={(event) => setAction(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">预期效果</span>
                <input
                  value={impact}
                  placeholder="例如：注册完成率提升 5 个百分点"
                  onChange={(event) => setImpact(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">风险</span>
                <input
                  value={risk}
                  placeholder="可能的代价和副作用"
                  onChange={(event) => setRisk(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">验证方式</span>
                <textarea
                  rows={2}
                  value={validation}
                  placeholder="上线后看哪个指标、看多久、达到多少算成功"
                  onChange={(event) => setValidation(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">优先级</span>
                <select value={priority} onChange={(event) => setPriority(event.target.value)}>
                  {PRIORITIES.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <div
                style={{
                  display: "flex",
                  gap: 8,
                  justifyContent: "flex-end",
                  marginTop: 12,
                  flexWrap: "wrap",
                }}
              >
                <button className="btn btn-subtle btn-sm" disabled={busy} onClick={prefill}>
                  用选定方案填充
                </button>
                <button
                  className="btn btn-primary"
                  disabled={busy || !title.trim() || !action.trim() || !validation.trim()}
                  onClick={() => void create()}
                >
                  保存为草稿
                </button>
              </div>
              {!validation.trim() && (
                <p style={{ color: "var(--muted)", fontSize: 13, textAlign: "right", marginTop: 8 }}>
                  验证方式必填：没有验证口径的决策没法复盘。
                </p>
              )}
            </section>

            {decisions.length > 0 && (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">决策记录</h2>
                    <div className="card-kicker">
                      共 {decisions.length} 条 · 已确认{" "}
                      {decisions.filter((item) => item.status === "approved").length} 条
                    </div>
                  </div>
                  <ShieldCheck size={17} color="#4a6cf7" />
                </div>
                <div className="list">
                  {decisions.map((decision) => (
                    <div className="card card-pad" key={decision.id} style={{ marginBottom: 10 }}>
                      <div className="card-head">
                        <div>
                          <strong>{decision.title || "未命名决策"}</strong>
                          <div className="card-kicker">
                            {decision.priority || "P2"} · v{decision.version ?? 1} ·{" "}
                            {formatWorkflowDate(decision.created_at)}
                          </div>
                        </div>
                        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                          <EvidenceStatus count={decision.evidence_json?.length || 0} />
                          <span
                            className={`tag ${decision.status === "approved" ? "tag-green" : decision.status === "rejected" ? "tag-rose" : "tag-amber"}`}
                          >
                            {decision.status || "draft"}
                          </span>
                        </div>
                      </div>
                      <p style={{ lineHeight: 1.6 }}>{decision.proposed_action || "没有动作描述。"}</p>
                      {decision.validation_plan && (
                        <p style={{ color: "var(--muted)", fontSize: 13 }}>
                          验证：{decision.validation_plan}
                        </p>
                      )}
                      {(decision.status === "draft" || decision.status === "rejected") && (
                        <button
                          className="btn btn-primary btn-sm"
                          disabled={busyId === decision.id}
                          onClick={() => void submit(decision.id)}
                        >
                          <Send size={13} />
                          提交并确认
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
                  <Link className="btn btn-primary btn-sm" href="/stage12-prd">
                    下一步·生成 PRD <ChevronRight size={13} />
                  </Link>
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
