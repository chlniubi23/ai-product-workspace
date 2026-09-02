"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import {
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
  workbenchNavItem,
  type NavItem,
} from "@/lib/navigation";
import { accessToken, apiRequest, clearSession } from "@/lib/api";
import {
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

  const currentNav = navItems.find(
    (item) =>
      item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`),
  );
  // /data hosts the dataset detail UI the workbench links into.
  const datasetRoute = pathname === "/data" || pathname.startsWith("/data/");
  const pageTitle = currentNav?.label ?? (datasetRoute ? "数据" : "工作台");
  const completion = workflow ? stepCompletion(workflow) : emptyCompletion();

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
          {/* The AI disclosure banner lives in WorkflowFrame right above the
              stage content it describes; a second global one here duplicated
              it on every AI step, so the shell renders none. */}
        </header>
        {children}
      </main>
    </div>
  );
}
