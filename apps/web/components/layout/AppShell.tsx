"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import {
  Archive,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  Database,
  FileText,
  Gavel,
  LayoutDashboard,
  Lightbulb,
  Menu,
  MessageSquare,
  Route,
  Send,
  Settings,
  ShieldCheck,
  Sparkles,
  Target,
  X,
} from "lucide-react";
import {
  navItems,
  pipelineNavItems,
  pipelinePhases,
  utilityNavItems,
  workbenchNavItem,
  type NavItem,
} from "@/lib/navigation";
import { API_BASE, accessToken, apiRequest, clearSession } from "@/lib/api";
import {
  emptyCompletion,
  isAiStage,
  loadWorkflowSnapshot,
  stepCompletion,
  type WorkflowSnapshot,
} from "@/lib/workflow";

const iconMap = {
  layout: LayoutDashboard,
  database: Database,
  search: Database,
  shield: ShieldCheck,
  chart: Database,
  message: MessageSquare,
  sparkles: Sparkles,
  lightbulb: Lightbulb,
  target: Target,
  route: Route,
  gavel: Gavel,
  file: FileText,
  settings: Settings,
};

export type IconName = keyof typeof iconMap;

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  const Component = iconMap[name];
  return <Component size={size} strokeWidth={1.8} />;
}

type CopilotChatMessage = {
  id: string;
  role: "assistant" | "user";
  content?: string;
  structured?: CopilotStructuredAnswer;
  approvalRequired?: boolean;
};

type CopilotStructuredValue = string | number | boolean | Record<string, unknown>;
type CopilotStructuredAnswer = {
  summary?: CopilotStructuredValue;
  facts?: CopilotStructuredValue[];
  hypotheses?: CopilotStructuredValue[];
  recommendations?: CopilotStructuredValue[];
  limitations?: CopilotStructuredValue[];
  approval_required?: boolean;
  requires_approval?: boolean;
  [key: string]: unknown;
};

function parseStructuredAnswer(value: unknown): CopilotStructuredAnswer | undefined {
  let candidate = value;
  if (typeof candidate === "string") {
    const trimmed = candidate
      .trim()
      .replace(/^```(?:json)?\s*/i, "")
      .replace(/\s*```$/, "");
    try {
      candidate = JSON.parse(trimmed);
    } catch {
      return undefined;
    }
  }
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return undefined;
  const record = candidate as Record<string, unknown>;
  const hasStructuredFields = ["summary", "facts", "hypotheses", "recommendations", "limitations"].some(
    (key) => key in record,
  );
  return hasStructuredFields ? (record as CopilotStructuredAnswer) : undefined;
}

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (["string", "number", "boolean"].includes(typeof value)) return String(value);
  if (Array.isArray(value)) return value.map(valueText).filter(Boolean).join("；");
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const preferred = [
      "text",
      "content",
      "statement",
      "recommendation",
      "action",
      "description",
      "reason",
      "value",
      "title",
    ];
    const key = preferred.find((name) => record[name] !== undefined && record[name] !== null);
    if (key) return valueText(record[key]);
    return Object.entries(record)
      .filter(([name]) => !["requires_approval", "approval_required"].includes(name))
      .map(([name, item]) => `${name}: ${valueText(item)}`)
      .filter(Boolean)
      .join("；");
  }
  return "";
}

function hasApprovalRequirement(answer: CopilotStructuredAnswer): boolean {
  return (
    answer.approval_required === true ||
    answer.requires_approval === true ||
    answer.recommendations?.some(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        ((item as Record<string, unknown>).requires_approval === true ||
          (item as Record<string, unknown>).approval_required === true),
    ) === true
  );
}

