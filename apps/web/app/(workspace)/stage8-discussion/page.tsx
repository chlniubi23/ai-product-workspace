"use client";

import Link from "next/link";
import { ChevronRight, MessageSquare, Send } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiRequest, accessToken } from "@/lib/api";
import {
  WorkflowGate,
  WorkflowHeader,
  SnapshotMeta,
  useWorkflowSnapshot,
} from "@/components/workflow/WorkflowFrame";

type CopilotMessage = {
  id: string;
  role?: string;
  content_json?: { content?: string; status?: string } | null;
  created_at?: string;
};

function messageText(message: CopilotMessage): string {
  return message.content_json?.content?.trim() || "（这条消息没有正文）";
}

export default function Stage8DiscussionPage() {
  const { snapshot, loading, error, completion, refresh } = useWorkflowSnapshot();
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<CopilotMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const confirmedInsights = useMemo(
    () => (snapshot?.insights || []).filter((item) => item.status === "confirmed"),
    [snapshot],
  );
  const existing = useMemo(
    () =>
      (snapshot?.discussions || []).find((item) => (item.turn_count || 0) > 0) || snapshot?.discussions?.[0],
    [snapshot],
  );

  useEffect(() => {
    if (!sessionId && existing?.id) setSessionId(existing.id);
  }, [existing, sessionId]);

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
  }, [sessionId, busy]);

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId;
    const workspaceId = snapshot?.workspaceId;
    if (!workspaceId) throw new Error("找不到工作区，请重新登录。");
    const created = await apiRequest<{ id: string }>("/copilot/sessions", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: workspaceId,
        project_id: snapshot?.activeDataset?.project_id || null,
        page_context: { stage: "stage8-discussion" },
      }),
    });
    if (!created?.id) throw new Error("创建讨论会话失败。");
    setSessionId(created.id);
    return created.id;
  }

  async function send() {
    const question = draft.trim();
    if (!question || !accessToken()) return;
    setBusy(true);
    setNotice("");
    try {
      const id = await ensureSession();
      await apiRequest(`/copilot/sessions/${id}/messages`, {
        method: "POST",
        body: JSON.stringify({
          content: question,
          context: { insight_ids: confirmedInsights.map((item) => item.id) },
        }),
      });
      setDraft("");
      setNotice("已记录一轮讨论。讨论结论需要你自己写进第 9 步的问题定义。");
      await refresh();
    } catch (sendError) {
      setNotice(sendError instanceof Error ? sendError.message : "发送失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <WorkflowHeader
        step={8}
        title="人机讨论"
        description="就已采纳的洞察和 AI 对话，把模糊的现象聊成一个能定义的问题。对话只作为思考记录，不会自动写入后续步骤。"
        completion={completion}
        loading={loading || busy}
      />
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <WorkflowGate step={8} completion={completion} loading={loading}>
        <section className="card card-pad" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2 className="card-title">讨论区</h2>
              <div className="card-kicker">
                已采纳洞察 {confirmedInsights.length} 条会作为对话上下文 · 已有 {messages.length} 条消息
              </div>
            </div>
            <span className="tag tag-blue">AI 辅助 · 可跳过</span>
          </div>

          {confirmedInsights.length === 0 ? (
            <div className="empty-state">
              <MessageSquare size={20} />
              <strong>还没有已采纳的洞察</strong>
              <p>先在第 7 步采纳至少一条洞察。</p>
              <Link className="btn btn-primary btn-sm" href="/stage7-copilot">
                前往第 7 步·决策副驾 <ChevronRight size={13} />
              </Link>
            </div>
          ) : (
            <>
              <div className="list" style={{ marginBottom: 16 }}>
                {messages.length === 0 ? (
                  <p style={{ color: "var(--muted)" }}>
                    还没有对话。可以先问：这些洞察背后最可能的共同原因是什么？
                  </p>
                ) : (
                  messages.map((message) => (
                    <div
                      className="card card-pad"
                      key={message.id}
                      style={{
                        marginBottom: 10,
                        background:
                          message.role === "assistant" ? "var(--surface-2, #f7f8fb)" : "transparent",
                      }}
                    >
                      <div className="card-kicker" style={{ marginBottom: 6 }}>
                        {message.role === "assistant" ? "AI" : "我"}
                      </div>
                      <p style={{ lineHeight: 1.6, whiteSpace: "pre-wrap", margin: 0 }}>
                        {messageText(message)}
                      </p>
                    </div>
                  ))
                )}
              </div>

              <label className="field">
                <span className="field-label">提问</span>
                <textarea
                  rows={3}
                  value={draft}
                  placeholder="例如：留存下滑集中在哪类用户？我该先验证哪个假设？"
                  onChange={(event) => setDraft(event.target.value)}
                />
              </label>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginTop: 12,
                  gap: 12,
                  flexWrap: "wrap",
                }}
              >
                <span style={{ color: "var(--muted)", fontSize: 13 }}>
                  AI 只能看到聚合产物和洞察，拿不到原始明细数据。
                </span>
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    className="btn btn-primary"
                    disabled={busy || !draft.trim()}
                    onClick={() => void send()}
                  >
                    <Send size={14} />
                    {busy ? "发送中…" : "发送"}
                  </button>
                  <Link className="btn btn-subtle btn-sm" href="/stage9-problem">
                    下一步·问题定义 <ChevronRight size={13} />
                  </Link>
                </div>
              </div>
            </>
          )}
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
