/**
 * Navigation metadata for the refactored IA.
 *
 * The deterministic data layer (old stages 1-5) collapsed into the workbench:
 * upload -> auto analysis -> AI report happens on one page at "/".
 * The human-led decision chain (insight -> problem -> solution -> decision ->
 * PRD) keeps its ordered pages. `ai` marks where AI drafts require human
 * confirmation; `step` keeps the original numbering for the workflow gates.
 */
export const pipelineNavItems = [
  { href: "/stage6-insight", label: "洞察引擎", step: 6, icon: "lightbulb", ai: true, phase: "insight" },
  { href: "/stage7-copilot", label: "决策副驾", step: 7, icon: "sparkles", ai: true, phase: "insight" },
  { href: "/stage8-discussion", label: "人机讨论", step: 8, icon: "message", ai: true, phase: "insight" },
  { href: "/stage9-problem", label: "产品问题", step: 9, icon: "target", ai: true, phase: "decision" },
  { href: "/stage10-solution", label: "方案讨论", step: 10, icon: "route", ai: true, phase: "decision" },
  { href: "/stage11-decision", label: "产品决策", step: 11, icon: "gavel", ai: false, phase: "decision" },
  { href: "/stage12-prd", label: "PRD", step: 12, icon: "file", ai: true, phase: "decision" },
] as const;

/** The two bands shown as dividers in the sidebar. */
export const pipelinePhases = [
  { key: "insight", label: "洞察层 · AI 辅助" },
  { key: "decision", label: "决策层 · 人工主导" },
] as const;

export const utilityNavItems = [
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
  "/insights": "/stage6-insight",
  "/step4-insights": "/stage6-insight",
  "/feedback": "/stage6-insight",
  "/documents": "/stage12-prd",
  "/step5-deliver": "/stage12-prd",
  "/projects": "/",
};
