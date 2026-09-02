import { apiRequest, pagedItems } from "@/lib/api";
import { pipelineNavItems } from "@/lib/navigation";

export type WorkflowProject = {
  id: string;
  name?: string;
  goal_statement?: string;
  description?: string;
  created_at?: string;
};

export type WorkflowColumn = {
  id?: string;
  name?: string;
  display_name?: string;
  inferred_type?: string;
  confirmed_type?: string;
  mapping_role?: string;
  nullable?: boolean;
  unique_ratio?: number;
};

export type WorkflowQualityReport = {
  id?: string;
  status?: string;
  overall_score?: number;
  summary_json?: Record<string, unknown>;
  created_at?: string;
};

export type WorkflowVersion = {
  id: string;
  version_number?: number;
  row_count?: number;
  column_count?: number;
  status?: string;
  created_at?: string;
  file_name?: string;
  columns?: WorkflowColumn[];
  quality_report?: WorkflowQualityReport | null;
  schema_reviewed_at?: string | null;
  schema_auto_accepted_at?: string | null;
};

export type WorkflowDataset = {
  id: string;
  name?: string;
  project_id?: string;
  created_at?: string;
  versions?: WorkflowVersion[];
};

export type WorkflowAnalysisRun = {
  id: string;
  project_id?: string;
  dataset_version_id?: string;
  analysis_type?: string;
  status?: string;
  created_at?: string;
  artifacts?: Array<Record<string, unknown>>;
  result_summary?: Record<string, unknown>;
};

export type WorkflowInsight = {
  id: string;
  project_id?: string;
  title?: string;
  content?: string;
  insight_type?: string;
  status?: string;
  confidence?: string;
  evidence_json?: unknown[];
  created_at?: string;
};

export type WorkflowDocumentVersion = {
  id?: string;
  version_number?: number;
  created_at?: string;
  content_markdown?: string;
};

export type WorkflowDocument = {
  id: string;
  project_id?: string;
  title?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  current_version?: WorkflowDocumentVersion | null;
  versions?: WorkflowDocumentVersion[];
};

export type WorkflowProblem = {
  id: string;
  project_id?: string;
  title?: string;
  statement?: string;
  impact_scope?: string;
  priority?: string;
  status?: string;
  source_insight_ids?: string[];
  created_at?: string;
};

export type WorkflowSolution = {
  id: string;
  problem_id?: string;
  title?: string;
  approach?: string;
  pros?: string[];
  cons?: string[];
  effort?: string;
  status?: string;
  reject_reason?: string;
  created_at?: string;
};

export type WorkflowDecision = {
  id: string;
  project_id?: string;
  title?: string;
  problem_statement?: string;
  proposed_action?: string;
  validation_plan?: string;
  status?: string;
  priority?: string;
  version?: number;
  evidence_json?: unknown[];
  created_at?: string;
};


export type WorkflowInterviewQuestion = {
  id: string;
  project_id?: string;
  round_number?: number;
  topic?: string;
  question_text?: string;
  rationale?: string;
  status?: string;
  answer_text?: string;
  source?: string;
  created_at?: string;
  answered_at?: string;
};

/** Pending approval request; the backend only ever lists status=pending rows. */
export type WorkflowApproval = {
  id: string;
  target_type?: string;
  target_id?: string;
  status?: string;
  version?: number;
  requested_by?: string;
  created_at?: string;
};

export type WorkflowSnapshot = {
  projects: WorkflowProject[];
  datasets: WorkflowDataset[];
  analysisRuns: WorkflowAnalysisRun[];
  insights: WorkflowInsight[];
  documents: WorkflowDocument[];
  problems: WorkflowProblem[];
  solutions: WorkflowSolution[];
  decisions: WorkflowDecision[];
  approvals: WorkflowApproval[];
  interviewQuestions: WorkflowInterviewQuestion[];
  activeDataset?: WorkflowDataset;
  activeVersion?: WorkflowVersion;
  workspaceId: string;
  loadErrors: string[];
  loadedAt: string;
};

