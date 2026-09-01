"use client";

import Link from "next/link";
import { ChevronRight, MessageSquare, Send, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate, type WorkflowInterviewQuestion } from "@/lib/workflow";

type CopilotMessage = {
  id: string;
  role?: string;
  content_json?: { content?: string; status?: string } | null;
  created_at?: string;
};

function messageText(message: CopilotMessage): string {
  return message.content_json?.content?.trim() || "（这条消息没有正文）";
}

export default function Stage6InterviewPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");
  const [roundNote, setRoundNote] = useState("");
  const [manualTopic, setManualTopic] = useState("");
  const [manualText, setManualText] = useState("");
  const [manualInfo, setManualInfo] = useState("");
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({});

  // 自由追问（原人机讨论的 Copilot 聊天）状态
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<CopilotMessage[]>([]);
  const [chatDraft, setChatDraft] = useState("");

  const questions = useMemo(
    () => [...(snapshot?.interviewQuestions || [])].sort((a, b) => (a.created_at || "").localeCompare(b.created_at || "")),
    [snapshot],
  );
  const answeredCount = questions.filter((q) => q.status === "answered").length;
  const pendingCount = questions.filter((q) => q.status === "pending").length;
  const projectId = snapshot?.activeDataset?.project_id;
  const existingSession = useMemo(
    () => (snapshot?.discussions || []).find((item) => (item.turn_count || 0) > 0) || snapshot?.discussions?.[0],
    [snapshot],
  );

  useEffect(() => {
    if (!sessionId && existingSession?.id) setSessionId(existingSession.id);
  }, [existingSession, sessionId]);

  useEffect(() => {
    if (!sessionId || !accessToken()) return;
    let cancelled = false;
    void (async () => {
      try {
        const data = await apiRequest<{ messages?: CopilotMessage[] }>(`/copilot/sessions/${sessionId}`);
        if (!cancelled) setMessages(data?.messages || []);
      } catch {
        if (!cancelled) setMessages([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, messages.length]);

  async function generateRound() {
    if (!projectId || !accessToken()) return;
    setBusy(true);
    setRoundNote("");
    try {
      const result = await apiRequest<{
        status?: string;
        round_number?: number;
        questions?: WorkflowInterviewQuestion[];
        duplicates_dropped?: number;
      }>(`/projects/${projectId}/interview/rounds`, { method: "POST" });
      if (result?.status === "succeeded" && (result.questions?.length || 0) > 0) {
        const dropped = result.duplicates_dropped || 0;
        setRoundNote(
          `第 ${result.round_number} 轮生成了 ${result.questions?.length} 个问题${dropped ? `（去重丢弃 ${dropped} 个重复）` : ""}。`,
        );
      } else {
        setRoundNote("AI 采访暂不可用，可先手动补充要点。");
      }
      await refresh();
    } catch (roundError) {
      setRoundNote(roundError instanceof Error ? roundError.message : "生成失败");
    } finally {
      setBusy(false);
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

  async function answerQuestion(id: string) {
    const text = (answerDrafts[id] || "").trim();
    if (!text || !accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      await apiRequest(`/interview-questions/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ answer_text: text }),
      });
      setAnswerDrafts((prev) => ({ ...prev, [id]: "" }));
      setNotice("已记录回答。");
      await refresh();
    } catch (answerError) {
      setNotice(answerError instanceof Error ? answerError.message : "回答失败");
    } finally {
      setBusyId("");
    }
  }

  async function skipQuestion(id: string) {
    if (!accessToken()) return;
    setBusyId(id);
    setNotice("");
    try {
      await apiRequest(`/interview-questions/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "skipped" }),
      });
      setNotice("已跳过。");
      await refresh();
    } catch (skipError) {
      setNotice(skipError instanceof Error ? skipError.message : "操作失败");
    } finally {
      setBusyId("");
    }
  }

  async function sendChat() {
    const text = chatDraft.trim();
    if (!text || !accessToken()) return;
    setBusy(true);
    setNotice("");
    try {
      const id = sessionId || (await ensureSession());
      await apiRequest(`/copilot/sessions/${id}/messages`, {
        method: "POST",
        body: JSON.stringify({
          content: text,
          context: { stage: "stage6-interview" },
        }),
      });
      setChatDraft("");
      setNotice("已发送。讨论结论要自己写进后续步骤。");
      await refresh();
    } catch (chatError) {
      setNotice(chatError instanceof Error ? chatError.message : "发送失败");
    } finally {
      setBusy(false);
    }
  }

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId;
    const workspaceId = snapshot?.workspaceId;
    if (!workspaceId) throw new Error("找不到工作区，请重新登录。");
    const created = await apiRequest<{ id: string }>("/copilot/sessions", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: workspaceId,
        project_id: snapshot?.activeDataset?.project_id || null,
        page_context: { stage: "stage6-interview" },
      }),
    });
    if (!created?.id) throw new Error("创建讨论会话失败。");
    setSessionId(created.id);
    return created.id;
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={6}
        title="AI 采访"
        description="AI 基于数据结论每轮提出关键问题，你逐个回答或跳过，也可手动补充信息。下半区可以自由追问。"
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
            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">采访进度</h2>
                  <div className="card-kicker">
                    共 {questions.length} 个问题 · 已回答 {answeredCount} · 待回答 {pendingCount}
                  </div>
                </div>
                <span className="tag tag-blue">AI 辅助</span>
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button className="btn btn-primary" disabled={busy} onClick={() => void generateRound()}>
                  <Sparkles size={14} />
                  {questions.length === 0 ? "开始采访" : "下一轮提问"}
                </button>
              </div>
              {roundNote && (
                <p style={{ color: "var(--muted)", marginTop: 8, marginBottom: 0 }}>{roundNote}</p>
              )}
            </section>

            {questions.length > 0 && (
              <section className="card card-pad" style={{ marginTop: 16 }}>
                <div className="card-head">
                  <div>
                    <h2 className="card-title">采访问题</h2>
                    <div className="card-kicker">回答会作为第 7 步生成洞察草稿的依据；跳过的问题不会进入蒸馏。</div>
                  </div>
                </div>
                <div className="list" style={{ marginTop: 8 }}>
                  {questions.map((question) => (
                    <div className="card card-pad" key={question.id} style={{ marginBottom: 10 }}>
                      <div className="card-head">
                        <div>
                          <strong>{question.question_text || "未命名问题"}</strong>
                          <div className="card-kicker">
                            {question.source === "manual" ? "手动补充" : `第 ${question.round_number} 轮`}
                            {question.topic ? ` · ${question.topic}` : ""} · {formatWorkflowDate(question.created_at)}
                          </div>
                        </div>
                        <span
                          className={`tag ${question.status === "answered" ? "tag-green" : question.status === "skipped" ? "tag-rose" : "tag-amber"}`}
                        >
                          {question.status === "answered" ? "已回答" : question.status === "skipped" ? "已跳过" : "待回答"}
                        </span>
                      </div>
                      {question.rationale && (
                        <p style={{ color: "var(--muted)", fontSize: 13, margin: "4px 0 0" }}>
                          为什么问这个：{question.rationale}
                        </p>
                      )}
                      {question.status === "answered" ? (
                        <p style={{ lineHeight: 1.6, margin: "8px 0 0", whiteSpace: "pre-wrap" }}>{question.answer_text}</p>
                      ) : question.status === "pending" ? (
                        <>
                          <textarea
                            rows={2}
                            style={{ marginTop: 8 }}
                            placeholder="写下你的回答…"
                            value={answerDrafts[question.id] || ""}
                            onChange={(event) =>
                              setAnswerDrafts((prev) => ({ ...prev, [question.id]: event.target.value }))
                            }
                          />
                          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 8 }}>
                            <button
                              className="btn btn-subtle btn-sm"
                              disabled={busyId === question.id}
                              onClick={() => void skipQuestion(question.id)}
                            >
                              跳过
                            </button>
                            <button
                              className="btn btn-primary btn-sm"
                              disabled={busyId === question.id || !(answerDrafts[question.id] || "").trim()}
                              onClick={() => void answerQuestion(question.id)}
                            >
                              提交回答
                            </button>
                          </div>
                        </>
                      ) : null}
                    </div>
                  ))}
                </div>
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
                <button className="btn btn-primary" disabled={busy || !manualText.trim()} onClick={() => void addManual()}>
                  补充要点
                </button>
              </div>
            </section>

            <section className="card card-pad" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2 className="card-title">自由追问</h2>
                  <div className="card-kicker">
                    与 AI 自由讨论数据结论 · 已有 {messages.length} 条消息
                  </div>
                </div>
                <span className="tag tag-blue">AI 辅助 · 可跳过</span>
              </div>
              <div className="list" style={{ marginTop: 8, marginBottom: 16 }}>
                {messages.length === 0 ? (
                  <p style={{ color: "var(--muted)" }}>
                    还没有对话。可以就采访问题背后的数据现象继续追问。
                  </p>
                ) : (
                  messages.map((message) => (
                    <div
                      className="card card-pad"
                      key={message.id}
                      style={{
                        marginBottom: 10,
                        background: message.role === "assistant" ? "var(--surface-2, #f7f8fb)" : "transparent",
                      }}
                    >
                      <div className="card-kicker" style={{ marginBottom: 6 }}>
                        {message.role === "assistant" ? "AI" : "我"}
                      </div>
                      <p style={{ lineHeight: 1.6, whiteSpace: "pre-wrap", margin: 0 }}>{messageText(message)}</p>
                    </div>
                  ))
                )}
              </div>
              <label className="field">
                <span className="field-label">提问</span>
                <textarea
                  rows={2}
                  value={chatDraft}
                  placeholder="例如：这些数据结论对采访方向有什么提示？"
                  onChange={(event) => setChatDraft(event.target.value)}
                />
              </label>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                <span style={{ color: "var(--muted)", fontSize: 13 }}>
                  AI 只能看到聚合产物和洞察，拿不到原始明细数据。
                </span>
                <button className="btn btn-primary" disabled={busy || !chatDraft.trim()} onClick={() => void sendChat()}>
                  <Send size={14} />
                  发送
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
      {notice && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
    </div>
  );
}
