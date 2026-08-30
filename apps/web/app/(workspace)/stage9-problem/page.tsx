"use client";

import Link from "next/link";
import { ChevronRight, Check, Sparkles, Target } from "lucide-react";
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

export default function Stage9ProblemPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [title, setTitle] = useState("");
  const [statement, setStatement] = useState("");
  const [impact, setImpact] = useState("");
  const [priority, setPriority] = useState("P2");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");

  const confirmedInsights = useMemo(
    () => (snapshot?.insights || []).filter((item) => item.status === "confirmed"),
    [snapshot],
  );
  const problems = snapshot?.problems || [];
  const projectId = snapshot?.activeDataset?.project_id;

  function toggle(id: string) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  }

  async function draftWithAi() {
    if (!projectId || !accessToken()) return;
    setBusy(true);
    setNotice("");
    try {
      const result = await apiRequest<{
        status?: string;
        output?: {
          title?: string;
          statement?: string;
          impact_scope?: string;
          priority?: string;
          limitations?: string[];
        };
      }>("/ai/frame-problem", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          insight_ids: selected.length ? selected : confirmedInsights.map((item) => item.id),
          question: title.trim(),
        }),
      });
      if (result?.status === "succeeded" && result.output) {
        setTitle(result.output.title || "");
        setStatement(result.output.statement || "");
        setImpact(result.output.impact_scope || "");
        if (result.output.priority && PRIORITIES.includes(result.output.priority)) {
          setPriority(result.output.priority);
        }
        setNotice("AI 起草完成，这是草稿，请你改成自己的说法再保存。");
      } else {
        setNotice("AI 起草不可用，可手写。");
      }
      if (!selected.length) setSelected(confirmedInsights.map((item) => item.id));
    } catch (draftError) {
      setNotice(draftError instanceof Error ? draftError.message : "AI 起草失败，可以手写。");
    } finally {
      setBusy(false);
    }
  }

  async function save(status: "open" | "confirmed") {
    if (!projectId || !accessToken() || !title.trim() || !statement.trim()) return;
    setBusy(true);
    setNotice("");
    try {
      await apiRequest("/problems", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          title: title.trim(),
          statement: statement.trim(),
          impact_scope: impact.trim(),
          source_insight_ids: selected,
          priority,
          status,
        }),
      });
      setTitle("");
      setStatement("");
      setImpact("");
      setSelected([]);
      setNotice(status === "confirmed" ? "问题已确认，可以进入方案讨论。" : "已存为草稿。");
      await refresh();
    } catch (saveError) {
      setNotice(saveError instanceof Error ? saveError.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function confirmExisting(id: string) {
    if (!accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      await apiRequest(`/problems/${id}`, { method: "PATCH", body: JSON.stringify({ status: "confirmed" }) });
      setNotice("问题已确认。");
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
        step={9}
        title="问题定义"
        description="把讨论收敛成一句能被验证的问题陈述。确认问题必须引用至少一条已采纳洞察，避免凭感觉立项。"
        completion={completion}
        loading={loading || busy}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={9} completion={completion} loading={loading}>
        {confirmedInsights.length === 0 ? (
          <section className="card empty-state">
            <Target size={20} />
            <strong>还没有已采纳的洞察</strong>
            <p>问题定义必须建立在已采纳的洞察上。</p>
            <Link className="btn btn-primary btn-sm" href="/stage7-copilot">
              前往第 7 步·决策副驾 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <>
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">定义一个问题</h2>
                  <div className="card-kicker">先勾选证据，再写陈述。AI 起草可选。</div>
                </div>
                <EvidenceStatus count={selected.length} />
              </div>

              <div className="field">
                <span className="field-label">证据来源（已采纳洞察 {confirmedInsights.length} 条）</span>
                <div className="list">
                  {confirmedInsights.map((insight) => (
                    <label
                      key={insight.id}
                      style={{
                        display: "flex",
                        gap: 8,
                        alignItems: "flex-start",
                        padding: "8px 0",
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={selected.includes(insight.id)}
                        onChange={() => toggle(insight.id)}
                      />
                      <span>
                        <strong>{insight.title || "未命名洞察"}</strong>
                        <div className="card-kicker">{formatWorkflowDate(insight.created_at)}</div>
                      </span>
                    </label>
                  ))}
                </div>
              </div>

              <label className="field">
                <span className="field-label">问题标题</span>
                <input
                  value={title}
                  placeholder="例如：新用户在验证码环节大量流失"
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">问题陈述</span>
                <textarea
                  rows={3}
                  value={statement}
                  placeholder="谁、在什么场景下、遇到什么障碍、造成什么后果"
                  onChange={(event) => setStatement(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">影响范围</span>
                <input
                  value={impact}
                  placeholder="例如：占注册流量 18%，日均 240 人"
                  onChange={(event) => setImpact(event.target.value)}
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
                <button className="btn btn-subtle btn-sm" disabled={busy} onClick={() => void draftWithAi()}>
                  <Sparkles size={13} />
                  AI 起草
                </button>
                <button
                  className="btn btn-subtle"
                  disabled={busy || !title.trim() || !statement.trim()}
                  onClick={() => void save("open")}
                >
                  存为草稿
                </button>
                <button
                  className="btn btn-primary"
                  disabled={busy || !title.trim() || !statement.trim() || selected.length === 0}
                  onClick={() => void save("confirmed")}
                >
                  <Check size={14} />
                  确认问题
                </button>
              </div>
              {selected.length === 0 && (
                <p style={{ color: "var(--muted)", fontSize: 13, textAlign: "right", marginTop: 8 }}>
                  勾选至少一条洞察后才能确认。
                </p>
              )}
            </section>

            {problems.length > 0 && (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">已定义问题</h2>
                    <div className="card-kicker">
                      共 {problems.length} 条 · 已确认{" "}
                      {problems.filter((item) => item.status === "confirmed").length} 条
                    </div>
                  </div>
                </div>
                <div className="list">
                  {problems.map((problem) => (
                    <div className="card card-pad" key={problem.id} style={{ marginBottom: 10 }}>
                      <div className="card-head">
                        <div>
                          <strong>{problem.title || "未命名问题"}</strong>
                          <div className="card-kicker">
                            {problem.priority || "P2"} · {formatWorkflowDate(problem.created_at)}
                          </div>
                        </div>
                        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                          <EvidenceStatus count={problem.source_insight_ids?.length || 0} />
                          <span
                            className={`tag ${problem.status === "confirmed" ? "tag-green" : "tag-amber"}`}
                          >
                            {problem.status || "open"}
                          </span>
                        </div>
                      </div>
                      <p style={{ color: "var(--muted)", lineHeight: 1.6 }}>
                        {problem.statement || "没有陈述正文。"}
                      </p>
                      {problem.status !== "confirmed" && (problem.source_insight_ids?.length || 0) > 0 && (
                        <button
                          className="btn btn-primary btn-sm"
                          disabled={busyId === problem.id}
                          onClick={() => void confirmExisting(problem.id)}
                        >
                          <Check size={13} />
                          确认这条
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
                  <Link className="btn btn-primary btn-sm" href="/stage10-solution">
                    下一步·方案讨论 <ChevronRight size={13} />
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