function StructuredAnswerView({
  answer,
  approvalRequired,
}: {
  answer: CopilotStructuredAnswer;
  approvalRequired?: boolean;
}) {
  const sections = [
    { key: "facts", label: "事实", values: answer.facts, tone: "tag-blue" },
    { key: "hypotheses", label: "推断", values: answer.hypotheses, tone: "tag-amber" },
    { key: "recommendations", label: "建议", values: answer.recommendations, tone: "tag-green" },
    { key: "limitations", label: "限制", values: answer.limitations, tone: "tag-slate" },
  ] as const;
  const requiresApproval = approvalRequired || hasApprovalRequirement(answer);
  return (
    <div style={{ display: "grid", gap: 10 }}>
      {answer.summary !== undefined && (
        <div>
          <strong style={{ display: "block", color: "#394860", fontSize: 11, marginBottom: 4 }}>摘要</strong>
          <div>{valueText(answer.summary)}</div>
        </div>
      )}
      {sections.map((section) => {
        const values = (section.values || []).map(valueText).filter(Boolean);
        if (!values.length) return null;
        return (
          <div key={section.key}>
            <span className={`tag ${section.tone}`} style={{ marginBottom: 5 }}>
              {section.label}
            </span>
            <ul style={{ margin: 0, paddingLeft: 17 }}>
              {values.slice(0, 6).map((value, index) => (
                <li key={`${section.key}-${index}`} style={{ marginTop: index ? 4 : 0 }}>
                  {value}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      {requiresApproval && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            marginTop: 2,
            padding: "7px 8px",
            border: "1px solid #f0d49a",
            borderRadius: 6,
            background: "#fff9eb",
            color: "#8b5c08",
            fontSize: 10,
          }}
        >
          <span className="tag tag-amber">需要人工审批</span>
          <span>建议不会自动执行，需确认后才能进入决策流程。</span>
        </div>
      )}
    </div>
  );
}

const initialCopilotMessages: CopilotChatMessage[] = [
  {
    id: "welcome",
    role: "assistant",
    content: "我会基于当前工作空间中已确认的摘要、数据版本和分析产物回答问题，并把事实、推断和建议分开呈现。",
  },
];

export function AppShell({ children }: Readonly<{ children: React.ReactNode }>) {
  const pathname = usePathname();
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [copilotOpen, setCopilotOpen] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [identity, setIdentity] = useState({
    name: "",
    role: "",
    workspace: "",
    workspaceId: "",
  });
  const [copilotSessionId, setCopilotSessionId] = useState<string>();
  const [copilotMessages, setCopilotMessages] = useState<CopilotChatMessage[]>(initialCopilotMessages);
  const [workflow, setWorkflow] = useState<WorkflowSnapshot>();

  useEffect(() => {
    // The session marker cookie and the bearer token are separate stores, so a
    // cookie can outlive its token (expiry, cleared storage, private window).
    // Clearing the cookie here is what breaks the redirect loop.
    if (!accessToken()) {
      clearSession();
      router.replace("/login");
      return;
    }
    void apiRequest<{
      user?: { name?: string };
      workspaces?: Array<{ id?: string; name?: string; role?: string }>;
    }>("/me")
      .then((result) => {
        const workspace = result.workspaces?.[0];
        setIdentity({
          name: result.user?.name || "当前用户",
          role: workspace?.role === "owner" ? "Owner" : workspace?.role || "Viewer",
          workspace: workspace?.name || "当前工作空间",
          workspaceId: workspace?.id || "",
        });
        setAuthReady(true);
      })
      .catch(() => {
        clearSession();
        router.replace("/login");
      });
  }, [router]);

  // The shell stays mounted while the user moves through the pipeline. Keep
  // its progress in sync after uploads, reports, and confirmations instead of
  // showing the snapshot from the initial page load.
  useEffect(() => {
    if (!authReady) return;
    let active = true;
    void loadWorkflowSnapshot()
      .then((next) => {
        if (active) setWorkflow(next);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [authReady, pathname]);

  if (!authReady) {
    return (
      <div className="auth-loading" aria-live="polite">
        <Database size={18} />
        正在加载工作空间…
      </div>
    );
  }

  const currentNav = navItems.find(
    (item) =>
      item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`),
  );
  // /data hosts the dataset detail UI the workbench links into.
  const datasetRoute = pathname === "/data" || pathname.startsWith("/data/");
  const pageTitle = currentNav?.label ?? (datasetRoute ? "数据" : "工作台");
  const completion = workflow ? stepCompletion(workflow) : emptyCompletion();
  const aiSurface = currentNav && "step" in currentNav ? isAiStage(currentNav.step) : false;

  const renderNavItem = (item: NavItem) => {
    const isActive =
      item.href === "/"
        ? pathname === "/"
        : pathname === item.href || pathname.startsWith(`${item.href}/`);
    const step = "step" in item ? item.step : undefined;
    const complete = step ? completion[step - 1] : false;
    const showAiTreatment = "ai" in item && item.ai;
    return (
      <Link
        key={item.href}
        href={item.href}
        onClick={() => setSidebarOpen(false)}
        className={`nav-item ${isActive ? "active" : ""} ${showAiTreatment ? "nav-item-ai" : ""}`}
        aria-current={isActive ? "page" : undefined}
      >
        <Icon name={item.icon as IconName} />
        <span className="nav-item-copy">
          <span>{item.label}</span>
          {step && <small>第 {step} 步</small>}
        </span>
        {step && (
          <span
            className={`nav-state ${complete ? "complete" : "pending"}`}
            aria-label={complete ? "已完成" : "未完成"}
          >
            {complete ? <Check size={12} /> : <Circle size={10} />}
          </span>
        )}
      </Link>
    );
  };

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-brand">
          <div className="brand-mark">
            <Database size={17} />
          </div>
          <div>
            <div className="brand-name">AI Product Workspace</div>
            <div className="brand-caption">让证据连接到决策</div>
          </div>
        </div>
        <div className="nav-label">工作流</div>
        <nav className="nav-list" aria-label="主导航">
          {renderNavItem(workbenchNavItem)}
          {pipelinePhases.map((phase) => (
            <Fragment key={phase.key}>
              <div
                className={`nav-divider ${phase.key === "insight" ? "ai-divider" : ""}`}
                aria-hidden="true"
              >
                <span>{phase.label}</span>
              </div>
              {pipelineNavItems.filter((item) => item.phase === phase.key).map(renderNavItem)}
            </Fragment>
          ))}
          <div className="nav-divider" aria-hidden="true">
            <span>其他</span>
          </div>
          {utilityNavItems.map(renderNavItem)}
        </nav>
        <div className="sidebar-foot">
          <div className="workspace-select">
            <div>
              <small>当前工作空间</small>
              <strong>{identity.workspace}</strong>
            </div>
            <ChevronDown size={14} color="#8e9ab0" />
          </div>
          <div className="user-mini">
            <div className="avatar">{identity.name.slice(0, 2)}</div>
            <div>
              <p>{identity.name}</p>
              <small>{identity.role}</small>
            </div>
          </div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="topbar-left">
            <button className="mobile-menu" aria-label="打开导航" onClick={() => setSidebarOpen(true)}>
              <Menu size={19} />
            </button>
            <div className="breadcrumb">
              <span>{identity.workspace}</span>
              <ChevronRight size={13} />
              <strong>{pageTitle}</strong>
            </div>
          </div>
          <div className="topbar-actions">
            <button className="topbar-copilot" onClick={() => setCopilotOpen(true)}>
              <Sparkles size={15} />
              <span>AI 助手</span>
            </button>
          </div>
        </header>
        {aiSurface && (
          <div className="ai-disclosure" role="note">
            <Sparkles size={14} />
            以下内容由 AI 生成，需人工确认后才能使用
          </div>
        )}
        {children}
      </main>

      <>
        <div
          className={`copilot-overlay ${copilotOpen ? "open" : ""}`}
          onClick={() => setCopilotOpen(false)}
        />
        <aside className={`copilot-drawer ${copilotOpen ? "open" : ""}`} aria-label="AI 助手">
          <div className="copilot-head">
            <div className="copilot-title">
              <div className="spark">
                <Sparkles size={16} />
              </div>
              <div>
                <strong>AI 助手</strong>
                <small>基于当前工作空间的证据协作</small>
              </div>
            </div>
            <button className="icon-btn" aria-label="关闭 AI 助手" onClick={() => setCopilotOpen(false)}>
              <X size={16} />
            </button>
          </div>
          <div className="copilot-body">
            <div className="copilot-context">
              <strong>当前上下文</strong>
              <br />
              {identity.workspace} · {pageTitle}
              <br />
              <span>仅发送已选择的摘要、字段定义与分析产物，不发送原始整表或 API 密钥。</span>
            </div>
            {copilotMessages.map((message) =>
              message.role === "user" ? (
                <div className="copilot-message user" key={message.id}>
                  <div className="message-bubble">{message.content}</div>
                </div>
              ) : (
                <div className="copilot-message" key={message.id}>
                  <div className="message-avatar">
                    <Bot size={13} />
                  </div>
                  <div>
                    <div className="message-bubble">
                      {message.structured ? (
                        <StructuredAnswerView
                          answer={message.structured}
                          approvalRequired={message.approvalRequired}
                        />
                      ) : (
                        message.content
                      )}
                    </div>
                    <div className="copilot-sources">
                      <span className="copilot-source">
                        <ShieldCheck size={10} />
                        证据优先
                      </span>
                      <span className="copilot-source">
                        <Archive size={10} />
                        可追溯
                      </span>
                      {message.approvalRequired && (
                        <span
                          className="copilot-source"
                          style={{ color: "#8b5c08", background: "#fff4d8" }}
                        >
                          需要审批
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              ),
            )}
          </div>
          <CopilotComposer
            workspaceId={identity.workspaceId}
            sessionId={copilotSessionId}
            onSession={setCopilotSessionId}
            onMessage={(message) => setCopilotMessages((current) => [...current, message])}
            pathname={pathname}
          />
        </aside>
      </>
    </div>
  );
}

function CopilotComposer({
  workspaceId,
  sessionId,
  onSession,
  onMessage,
  pathname,
}: {
  workspaceId: string;
  sessionId?: string;
  onSession: (id: string) => void;
  onMessage: (message: CopilotChatMessage) => void;
  pathname: string;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const send = async () => {
    const question = value.trim();
    if (!question || busy) return;
    if (!workspaceId) {
      setNotice("当前工作空间尚未加载完成");
      return;
    }
    setValue("");
    setNotice("正在创建分析计划…");
    onMessage({ id: `user-${Date.now()}`, role: "user", content: question });
    setBusy(true);
    try {
      let activeSessionId = sessionId;
      if (!activeSessionId) {
        const session = await apiRequest<{ id: string }>("/copilot/sessions", {
          method: "POST",
          body: JSON.stringify({
            workspace_id: workspaceId,
            page_context: { pathname },
          }),
        });
        activeSessionId = session.id;
        onSession(activeSessionId);
      }
      const result = await apiRequest<{ run_id: string; status: string }>(
        `/copilot/sessions/${activeSessionId}/messages`,
        {
          method: "POST",
          body: JSON.stringify({ content: question, context: { pathname } }),
        },
      );
      const streamed = await streamCopilotRun(result.run_id);
      let answer = streamed.text;
      let structured = streamed.structured;
      let approvalRequired = streamed.approvalRequired;
      let run:
        | { answer?: unknown; structured_answer?: unknown; status?: string; error_code?: string }
        | undefined;
      if (!answer || !structured) {
        try {
          run = await apiRequest<typeof run>(`/copilot/runs/${result.run_id}`);
        } catch {
          /* The streamed result remains usable if the status lookup fails. */
        }
        structured = parseStructuredAnswer(run?.structured_answer) || structured;
        const runAnswer = typeof run?.answer === "string" ? run.answer : valueText(run?.answer);
        answer = answer || runAnswer;
      }
      structured = structured || parseStructuredAnswer(answer);
      approvalRequired = approvalRequired || (structured ? hasApprovalRequirement(structured) : false);
      if (!answer && !structured)
        answer =
          run?.status === "not_configured" ? "AI 助手尚未配置模型服务。" : "这次运行没有返回可展示的回答。";
      onMessage({
        id: `assistant-${result.run_id}`,
        role: "assistant",
        content: structured ? undefined : answer,
        structured,
        approvalRequired,
      });
      setNotice(
        result.status === "succeeded" ? "已完成并保存运行记录" : "已记录本次失败运行",
      );
    } catch (cause) {
      onMessage({
        id: `assistant-error-${Date.now()}`,
        role: "assistant",
        content: cause instanceof Error ? cause.message : "请求失败，请稍后重试。",
      });
      setNotice("请求失败");
    } finally {
      setBusy(false);
      window.setTimeout(() => setNotice(""), 2600);
    }
  };

  return (
    <div className="copilot-composer">
      <textarea
        aria-label="向 AI 助手提问"
        value={value}
        disabled={busy}
        onChange={(event) => setValue(event.target.value)}
        placeholder={busy ? "正在执行受控工具步骤…" : "描述你的问题，或选择一个分析任务…"}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void send();
          }
        }}
      />
      <button
        className="send-btn"
        aria-label="发送"
        disabled={busy || !value.trim()}
        onClick={() => void send()}
      >
        <Send size={15} />
      </button>
      {notice && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
    </div>
  );
}

async function streamCopilotRun(
  runId: string,
): Promise<{ text: string; structured?: CopilotStructuredAnswer; approvalRequired: boolean }> {
  const token = accessToken();
  if (!token) throw new Error("登录状态已失效，请重新登录。");
  const response = await fetch(`${API_BASE}/copilot/runs/${runId}/events`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok || !response.body) return { text: "", approvalRequired: false };
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answer = "";
  let approvalRequired = false;
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const eventLine = part.split("\n").find((line) => line.startsWith("event:"));
      const dataLine = part.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      try {
        const eventType = eventLine?.slice(6).trim();
        const data = JSON.parse(dataLine.slice(5).trim()) as { text?: unknown };
        if (eventType === "text.delta")
          answer += typeof data.text === "string" ? data.text : valueText(data.text);
        if (eventType === "approval.required") approvalRequired = true;
      } catch {
        // Ignore keepalive and malformed provider payloads; the run endpoint remains authoritative.
      }
    }
  }
  return { text: answer, structured: parseStructuredAnswer(answer), approvalRequired };
}
