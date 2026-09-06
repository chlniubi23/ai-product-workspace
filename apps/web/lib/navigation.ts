/**
 * Navigation metadata for the refactored IA.
 *
 * The deterministic data layer (old stages 1-5) collapsed into the workbench:
 * upload -> auto analysis -> AI report happens on one page at "/".
 * The human-led decision chain (interview -> insight adjudication -> problem
 * -> solution -> decision -> PRD) keeps its ordered pages. `ai` marks where AI
 * drafts require human confirmation; `step` keeps the numbering for the
 * workflow gates. Batch 4 merged the old discussion stage into the interview,
 * so the pipeline has 11 stages.
 */
export const pipelineNavItems = [
  { href: "/stage6-interview", label: "AI 采访", step: 6, icon: "message", ai: true, phase: "insight" },
  { href: "/stage7-copilot", label: "决策副驾", step: 7, icon: "sparkles", ai: true, phase: "insight" },
  { href: "/stage8-problem", label: "产品问题", step: 8, icon: "target", ai: true, phase: "decision" },
  { href: "/stage9-solution", label: "方案讨论", step: 9, icon: "route", ai: true, phase: "decision" },
  { href: "/stage10-decision", label: "产品决策", step: 10, icon: "gavel", ai: false, phase: "decision" },
  { href: "/stage11-prd", label: "PRD", step: 11, icon: "file", ai: true, phase: "decision" },
] as const;

/** The two bands shown as on-line badges in the sidebar stepper (batch 24:
 * short labels ride the connector line; the long band names lived only here). */
export const pipelinePhases = [
  { key: "insight", label: "AI 辅助" },
  { key: "decision", label: "人工主导" },
] as const;

export const utilityNavItems = [
  { href: "/history", label: "历史", icon: "file" },
  { href: "/data", label: "数据", icon: "database" },
  { href: "/settings", label: "设置", icon: "settings" },
] as const;

export const workbenchNavItem = { href: "/", label: "工作台", icon: "layout" } as const;

export const navItems = [workbenchNavItem, ...pipelineNavItems, ...utilityNavItems] as const;

export type NavItem = (typeof navItems)[number];

/**
 * Gate metadata for the workflow pages. Steps 1-5 all live on the workbench
 * now; step 6+ keep their own routes.
 */
export const workflowSteps = [
  { number: 1, label: "工作台 · 数据与报告", href: "/", ai: false },
  { number: 2, label: "工作台 · 数据与报告", href: "/", ai: false },
  { number: 3, label: "工作台 · 数据与报告", href: "/", ai: false },
  { number: 4, label: "工作台 · 数据与报告", href: "/", ai: false },
  { number: 5, label: "工作台 · 数据与报告", href: "/", ai: false },
  ...pipelineNavItems.map((item) => ({ number: item.step, label: item.label, href: item.href, ai: item.ai })),
];

/** Old URLs resolve to their refactored destination. */
export const legacyRouteAliases: Record<string, string> = {
  "/workspace": "/",
  "/step1-data": "/",
  "/step2-quality": "/",
  "/analysis": "/",
  "/step3-analysis": "/",
  "/insights": "/stage6-interview",
  "/step4-insights": "/stage6-interview",
  "/feedback": "/stage6-interview",
  "/documents": "/stage11-prd",
  "/step5-deliver": "/stage11-prd",
  "/projects": "/",
  // Batch 4: the discussion stage merged into the interview; 9-12 renumbered 8-11.
  "/stage6-insight": "/stage6-interview",
  "/stage8-discussion": "/stage6-interview",
  "/stage9-problem": "/stage8-problem",
  "/stage10-solution": "/stage9-solution",
  "/stage11-decision": "/stage10-decision",
  "/stage12-prd": "/stage11-prd",
};
