"use client";

import Link from "next/link";
import { ChevronRight, MessageSquare, Sparkles } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate, type WorkflowInterviewQuestion } from "@/lib/workflow";

type InterviewSummary = {
  collected: string[];
  gaps: string[];
  ready_for: string;
};

export default function Stage6InterviewPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");
  const [answerDraft, setAnswerDraft] = useState("");
  const [summary, setSummary] = useState<InterviewSummary | null>(null);
  const [completeNote, setCompleteNote] = useState("");
  const [interviewEnded, setInterviewEnded] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [manualTopic, setManualTopic] = useState("");
  const [manualText, setManualText] = useState("");
  const [manualInfo, setManualInfo] = useState("");
  // Batch 20: askNext needs a visible loading state (one LLM call, 5-15s).
  // The question card shows "AI 正在构思下一个问题…" while this is set.
  const [askingNext, setAskingNext] = useState(false);
  const [lastFailed, setLastFailed] = useState(false);
  const askNextInFlight = useRef(false);
  // The question returned by next-question is rendered immediately from the
  // response; the snapshot refresh remains a background sync.
  const [liveQuestion, setLiveQuestion] = useState<WorkflowInterviewQuestion | null>(null);

  const questions = useMemo(
    () =>
      [...(snapshot?.interviewQuestions || [])].sort((a, b) =>
        (a.created_at || "").localeCompare(b.created_at || ""),
      ),
    [snapshot],
  );
  const answeredCount = questions.filter((q) => q.status === "answered").length;
  const current = questions.find((q) => q.status === "pending") || null;
  const history = questions.filter((q) => q.status !== "pending");
  const projectId = snapshot?.activeDataset?.project_id;

  // Batch 18/20: one question at a time.  After the user answers or skips,
  // the next question is fetched automatically (visible loading state, guarded
  // against concurrent re-entry); when the AI (or the cap) ends the interview,
  // the completion digest is fetched in the same flow.
  async function askNext() {
    if (!projectId || !accessToken() || askNextInFlight.current) return "stopped";
    askNextInFlight.current = true;
    setAskingNext(true);
    try {
      const result = await apiRequest<{
        status?: string;
        reason?: string;
        note?: string;
        question?: WorkflowInterviewQuestion;
      }>(`/projects/${projectId}/interview/next-question`, { method: "POST" });
      if (result?.status === "ok" && result.question) {
        setLiveQuestion(result.question);
        setInterviewEnded(false);
        setLastFailed(false);
        await refresh();
        return "asked";
      }
      if (result?.status === "complete") {
        setInterviewEnded(true);
        setLastFailed(false);
        setLiveQuestion(null);
        setCompleteNote(result.note || "");
        await refresh();
        const digest = await apiRequest<{ status?: string; summary?: InterviewSummary }>(
          `/projects/${projectId}/interview/complete`,
          { method: "POST" },
        );
        if (digest?.status === "ok" && digest.summary) setSummary(digest.summary);
        return "complete";
      }
      return result?.status || "failed";
    } finally {
      askNextInFlight.current = false;
      setAskingNext(false);
    }
  }

  async function startInterview() {
    if (!projectId || !accessToken()) return;
    setBusy(true);
    setNotice("");
    try {
      const outcome = await askNext();
      if (outcome === "not_configured" || outcome === "failed") {
        setNotice("AI 采访暂不可用，可先手动补充要点。");
      }
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "开始采访失败");
    } finally {
      setBusy(false);
    }
  }

  async function finishInterview() {
    if (!projectId || !accessToken()) return;
    setBusy(true);
    setNotice("");
    try {
      const digest = await apiRequest<{ status?: string; summary?: InterviewSummary; message?: string }>(
        `/projects/${projectId}/interview/complete`,
        { method: "POST" },
      );
      if (digest?.status === "ok" && digest.summary) {
        setSummary(digest.summary);
        setInterviewEnded(true);
      } else {
        setNotice(digest?.message || "小结生成暂不可用。");
      }
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "小结生成失败");
    } finally {
      setBusy(false);
    }
  }

  async function submitAnswer() {
    if (!current || !answerDraft.trim() || !accessToken()) return;
    setBusyId(current.id);
    setNotice("");
    try {
      await apiRequest(`/interview-questions/${current.id}`, {
        method: "PATCH",
        body: JSON.stringify({ answer_text: answerDraft.trim() }),
      });
      setAnswerDraft("");
      await refresh();
      await askNext();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "提交失败");
    } finally {
      setBusyId("");
    }
  }

  async function skipCurrent() {
    if (!current || !accessToken()) return;
    setBusyId(current.id);
    setNotice("");
    try {
      await apiRequest(`/interview-questions/${current.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "skipped" }),
      });
      await refresh();
      await askNext();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "操作失败");
    } finally {
      setBusyId("");
    }
  }

  async function addManual() {
    if (!projectId || !accessToken() || !manualText.trim()) return;
    setBusy(true);
    setNotice("");
    try {
      await apiRequest("/interview-questions", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          topic: manualTopic.trim(),
          question_text: manualText.trim(),
          answer_text: manualInfo.trim(),
        }),
      });
      setManualTopic("");
      setManualText("");
      setManualInfo("");
      setNotice("已补充要点。");
      await refresh();
    } catch (manualError) {
      setNotice(manualError instanceof Error ? manualError.message : "补充失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={6}
        title="AI 采访"
        description="一次一问、围绕数据发现、最多 10 问、可随时结束；结束时生成信息小结，带着明确依据进入下一步。"
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
        {!projectId ? (
          <section className="card empty-state">
            <MessageSquare size={20} />
            <strong>还没有项目</strong>
            <p>先在工作台上传数据并创建项目。</p>
            <Link className="btn btn-primary btn-sm" href="/">
              返回工作台 <ChevronRight size={13} />
            </Link>
          </section>
        ) : (
          <>
            {summary ? (
              <section className="card card-pad" style={{ marginTop: 16, borderColor: "#cfe3d4" }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">采访小结</h2>
                    <div className="card-kicker">带着这些依据进入第 7 步洞察蒸馏。</div>
                  </div>
                  <span className="tag tag-green">采访完成</span>
                </div>
                <div style={{ display: "grid", gap: 10, marginTop: 8 }}>
                  <div>
                    <strong>已收集</strong>
                    <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                      {summary.collected.map((item, index) => (
                        <li key={index} style={{ lineHeight: 1.6 }}>
                          {item}
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <strong>未覆盖</strong>
                    <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                      {summary.gaps.map((item, index) => (
                        <li key={index} style={{ lineHeight: 1.6 }}>
                          {item}
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <strong>下一步建议</strong>
                    <p style={{ margin: "4px 0 0", lineHeight: 1.6 }}>{summary.ready_for}</p>
                  </div>
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
                  <Link className="btn btn-primary btn-sm" href="/stage7-copilot">
                    前往决策副驾 <ChevronRight size={13} />
                  </Link>
                </div>
              </section>
            ) : (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">当前问题</h2>
                    <div className="card-kicker">
                      已回答 {answeredCount} · AI 一次只问一个问题，回答后自动追问
                    </div>
                  </div>
                  <span className="tag tag-blue">AI 辅助</span>
                </div>
                {askingNext ? (
                  <div className="empty-state" style={{ minHeight: 120 }} role="status">
                    <Sparkles size={18} className="animate-spin" />
                    <strong>AI 正在构思下一个问题…</strong>
                    <p>回答已记录；新问题基于报告发现与你的回答生成。</p>
                  </div>
                ) : current ? (
                  <div style={{ marginTop: 8 }}>
                    <strong style={{ fontSize: 15, lineHeight: 1.6, display: "block" }}>
                      {(liveQuestion && liveQuestion.id === current.id ? liveQuestion.question_text : null) ||
                        current.question_text}
                    </strong>
                    <div className="card-kicker" style={{ marginTop: 4 }}>
                      {current.source === "manual" ? "手动补充" : `第 ${current.round_number} 问`}
                      {current.topic ? ` · ${current.topic}` : ""}
                    </div>
                    {current.rationale && (
                      <p style={{ color: "var(--muted)", fontSize: 13, margin: "4px 0 0" }}>
                        为什么问这个：{current.rationale}
                      </p>
                    )}
                    <label className="field" style={{ marginTop: 10 }}>
                      <textarea
                        rows={3}
                        placeholder="写下你的回答…"
                        value={answerDraft}
                        onChange={(event) => setAnswerDraft(event.target.value)}
                      />
                    </label>
                    <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 8 }}>
                      <button
                        className="btn btn-subtle btn-sm"
                        disabled={busyId === current.id}
                        onClick={() => void skipCurrent()}
                      >
                        跳过
                      </button>
                      <button
                        className="btn btn-primary btn-sm"
                        disabled={busyId === current.id || !answerDraft.trim()}
                        onClick={() => void submitAnswer()}
                      >
                        提交回答
                      </button>
                    </div>
                  </div>
                ) : interviewEnded ? (
                  <div className="empty-state" style={{ minHeight: 120 }}>
                    <MessageSquare size={18} />
                    <strong>采访已结束</strong>
                    <p>{completeNote || "可手动补充要点，或前往第 7 步生成洞察草稿。"}</p>
                    {lastFailed && (
                      <button
                        className="btn btn-subtle btn-sm"
                        disabled={busy}
                        onClick={() => void startInterview()}
                      >
                        继续追问
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="empty-state" style={{ minHeight: 120 }}>
                    <MessageSquare size={18} />
                    <strong>还没有进行中的问题</strong>
                    <p>AI 会基于数据发现一次提一个问题，根据你的回答追问。</p>
                    <button
                      className="btn btn-primary btn-sm"
                      disabled={busy}
                      onClick={() => void startInterview()}
                    >
                      <Sparkles size={13} />
                      开始采访
                    </button>
                  </div>
                )}
                {notice && <p style={{ color: "var(--muted)", marginTop: 8, marginBottom: 0 }}>{notice}</p>}
                {questions.length > 0 && !summary && (
                  <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
                    <button
                      className="btn btn-subtle btn-sm"
                      disabled={busy}
                      onClick={() => void finishInterview()}
                    >
                      结束采访并生成小结
                    </button>
                  </div>
                )}
              </section>
            )}

            {history.length > 0 && (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">采访记录</h2>
                    <div className="card-kicker">
                      已回答 {answeredCount} · 跳过 {history.filter((q) => q.status === "skipped").length}
                    </div>
                  </div>
                  <button className="btn btn-subtle btn-sm" onClick={() => setShowHistory((v) => !v)}>
                    {showHistory ? "收起" : "展开"}
                  </button>
                </div>
                {showHistory && (
                  <div className="list" style={{ marginTop: 8 }}>
                    {history.map((question) => (
                      <div className="card card-pad" key={question.id} style={{ marginBottom: 10 }}>
                        <div className="card-head">
                          <div>
                            <strong>{question.question_text || "未命名问题"}</strong>
                            <div className="card-kicker">
                              {question.source === "manual" ? "手动补充" : `第 ${question.round_number} 问`}
                              {question.topic ? ` · ${question.topic}` : ""} ·{" "}
                              {formatWorkflowDate(question.created_at)}
                            </div>
                          </div>
                          <span
                            className={`tag ${question.status === "answered" ? "tag-green" : "tag-rose"}`}
                          >
                            {question.status === "answered" ? "已回答" : "已跳过"}
                          </span>
                        </div>
                        {question.status === "answered" && (
                          <p style={{ lineHeight: 1.6, margin: "8px 0 0", whiteSpace: "pre-wrap" }}>
                            {question.answer_text}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </section>
            )}

            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">手动补充</h2>
                  <div className="card-kicker">随时补充要点或信息，与采访回答同样进入第 7 步蒸馏。</div>
                </div>
              </div>
              <label className="field">
                <span className="field-label">要点（可选主题）</span>
                <input
                  value={manualTopic}
                  placeholder="例如：渠道活动"
                  onChange={(event) => setManualTopic(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">要点标题</span>
                <input
                  value={manualText}
                  placeholder="例如：华北活动期的事件量为什么翻倍？"
                  onChange={(event) => setManualText(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">补充信息（可选，填写即视为已回答）</span>
                <textarea
                  rows={2}
                  value={manualInfo}
                  placeholder="把你知道的信息写在这里"
                  onChange={(event) => setManualInfo(event.target.value)}
                />
              </label>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button
                  className="btn btn-primary"
                  disabled={busy || !manualText.trim()}
                  onClick={() => void addManual()}
                >
                  补充要点
                </button>
              </div>
            </section>

            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
              <Link className="btn btn-primary btn-sm" href="/stage7-copilot">
                下一步·决策副驾 <ChevronRight size={13} />
              </Link>
            </div>
          </>
        )}
      </WorkflowGate>
      {notice && !summary && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
    </div>
  );
}