/** Number of pipeline stages; keep in sync with pipelineNavItems. */
/** Batch 4 merged the discussion stage into the interview; 11 stages since. */
export const STAGE_COUNT = 11;
export type StageCompletion = boolean[];

/** All stages incomplete; used before the first snapshot arrives. */
export function emptyCompletion(): StageCompletion {
  return new Array(STAGE_COUNT).fill(false);
}

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as UnknownRecord) : {};
}

function asArray<T>(value: unknown): T[] {
  return pagedItems<T>(value);
}

function timeValue(value?: string): number {
  if (!value) return 0;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function sortedVersions(dataset?: WorkflowDataset): WorkflowVersion[] {
  return [...(dataset?.versions || [])].sort(
    (left, right) => (left.version_number || 0) - (right.version_number || 0),
  );
}

export function latestVersion(dataset?: WorkflowDataset): WorkflowVersion | undefined {
  return sortedVersions(dataset).at(-1);
}

export function latestDataset(datasets: WorkflowDataset[]): WorkflowDataset | undefined {
  return [...datasets]
    .sort((left, right) => {
      const leftVersion = latestVersion(left);
      const rightVersion = latestVersion(right);
      return (
        Math.max(timeValue(left.created_at), timeValue(leftVersion?.created_at)) -
        Math.max(timeValue(right.created_at), timeValue(rightVersion?.created_at))
      );
    })
    .at(-1);
}

function hasReviewedSchema(version?: WorkflowVersion): boolean {
  // Stage 2 is an advisory review, not a mapping requirement.  The workbench
  // upload accepts the inferred roles automatically (``schema_auto_accepted_at``)
  // so the pipeline never deadlocks on a cleaned business table that has no
  // user/event/time columns at all.  A manual review still sets
  // ``schema_reviewed_at``; this gate accepts either.
  return Boolean(version?.id && (version.schema_reviewed_at || version.schema_auto_accepted_at));
}

/** Uploading a file completes stage 1; reviewing (or skipping) the schema completes stage 2. */
export function stepCompletion(snapshot: WorkflowSnapshot): StageCompletion {
  const version = snapshot.activeVersion;
  const projectId = snapshot.activeDataset?.project_id;
  const succeededRun = snapshot.analysisRuns.find(
    (run) =>
      run.status === "succeeded" &&
      (!version || run.dataset_version_id === version.id || run.project_id === projectId),
  );

  const stage1 = Boolean(snapshot.activeDataset && version?.id);
  const stage2 = Boolean(version && hasReviewedSchema(version));
  const stage3 = Boolean(version?.quality_report);
  const stage4 = Boolean(succeededRun);
  const stage5 = Boolean(succeededRun && (succeededRun.artifacts?.length || succeededRun.result_summary));
  // Stage 6 (AI interview) completes once an answer is on record -- an
  // answered AI question or any manual supplement counts.
  const stage6 = snapshot.interviewQuestions.some(
    (question) => question.status === "answered" || question.source === "manual",
  );
  const stage7 = snapshot.insights.some((insight) => insight.status === "confirmed");
  const stage8 = snapshot.problems.some((problem) => problem.status === "confirmed");
  const stage9 = snapshot.solutions.some((solution) => solution.status === "selected");
  const stage10 = snapshot.decisions.some((decision) => decision.status === "approved");
  const stage11 = snapshot.documents.some(
    (document) => (document.versions?.length || 0) > 0 || Boolean(document.current_version),
  );

  return [stage1, stage2, stage3, stage4, stage5, stage6, stage7, stage8, stage9, stage10, stage11];
}

export function firstIncompleteStep(snapshot: WorkflowSnapshot): number {
  const completion = stepCompletion(snapshot);
  const index = completion.findIndex((value) => !value);
  return index < 0 ? STAGE_COUNT : index + 1;
}

/** Derived from the `ai` flag in pipelineNavItems so the two cannot drift apart. */
export function isAiStage(step: number): boolean {
  return pipelineNavItems.some((item) => item.step === step && item.ai);
}

export function formatWorkflowDate(value?: string): string {
  if (!value) return "暂无时间";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(timestamp);
}

async function requestList<T>(path: string): Promise<T[]> {
  // The backend page_params caps page_size at 100. Without this, the default
  // page of 20 hides older rows once records accumulate and the stage 6-12
  // gates silently lose sight of them.
  const separator = path.includes("?") ? "&" : "?";
  const payload = await apiRequest<unknown>(`${path}${separator}page_size=100`);
  return asArray<T>(payload);
}

async function hydrateActiveVersion(
  dataset: WorkflowDataset | undefined,
): Promise<WorkflowVersion | undefined> {
  const version = latestVersion(dataset);
  if (!version?.id) return undefined;
  let hydrated: WorkflowVersion = { ...version };
  const tasks: Array<Promise<void>> = [];
  if (!hydrated.columns?.length) {
    tasks.push(
      apiRequest<unknown>(`/dataset-versions/${version.id}/schema`)
        .then((payload) => {
          const record = asRecord(payload);
          hydrated = { ...hydrated, columns: asArray<WorkflowColumn>(record.columns) };
        })
        .catch(() => undefined),
    );
  }
  if (!hydrated.quality_report) {
    tasks.push(
      apiRequest<WorkflowQualityReport>(`/dataset-versions/${version.id}/quality-report`)
        .then((report) => {
          hydrated = { ...hydrated, quality_report: report };
        })
        .catch(() => undefined),
    );
  }
  await Promise.all(tasks);
  return hydrated;
}

/** Load only persisted records used by the twelve workflow gates. */
export async function loadWorkflowSnapshot(): Promise<WorkflowSnapshot> {
  const loadErrors: string[] = [];
  const [
    projectsResult,
    datasetsResult,
    runsResult,
    insightsResult,
    documentsResult,
    problemsResult,
    solutionsResult,
    decisionsResult,
    approvalsResult,
    interviewQuestionsResult,
    meResult,
  ] = await Promise.allSettled([
    requestList<WorkflowProject>("/projects"),
    requestList<WorkflowDataset>("/datasets"),
    requestList<WorkflowAnalysisRun>("/analysis-runs"),
    requestList<WorkflowInsight>("/insights"),
    requestList<WorkflowDocument>("/documents"),
    requestList<WorkflowProblem>("/problems"),
    requestList<WorkflowSolution>("/solutions"),
    requestList<WorkflowDecision>("/decision-proposals"),
    // The endpoint itself only returns status=pending rows; requestList
    // appends page_size=100.
    requestList<WorkflowApproval>("/approval-requests"),
    requestList<WorkflowInterviewQuestion>("/interview-questions"),
    apiRequest<unknown>("/me"),
  ]);
  const read = <T>(result: PromiseSettledResult<T[]>, label: string): T[] => {
    if (result.status === "fulfilled") return result.value;
    loadErrors.push(`${label}：${result.reason instanceof Error ? result.reason.message : "加载失败"}`);
    return [];
  };
  const projects = read(projectsResult, "项目");
  const datasets = read(datasetsResult, "数据集");
  const analysisRuns = read(runsResult, "分析运行");
  const insights = read(insightsResult, "洞察");
  const documents = read(documentsResult, "文档");
  const problems = read(problemsResult, "产品问题");
  const solutions = read(solutionsResult, "候选方案");
  const decisions = read(decisionsResult, "产品决策");
  const approvals = read(approvalsResult, "待审批");
  const interviewQuestions = read(interviewQuestionsResult, "采访问题");
  const activeDataset = latestDataset(datasets);
  const activeVersion = await hydrateActiveVersion(activeDataset);
  const workspaceId =
    meResult.status === "fulfilled"
      ? String(asArray<{ id?: string }>(asRecord(meResult.value).workspaces)[0]?.id || "")
      : "";
  return {
    projects,
    datasets,
    analysisRuns,
    insights,
    documents,
    problems,
    solutions,
    decisions,
    approvals,
    interviewQuestions,
    activeDataset,
    activeVersion,
    workspaceId,
    loadErrors,
    loadedAt: new Date().toISOString(),
  };
}

export function projectName(projects: WorkflowProject[], id?: string): string {
  return projects.find((project) => project.id === id)?.name || "未关联项目";
}
