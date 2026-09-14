"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Database,
  KeyRound,
  FileText,
  Gavel,
  LayoutDashboard,
  Lightbulb,
  Menu,
  MessageSquare,
  Pencil,
  Route,
  Settings,
  ShieldCheck,
  Sparkles,
  Target,
  LogOut,
  UserRound,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useCountUp } from "@/components/hooks/useCountUp";
import {
  navItems,
  pipelineNavItems,
  pipelinePhases,
  utilityNavItems,
  workbenchNavItem,
  type NavItem,
} from "@/lib/navigation";
import { accessToken, apiRequest, clearSession } from "@/lib/api";
import {
  STAGE_COUNT,
  emptyCompletion,
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

/** Live sidebar status of one pipeline entry (batch 23). */
type FlowStatus = "done" | "current" | "pending";

/** Batch 30: the ring is an SVG circle with r=20; 2πr is the dash length that
 * maps 0-100% onto the visible arc. */
const FLOW_RING_CIRCUMFERENCE = 2 * Math.PI * 20;

export function AppShell({ children }: Readonly<{ children: React.ReactNode }>) {
  const pathname = usePathname();
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [identity, setIdentity] = useState({
    name: "",
    role: "",
    workspace: "",
    workspaceId: "",
  });
  const [workflow, setWorkflow] = useState<WorkflowSnapshot>();

  // Batch 30: the sidebar ring rolls to its new percentage.  Computed BEFORE
  // the authReady early return -- hooks must run on every render.
  const progressEarly = workflow
    ? Math.round((stepCompletion(workflow).filter(Boolean).length / STAGE_COUNT) * 100)
    : 0;
  const progressDisplay = useCountUp(progressEarly);

  // Batch 30: a reload that is still in flight must not start a second
  // concurrent one (event storms during uploads would otherwise pile up).
  const reloadInFlightRef = useRef(false);

  // Batch 26 user center: a small popover over the sidebar user block.
  const [userMenu, setUserMenu] = useState<"closed" | "menu" | "name" | "password">("closed");
  const [nameDraft, setNameDraft] = useState("");
  const [pwdCurrent, setPwdCurrent] = useState("");
  const [pwdNew, setPwdNew] = useState("");
  const [pwdConfirm, setPwdConfirm] = useState("");
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState("");
  const [profileNotice, setProfileNotice] = useState("");
  const userMenuRef = useRef<HTMLDivElement>(null);

  // Outside click / Esc closes the popover.
  useEffect(() => {
    if (userMenu === "closed") return;
    const onPointer = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setUserMenu("closed");
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setUserMenu("closed");
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [userMenu]);

  const applyProfile = (name: string) => setIdentity((current) => ({ ...current, name }));

  async function saveName() {
    const name = nameDraft.trim();
    if (!name || profileBusy) return;
    setProfileBusy(true);
    setProfileError("");
    setProfileNotice("");
    try {
      const result = await apiRequest<{ user?: { name?: string } }>("/me", {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      applyProfile(result.user?.name || name);
      setProfileNotice("昵称已更新");
      setUserMenu("menu");
    } catch (cause) {
      setProfileError(cause instanceof Error ? cause.message : "修改失败");
    } finally {
      setProfileBusy(false);
    }
  }

  async function savePassword() {
    if (profileBusy) return;
    if (pwdNew.length < 8 || pwdNew !== pwdConfirm) {
      setProfileError(pwdNew !== pwdConfirm ? "两次输入的新密码不一致" : "新密码至少 8 位");
      return;
    }
    setProfileBusy(true);
    setProfileError("");
    setProfileNotice("");
    try {
      await apiRequest("/me", {
        method: "PATCH",
        body: JSON.stringify({ current_password: pwdCurrent, new_password: pwdNew }),
      });
      setProfileNotice("密码已更新");
      setUserMenu("menu");
    } catch (cause) {
      setProfileError(cause instanceof Error ? cause.message : "修改失败");
    } finally {
      setProfileBusy(false);
    }
  }

  function logout() {
    clearSession();
    window.location.href = "/login";
  }

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

  // The shell stays mounted while the user moves through the pipeline, so its
  // progress must follow the data rather than the initial page load.  Three
  // triggers share one reload: navigation, the project/workflow change events
  // (uploads, confirmations, deletes), and a slow poll as the safety net for
  // pages that forget to notify -- skipped entirely while the tab is hidden.
  useEffect(() => {
    if (!authReady) return;
    let active = true;
    const reload = async () => {
      if (reloadInFlightRef.current) return;
      reloadInFlightRef.current = true;
      try {
        const next = await loadWorkflowSnapshot();
        if (active) setWorkflow(next);
      } catch {
        /* keep showing the previous snapshot on a transient failure */
      } finally {
        reloadInFlightRef.current = false;
      }
    };
    void reload();
    window.addEventListener("apw-project-changed", reload);
    window.addEventListener("apw-workflow-changed", reload);
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void reload();
    }, 10_000);
    return () => {
      active = false;
      window.removeEventListener("apw-project-changed", reload);
      window.removeEventListener("apw-workflow-changed", reload);
      window.clearInterval(timer);
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

  const currentNav = navItems.find((item) =>
    item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`),
  );
  // /data hosts the dataset detail UI the workbench links into.
  const datasetRoute = pathname === "/data" || pathname.startsWith("/data/");
  const pageTitle = currentNav?.label ?? (datasetRoute ? "数据" : "工作台");
  const completion = workflow ? stepCompletion(workflow) : emptyCompletion();

  const isActiveNav = (href: string) =>
    href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);

  // Batch 30: per-page status, independent of the pipeline frontier.  "done"
  // still comes from the gate; "current" means the user is ON that page (the
  // old derivation claimed the next incomplete stage was "进行中" even while
  // the user was still working on the stage-5.5 report inside the workbench).
  // Priority is done > current > pending: a finished page that the user is
  // looking at reads as 已完成, since "you are here" is already expressed by
  // the active highlight.
  const entryStatus = (done: boolean, href: string): FlowStatus => {
    if (done) return "done";
    return isActiveNav(href) ? "current" : "pending";
  };
  // The workbench hosts stages 1-5 (batch 4 IA): it reads as done only when
  // ALL of them are, and it still drives the workbench -> interview connector.
  const workbenchDone = completion.slice(0, 5).every(Boolean);
  // `phase` is lifted onto the entry so the group-label logic below needs no
  // `in`-narrowing over the const-union nav item types.
  const flowEntries: Array<{ item: NavItem; status: FlowStatus; phase?: string }> = [
    { item: workbenchNavItem, status: entryStatus(workbenchDone, workbenchNavItem.href) },
    ...pipelineNavItems.map((item) => ({
      item,
      status: entryStatus(completion[item.step - 1] ?? false, item.href),
      phase: item.phase,
    })),
  ];

  // Batch 30: the progress card is a ring over the same completion array --
  // the "x/11" figure, the frontier step line and the next-step link are gone.
  const doneCount = completion.filter(Boolean).length;
  const progressPct = Math.round((doneCount / STAGE_COUNT) * 100);
  const pipelineComplete = doneCount >= STAGE_COUNT;
  const noActiveProject = workflow !== undefined && !workflow.activeProject;

  // Utility entries keep the plain batch-22 treatment: no subtitle, no icon.
  const renderNavItem = (item: NavItem) => {
    const isActive = isActiveNav(item.href);
    return (
      <Link
        key={item.href}
        href={item.href}
        onClick={() => setSidebarOpen(false)}
        className={`nav-item ${isActive ? "active" : ""}`}
        aria-current={isActive ? "page" : undefined}
      >
        <Icon name={item.icon as IconName} />
        <span className="nav-item-copy">
          <span>{item.label}</span>
        </span>
      </Link>
    );
  };

  const renderFlowEntry = (item: NavItem, status: FlowStatus, showStatus: boolean) => {
    const isActive = isActiveNav(item.href);
    const showAiTreatment = "ai" in item && item.ai;
    return (
      <li className="flow-item">
        <Link
          href={item.href}
          onClick={() => setSidebarOpen(false)}
          className={`nav-item ${isActive ? "active" : ""} ${showAiTreatment ? "nav-item-ai" : ""}`}
          aria-current={isActive ? "page" : undefined}
        >
          <Icon name={item.icon as IconName} />
          <span className="nav-item-copy">
            <span>{item.label}</span>
            {showStatus && (
              <span className={`flow-status ${status}`}>
                {status === "done" ? "已完成" : status === "current" ? "进行中" : "未开始"}
              </span>
            )}
          </span>
          {showStatus && status === "done" && (
            <span className="flow-state-icon done" aria-hidden="true">
              <Check size={12} />
            </span>
          )}
          {showStatus && status === "current" && (
            <span className="flow-state-icon current" aria-label="进行中" />
          )}
        </Link>
      </li>
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
        <nav aria-label="工作流">
          <ol className="flow-nav">
            {flowEntries.map((entry, index) => {
              const item = entry.item;
              const phase = entry.phase ? pipelinePhases.find((p) => p.key === entry.phase) : undefined;
              const previousPhase = index > 0 ? flowEntries[index - 1].phase : undefined;
              const opensPhase = phase !== undefined && previousPhase !== phase.key;
              // Batch 24: the connector chain is NEVER broken now.  At a band
              // boundary the vertical line continues through the badge row and
              // the short-label badge rides beside it (batch 23 broke the line
              // at the decision band, which read as three separate lists).
              const connector =
                index > 0 ? (
                  <li
                    key={`connector-${item.href}`}
                    className={`flow-connector ${flowEntries[index - 1].status === "done" ? "done" : "pending"}`}
                    aria-hidden="true"
                  />
                ) : null;
              const badge =
                opensPhase && phase ? (
                  <li
                    className={`flow-phase ${flowEntries[index - 1].status === "done" ? "done" : "pending"}`}
                    key={`label-${phase.key}`}
                    aria-hidden="true"
                  >
                    <span className="flow-phase-badge">{phase.label}</span>
                  </li>
                ) : null;
              return (
                <Fragment key={item.href}>
                  {connector}
                  {badge}
                  {renderFlowEntry(item, entry.status, "step" in item)}
                </Fragment>
              );
            })}
          </ol>
        </nav>
        {/* Batch 24: the live progress card absorbs the leftover vertical space
            on tall screens (wrapper flex:1, card top-aligned).  Batch 30: the
            ring replaces the linear bar and the x/11 narrative; the copy next
            to it names the page the user is actually on. */}
        <div className="flow-progress-wrap">
          {noActiveProject ? (
            <div className="flow-progress flow-progress-empty">
              <Link className="btn btn-primary btn-sm" href="/" onClick={() => setSidebarOpen(false)}>
                新建项目，开始第一次分析
              </Link>
            </div>
          ) : (
            <div className="flow-progress">
              <div className="flow-progress-body">
                <div className="flow-progress-ring-wrap">
                  <svg
                    className="flow-progress-ring"
                    viewBox="0 0 48 48"
                    role="progressbar"
                    aria-valuenow={doneCount}
                    aria-valuemin={0}
                    aria-valuemax={STAGE_COUNT}
                    aria-label={`流水线进度 ${progressPct}%`}
                  >
                    <circle className="flow-progress-ring-track" cx="24" cy="24" r="20" />
                    <circle
                      className="flow-progress-ring-arc"
                      cx="24"
                      cy="24"
                      r="20"
                      strokeDasharray={FLOW_RING_CIRCUMFERENCE}
                      strokeDashoffset={
                        FLOW_RING_CIRCUMFERENCE * (1 - Math.min(Math.max(progressDisplay, 0), 100) / 100)
                      }
                    />
                  </svg>
                  <strong className="flow-progress-pct num">{Math.round(progressDisplay)}%</strong>
                </div>
                <div className="flow-progress-copy">
                  <div className="flow-progress-page">当前页面 · {pageTitle}</div>
                  {pipelineComplete && (
                    <Link className="flow-progress-next" href="/history" onClick={() => setSidebarOpen(false)}>
                      完成并归档 →
                    </Link>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
        <nav className="nav-list sidebar-utility" aria-label="工具">
          {utilityNavItems.map(renderNavItem)}
        </nav>
        <div className="sidebar-bottom">
          <div className="workspace-select">
            <div>
              <small>当前工作空间</small>
              <strong>{identity.workspace}</strong>
            </div>
            <ChevronDown size={14} />
          </div>
          <div ref={userMenuRef} style={{ position: "relative" }}>
            <button
              type="button"
              className="user-mini"
              style={{
                width: "100%",
                border: 0,
                background: "transparent",
                textAlign: "left",
                cursor: "pointer",
                padding: "12px 7px 0",
              }}
              aria-haspopup="menu"
              aria-expanded={userMenu !== "closed"}
              onClick={() => {
                setProfileError("");
                setProfileNotice("");
                setNameDraft(identity.name);
                setUserMenu(userMenu === "closed" ? "menu" : "closed");
              }}
            >
              <div className="avatar">{identity.name.slice(0, 2)}</div>
              <div>
                <p>{identity.name}</p>
                <small>{identity.role}</small>
              </div>
              <ChevronDown size={14} style={{ marginLeft: "auto", color: "var(--faint)" }} />
            </button>
            {userMenu !== "closed" && (
              <div
                role="menu"
                style={{
                  position: "absolute",
                  bottom: "100%",
                  left: 0,
                  right: 0,
                  marginBottom: 6,
                  background: "var(--panel)",
                  border: "1px solid var(--line)",
                  borderRadius: "var(--radius-lg)",
                  boxShadow: "var(--shadow-overlay)",
                  padding: 12,
                  display: "grid",
                  gap: 6,
                  zIndex: 50,
                }}
              >
                {profileError && (
                  <p className="form-error" style={{ margin: 0 }} role="alert">
                    {profileError}
                  </p>
                )}
                {profileNotice && (
                  <p style={{ margin: 0, fontSize: 12, color: "var(--success)" }} role="status">
                    {profileNotice}
                  </p>
                )}
                {userMenu === "menu" && (
                  <>
                    <UserMenuItem icon={UserRound} label="修改昵称" onClick={() => setUserMenu("name")} />
                    <UserMenuItem icon={KeyRound} label="修改密码" onClick={() => setUserMenu("password")} />
                    <UserMenuItem icon={LogOut} label="退出登录" onClick={logout} danger />
                  </>
                )}
                {userMenu === "name" && (
                  <>
                    <label style={{ display: "grid", gap: 4 }}>
                      <span style={{ fontSize: 12, color: "var(--muted)" }}>新昵称</span>
                      <input
                        value={nameDraft}
                        maxLength={120}
                        onChange={(event) => setNameDraft(event.target.value)}
                        style={{
                          height: 34,
                          border: "1px solid var(--line)",
                          borderRadius: "var(--radius)",
                          padding: "0 9px",
                        }}
                      />
                    </label>
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <button className="btn btn-subtle btn-sm" onClick={() => setUserMenu("menu")}>
                        返回
                      </button>
                      <button
                        className="btn btn-primary btn-sm"
                        disabled={profileBusy || !nameDraft.trim()}
                        onClick={() => void saveName()}
                      >
                        保存
                      </button>
                    </div>
                  </>
                )}
                {userMenu === "password" && (
                  <>
                    {(
                      [
                        ["当前密码", pwdCurrent, setPwdCurrent],
                        ["新密码（至少 8 位）", pwdNew, setPwdNew],
                        ["确认新密码", pwdConfirm, setPwdConfirm],
                      ] as Array<[string, string, (value: string) => void]>
                    ).map(([label, value, setter]) => (
                      <label key={label} style={{ display: "grid", gap: 4 }}>
                        <span style={{ fontSize: 12, color: "var(--muted)" }}>{label}</span>
                        <input
                          type="password"
                          value={value}
                          autoComplete="new-password"
                          onChange={(event) => setter(event.target.value)}
                          style={{
                            height: 34,
                            border: "1px solid var(--line)",
                            borderRadius: "var(--radius)",
                            padding: "0 9px",
                          }}
                        />
                      </label>
                    ))}
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <button className="btn btn-subtle btn-sm" onClick={() => setUserMenu("menu")}>
                        返回
                      </button>
                      <button
                        className="btn btn-primary btn-sm"
                        disabled={profileBusy || !pwdCurrent || !pwdNew || !pwdConfirm}
                        onClick={() => void savePassword()}
                      >
                        更新密码
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}
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
          {/* The AI disclosure banner lives in WorkflowFrame right above the
              stage content it describes; a second global one here duplicated
              it on every AI step, so the shell renders none. */}
        </header>
        {children}
      </main>
    </div>
  );
}

function UserMenuItem({
  icon,
  label,
  onClick,
  danger = false,
}: {
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  danger?: boolean;
}) {
  const IconComponent = icon;
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        height: 34,
        padding: "0 10px",
        borderRadius: "var(--radius-sm)",
        border: 0,
        background: "transparent",
        color: danger ? "var(--danger)" : "var(--ink)",
        fontSize: 13,
        cursor: "pointer",
      }}
      onMouseEnter={(event) => {
        event.currentTarget.style.background = "var(--fill)";
      }}
      onMouseLeave={(event) => {
        event.currentTarget.style.background = "transparent";
      }}
    >
      <IconComponent size={14} />
      {label}
    </button>
  );
}
