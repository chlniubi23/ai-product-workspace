"use client";

import Link from "next/link";
import { Check, ChevronRight, Gavel, Send, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  EvidenceStatus,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import {
  formatWorkflowDate,
  type WorkflowApproval,
  type WorkflowDecision,
} from "@/lib/workflow";

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
  const [rejectingId, setRejectingId] = useState("");
  const [rejectReason, setRejectReason] = useState("");

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
  const decisions = useMemo(() => snapshot?.decisions || [], [snapshot]);
  const projectId = snapshot?.activeDataset?.project_id;

  // Pending approvals that point at a known proposal; proposals of the active
  // project float to the top, everything else still shows.
  const pendingApprovals = useMemo(() => {
    const decisionsById = new Map(decisions.map((item) => [item.id, item]));
    return (snapshot?.approvals || [])
      .filter(
        (approval): approval is WorkflowApproval & { target_id: string } =>
          approval.target_type === "decision_proposal" &&
          !!approval.target_id &&
          decisionsById.has(approval.target_id),
      )
      .map((approval) => {
        const decision = decisionsById.get(approval.target_id) as WorkflowDecision;
        return { approval, decision };
      })
      .sort((left, right) => {
        const leftSameProject = left.decision.project_id === projectId ? 0 : 1;
        const rightSameProject = right.decision.project_id === projectId ? 0 : 1;
        return leftSameProject - rightSameProject;
      });
  }, [snapshot, decisions, projectId]);

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
      setNotice("决策已存为草稿，提交后进入待审批。");
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
      // Submitting only moves the proposal to pending_approval and creates the
      // approval request. Approving or rejecting is a separate, deliberate
      // action in the 待审批 section below -- the same account may do both.
      await apiRequest(`/decision-proposals/${id}/submit`, { method: "POST" });
      setNotice("决策已提交，等待审批");
      await refresh();
    } catch (submitError) {
      setNotice(submitError instanceof Error ? submitError.message : "提交失败");
    } finally {
      setBusyId("");
    }
  }

  async function approveRequest(approvalId: string, version: number | undefined) {
    if (!accessToken()) return;
    setBusyId(approvalId);
    setNotice("");
    try {
      await apiRequest(`/approval-requests/${approvalId}/approve`, {
        method: "POST",
        body: JSON.stringify({ version: version ?? 1, decision_note: "" }),
      });
      setNotice("决策已批准");
      await refresh();
    } catch (approvalError) {
      setNotice(approvalError instanceof Error ? approvalError.message : "审批操作失败");
    } finally {
      setBusyId("");
    }
  }

  function beginReject(approvalId: string) {
    setRejectingId(approvalId);
    setRejectReason("");
    setNotice("");
  }

  function cancelReject() {
    setRejectingId("");
    setRejectReason("");
  }

  async function rejectRequest(approvalId: string, version: number | undefined) {
    const reason = rejectReason.trim();
    if (!accessToken() || !reason) return;
    setBusyId(approvalId);
    setNotice("");
    try {
      await apiRequest(`/approval-requests/${approvalId}/reject`, {
        method: "POST",
        body: JSON.stringify({ version: version ?? 1, decision_note: reason }),
      });
      setRejectingId("");
      setRejectReason("");
      setNotice("决策已驳回，提案退回后可修改并重新提交。");
      await refresh();
    } catch (rejectError) {
      setNotice(rejectError instanceof Error ? rejectError.message : "审批操作失败");
    } finally {
      setBusyId("");
    }
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={10}
        title="产品决策"
        description="提交后进入待审批，批准或驳回（驳回必须写明理由）。同一账号可先提交再审批；用两个账号登录即可演示双人治理。"
        completion={completion}
        loading={loading || busy}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={10} completion={completion} loading={loading}>
        {!selectedSolution ? (
          <section className="card empty-state">
            <Gavel size={20} />
            <strong>还没有选定的方案</strong>
            <p>先在第 9 步选定一个方案。</p>
            <Link className="btn btn-primary btn-sm" href="/stage9-solution">
              前往第 9 步·方案讨论 <ChevronRight size={13} />
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
                      共 {decisions.length} 条 · 已批准{" "}
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
                          提交审批
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
                  <Link className="btn btn-primary btn-sm" href="/stage11-prd">
                    下一步·生成 PRD <ChevronRight size={13} />
                  </Link>
                </div>
              </section>
            )}

            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">待审批</h2>
                  <div className="card-kicker">
                    批准或驳回；驳回必须写明理由，提案在审批中被编辑会导致审批失效。
                  </div>
                </div>
                <ShieldCheck size={17} color="#4a6cf7" />
              </div>
              {pendingApprovals.length === 0 ? (
                <p style={{ color: "var(--muted)" }}>没有等待审批的决策提案。</p>
              ) : (
                <div className="list" style={{ marginTop: 8 }}>
                  {pendingApprovals.map(({ approval, decision }) => (
                    <div className="card card-pad" key={approval.id} style={{ marginBottom: 10 }}>
                      <div className="card-head">
                        <div>
                          <strong>{decision.title || "未命名决策"}</strong>
                          <div className="card-kicker">
                            v{approval.version ?? decision.version ?? 1} · 提交于{" "}
                            {formatWorkflowDate(approval.created_at)}
                            {approval.requested_by
                              ? ` · 提交人 ${String(approval.requested_by).slice(0, 8)}`
                              : ""}
                          </div>
                        </div>
                        <span className="tag tag-amber">待审批</span>
                      </div>
                      <p style={{ lineHeight: 1.6 }}>{decision.proposed_action || "没有动作描述。"}</p>
                      {decision.validation_plan && (
                        <p style={{ color: "var(--muted)", fontSize: 13 }}>验证：{decision.validation_plan}</p>
                      )}
                      {rejectingId === approval.id ? (
                        <div style={{ marginTop: 12 }}>
                          <label className="field">
                            <span className="field-label">驳回理由（必填）</span>
                            <textarea
                              rows={2}
                              value={rejectReason}
                              placeholder="写明为什么不批准，例如：验证口径无法复盘"
                              onChange={(event) => setRejectReason(event.target.value)}
                            />
                          </label>
                          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                            <button
                              className="btn btn-subtle btn-sm"
                              disabled={busyId === approval.id}
                              onClick={cancelReject}
                            >
                              取消
                            </button>
                            <button
                              className="btn btn-primary btn-sm"
                              disabled={busyId === approval.id || !rejectReason.trim()}
                              onClick={() => void rejectRequest(approval.id, approval.version)}
                            >
                              确认驳回
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div
                          style={{
                            display: "flex",
                            gap: 8,
                            marginTop: 12,
                            justifyContent: "flex-end",
                          }}
                        >
                          <button
                            className="btn btn-subtle btn-sm"
                            disabled={busyId === approval.id}
                            onClick={() => beginReject(approval.id)}
                          >
                            驳回
                          </button>
                          <button
                            className="btn btn-primary btn-sm"
                            disabled={busyId === approval.id}
                            onClick={() => void approveRequest(approval.id, approval.version)}
                          >
                            <Check size={13} />
                            批准
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
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
