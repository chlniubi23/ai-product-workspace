"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Database,
  FileText,
  Gavel,
  LayoutDashboard,
  Lightbulb,
  Menu,
  MessageSquare,
  Route,
  Settings,
  ShieldCheck,
  Sparkles,
  Target,
} from "lucide-react";
import {
  navItems,
  pipelineNavItems,
  pipelinePhases,
  utilityNavItems,
  workflowSteps,
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

  const currentNav = navItems.find((item) =>
    item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`),
  );
  // /data hosts the dataset detail UI the workbench links into.
  const datasetRoute = pathname === "/data" || pathname.startsWith("/data/");
  const pageTitle = currentNav?.label ?? (datasetRoute ? "数据" : "工作台");
  const completion = workflow ? stepCompletion(workflow) : emptyCompletion();

  const isActiveNav = (href: string) =>
    href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);

  // Batch 23: pure display derivation over the unchanged stepCompletion array.
  // The first incomplete stage is the pipeline frontier ("进行中"); everything
  // before it is done, everything after is pending.
  const firstIncomplete = completion.findIndex((value) => !value);
  const flowStatus = (step: number): FlowStatus => {
    const index = step - 1;
    return completion[index] ? "done" : index === firstIncomplete ? "current" : "pending";
  };
  // The workbench hosts stages 1-5 (batch 4 IA): per the design spec it shows
  // no status subtitle, but it still drives the workbench -> interview
  // connector, which turns brand once ALL of stages 1-5 are complete.
  const workbenchDone = firstIncomplete < 0 || firstIncomplete >= 5;
  // `phase` is lifted onto the entry so the group-label logic below needs no
  // `in`-narrowing over the const-union nav item types.
  const flowEntries: Array<{ item: NavItem; status: FlowStatus; phase?: string }> = [
    { item: workbenchNavItem, status: workbenchDone ? "done" : "current" },
    ...pipelineNavItems.map((item) => ({ item, status: flowStatus(item.step), phase: item.phase })),
  ];

  // Batch 24: progress card derivation -- all of it display-only over the
  // same completion array. `next` is the first incomplete stage AFTER the
  // frontier (out-of-order completion is possible since gates are advisory).
  const doneCount = completion.filter(Boolean).length;
  const progressPct = Math.round((doneCount / STAGE_COUNT) * 100);
  const currentStep = firstIncomplete >= 0 ? workflowSteps[firstIncomplete] : null;
  let nextIndex = -1;
  if (firstIncomplete >= 0) {
    for (let index = firstIncomplete + 1; index < completion.length; index += 1) {
      if (!completion[index]) {
        nextIndex = index;
        break;
      }
    }
  }
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
            on tall screens (wrapper flex:1, card top-aligned). */}
        <div className="flow-progress-wrap">
          {noActiveProject ? (
            <div className="flow-progress flow-progress-empty">
              <Link className="btn btn-primary btn-sm" href="/" onClick={() => setSidebarOpen(false)}>
                新建项目，开始第一次分析
              </Link>
            </div>
          ) : (
            <div className="flow-progress">
              <div className="flow-progress-head">
                <span>流水线进度</span>
                <strong>
                  {doneCount}/{STAGE_COUNT}
                </strong>
              </div>
              <div
                className="flow-progress-bar"
                role="progressbar"
                aria-valuenow={doneCount}
                aria-valuemin={0}
                aria-valuemax={STAGE_COUNT}
              >
                <span style={{ width: `${progressPct}%` }} />
              </div>
              {currentStep ? (
                <div className="flow-progress-row">当前 · {currentStep.label}</div>
              ) : (
                <div className="flow-progress-row">当前 · 已全部完成</div>
              )}
              {currentStep && nextIndex >= 0 && (
                <Link
                  className="flow-progress-next"
                  href={workflowSteps[nextIndex].href}
                  onClick={() => setSidebarOpen(false)}
                >
                  下一步 · {workflowSteps[nextIndex].label} →
                </Link>
              )}
              {!currentStep && (
                <Link className="flow-progress-next" href="/history" onClick={() => setSidebarOpen(false)}>
                  完成并归档 →
                </Link>
              )}
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
          {/* The AI disclosure banner lives in WorkflowFrame right above the
              stage content it describes; a second global one here duplicated
              it on every AI step, so the shell renders none. */}
        </header>
        {children}
      </main>
    </div>
  );
}
