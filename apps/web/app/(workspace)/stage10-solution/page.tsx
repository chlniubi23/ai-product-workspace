"use client";

import Link from "next/link";
import { ChevronRight, CircleCheck, Lightbulb, Plus, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate } from "@/lib/workflow";

const EFFORTS = ["S", "M", "L", "XL"];

function splitLines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

export default function Stage10SolutionPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [problemId, setProblemId] = useState("");
  const [title, setTitle] = useState("");
  const [approach, setApproach] = useState("");
  const [pros, setPros] = useState("");
  const [cons, setCons] = useState("");
  const [effort, setEffort] = useState("M");
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");

  const confirmedProblems = useMemo(
    () => (snapshot?.problems || []).filter((item) => item.status === "confirmed"),
    [snapshot],
  );
  const activeProblemId = problemId || confirmedProblems[0]?.id || "";
  const options = useMemo(
    () => (snapshot?.solutions || []).filter((item) => item.problem_id === activeProblemId),
    [snapshot, activeProblemId],
  );
  const selectedOption = options.find((item) => item.status === "selected");

  async function draftWithAi() {
    if (!activeProblemId || !accessToken()) return;
    setBusy(true);
    setNotice("");
    try {
      await apiRequest("/ai/propose-solutions", {
        method: "POST",
        body: JSON.stringify({ problem_id: activeProblemId, option_count: 3 }),
      });
      setNotice("AI 已给出候选方案思路，请择优手动录入为正式选项，或直接自己写。");
    } catch (draftError) {
      setNotice(draftError instanceof Error ? draftError.message : "AI 起草失败，可以手写。");
    } finally {
      setBusy(false);
    }
  }

  async function addOption() {
    if (!activeProblemId || !accessToken() || !title.trim() || !approach.trim()) return;
    setBusy(true);
    setNotice("");
    try {
      await apiRequest("/solutions", {
        method: "POST",
        body: JSON.stringify({
          problem_id: activeProblemId,
          title: title.trim(),
          approach: approach.trim(),
          pros: splitLines(pros),
          cons: splitLines(cons),
          effort,
        }),
      });
      setTitle("");
      setApproach("");
      setPros("");
      setCons("");
      setNotice("已添加候选方案。至少两个方案再做选择，避免只有一个选项的假比较。");
      await refresh();
    } catch (addError) {
      setNotice(addError instanceof Error ? addError.message : "添加失败");
    } finally {
      setBusy(false);
    }
  }

  async function select(id: string) {
    if (!accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      await apiRequest(`/solutions/${id}/select`, { method: "POST", body: JSON.stringify({}) });
      setNotice("已选定方案，其余候选自动标记为未采纳。");
      await refresh();
    } catch (selectError) {
      setNotice(selectError instanceof Error ? selectError.message : "操作失败");
    } finally {
      setBusyId("");
    }
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={10}
        title="方案讨论"
        description="针对已确认的问题列出多个候选方案，比较取舍后选定一个。选定动作会把其他方案标记为未采纳，留下比较痕迹。"
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
        {confirmedProblems.length === 0 ? (
          <section className="card empty-state">
            <Lightbulb size={20} />
            <strong>还没有已确认的问题</strong>
            <p>先在第 9 步确认一个问题。</p>
            <Link className="btn btn-primary btn-sm" href="/stage9-problem">
              前往第 9 步·问题定义 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <>
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">选择问题</h2>
                  <div className="card-kicker">已确认问题 {confirmedProblems.length} 条</div>
                </div>
                <span className="tag tag-blue">AI 辅助 · 可跳过</span>
              </div>
              <label className="field">
                <span className="field-label">当前讨论的问题</span>
                <select value={activeProblemId} onChange={(event) => setProblemId(event.target.value)}>
                  {confirmedProblems.map((problem) => (
                    <option key={problem.id} value={problem.id}>
                      {problem.title || problem.id.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </label>
            </section>

            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">候选方案</h2>
                  <div className="card-kicker">
                    共 {options.length} 个
                    {selectedOption ? " · 已选定" : options.length < 2 ? " · 建议至少 2 个" : ""}
                  </div>
                </div>
                <button className="btn btn-subtle btn-sm" disabled={busy} onClick={() => void draftWithAi()}>
                  <Sparkles size={13} />
                  AI 给思路
                </button>
              </div>

              {options.length === 0 ? (
                <p style={{ color: "var(--muted)" }}>还没有候选方案，先添加一个。</p>
              ) : (
                <div className="list">
                  {options.map((option) => (
                    <div
                      className="card card-pad"
                      key={option.id}
                      style={{
                        marginBottom: 10,
                        borderColor: option.status === "selected" ? "#2f9e6e" : undefined,
                      }}
                    >
                      <div className="card-head">
                        <div>
                          <strong>{option.title || "未命名方案"}</strong>
                          <div className="card-kicker">
                            工作量 {option.effort || "未评估"} · {formatWorkflowDate(option.created_at)}
                          </div>
                        </div>
                        <span
                          className={`tag ${option.status === "selected" ? "tag-green" : option.status === "rejected" ? "tag-rose" : "tag-amber"}`}
                        >
                          {option.status === "selected"
                            ? "已选定"
                            : option.status === "rejected"
                              ? "未采纳"
                              : "候选"}
                        </span>
                      </div>
                      <p style={{ lineHeight: 1.6 }}>{option.approach || "没有方案说明。"}</p>
                      {option.pros?.length || option.cons?.length ? (
                        <div className="grid grid-2" style={{ gap: 12 }}>
                          <div>
                            <div className="card-kicker">优点</div>
                            <ul style={{ margin: "4px 0 0", paddingLeft: 18, color: "var(--muted)" }}>
                              {(option.pros || []).map((item, index) => (
                                <li key={index}>{item}</li>
                              ))}
                            </ul>
                          </div>
                          <div>
                            <div className="card-kicker">代价</div>
                            <ul style={{ margin: "4px 0 0", paddingLeft: 18, color: "var(--muted)" }}>
                              {(option.cons || []).map((item, index) => (
                                <li key={index}>{item}</li>
                              ))}
                            </ul>
                          </div>
                        </div>
                      ) : null}
                      {!selectedOption && (
                        <button
                          className="btn btn-primary btn-sm"
                          style={{ marginTop: 12 }}
                          disabled={busyId === option.id}
                          onClick={() => void select(option.id)}
                        >
                          <CircleCheck size={13} />
                          选定这个方案
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">添加候选方案</h2>
                  <div className="card-kicker">优点和代价一行一条</div>
                </div>
              </div>
              <label className="field">
                <span className="field-label">方案名称</span>
                <input
                  value={title}
                  placeholder="例如：改为短信验证码兜底"
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">做法</span>
                <textarea
                  rows={3}
                  value={approach}
                  placeholder="具体怎么做，改哪里"
                  onChange={(event) => setApproach(event.target.value)}
                />
              </label>
              <div className="grid grid-2" style={{ gap: 12 }}>
                <label className="field">
                  <span className="field-label">优点</span>
                  <textarea
                    rows={3}
                    value={pros}
                    placeholder={"一行一条"}
                    onChange={(event) => setPros(event.target.value)}
                  />
                </label>
                <label className="field">
                  <span className="field-label">代价</span>
                  <textarea
                    rows={3}
                    value={cons}
                    placeholder={"一行一条"}
                    onChange={(event) => setCons(event.target.value)}
                  />
                </label>
              </div>
              <label className="field">
                <span className="field-label">工作量</span>
                <select value={effort} onChange={(event) => setEffort(event.target.value)}>
                  {EFFORTS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
                <button
                  className="btn btn-primary"
                  disabled={busy || !title.trim() || !approach.trim()}
                  onClick={() => void addOption()}
                >
                  <Plus size={14} />
                  添加方案
                </button>
                {selectedOption && (
                  <Link className="btn btn-subtle" href="/stage11-decision">
                    下一步·产品决策 <ChevronRight size={13} />
                  </Link>
                )}
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
