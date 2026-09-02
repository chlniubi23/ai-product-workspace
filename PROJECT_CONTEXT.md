# PROJECT_CONTEXT — AI Product Workspace 项目事实基准

> 本文档记录截至 2026-08-30 对本项目的实际代码阅读结论，作为后续规划与开发对话的事实基准。
> 所有结论均来自实际代码阅读与测试运行，不是推测。行号引用为写作时快照，代码变动后以函数/类名为准。

---

## 1. 项目定位与业务主张

**AI Product Workspace** 是一个「AI 辅助、人工主导」的产品数据分析与决策工作台（中文界面）。

核心设计原则（在代码中被反复强制执行）：

- **数据计算全部确定性**：所有统计数字由 pandas 引擎（`apps/api/app/analytics/engine.py`）计算，LLM 永远不自己算数。
- **AI 只产草稿**：AI 输出永远是 draft 状态，永不自动 confirm；每个阶段的人工确认是独立动作。
- **证据链贯穿**：事实/假设/建议每条必须携带 evidence 引用（`AI_OUTPUT_SCHEMA`），采纳洞察强制要求非空证据。
- **硬性 AI 边界**：流水线阶段 1–5 之前无任何 AI 参与，AI 仅在阶段 6+ 出现。
- **多租户隔离**：一切资源挂 workspace，所有读取路径做成员校验 + workspace 边界检查。

---

## 2. 整体架构

```
┌─────────────────────┐      REST /api/v1       ┌──────────────────────────┐
│  apps/web (Next.js) │ ───────────────────────▶ │  apps/api (FastAPI)      │
│  React 18 / ECharts │  JWT Bearer (localStorage│  SQLAlchemy 2 / pandas   │
│  TailwindCSS        │  + apw_session cookie    │  JobExecutor(进程内)      │
└─────────────────────┘  供 middleware 门禁)      └───────────┬──────────────┘
                                                             │
                                              ┌──────────────┼───────────────┐
                                              ▼              ▼               ▼
                                         MySQL 8        DeepSeek API     文件系统
                                      (docker-compose;  (OpenAI 兼容,    data/uploads
                                       开发/测试用       httpx 或 openai  data/processed
                                       SQLite fallback)  SDK 二选一)      data/exports
```

- **通信**：纯 REST，统一响应信封 `{"data": ..., "meta": {request_id, ...}}` / `{"error": {code, message, details}}`（`app/main.py` 的 `ok()`/`error()`）。
- **鉴权**：HS256 JWT（`app/auth.py`），bcrypt 密码（>72 字节自动 pre-hash 标记 `bcrypt_sha256$`，兼容旧 pbkdf2 哈希并登录时升级）。JWT 存前端 localStorage；登录时镜像一个 `apw_session=1` cookie（max-age 对齐 JWT exp），Next.js middleware 只检查 cookie 存在性做**页面级软门禁**，真实鉴权在 API 层。
- **异步任务**：无外部队列。`app/infrastructure/jobs.py` 的 `JobExecutor` 通过 FastAPI BackgroundTasks 在进程内执行 DB 持久化的 Job（状态机 queued→running→succeeded/failed/cancelled），启动时 `recover_pending()` 重放未完成 job。handler 通过 `_register_job_handlers()`（main.py 末尾）注册：`dataset_parse`、`dataset_cleaning`、`analysis_run`、`feedback_import`、`feedback_clusters`、`document_generation`。
- **数据库**：生产 MySQL 8（docker-compose 只含 mysql 一个服务）；`DATABASE_URL` 未配置或 `ALLOW_SQLITE_FALLBACK=true` 时可回退 SQLite（`app/db.py`，fallback 状态通过 `/health/ready` 暴露）。`db.py` 还含 `_repair_missing_columns()` 运行时补列安全网（dev 便利，与 Alembic 并行的第二套 schema 机制）。
- **迁移**：`apps/api/alembic/versions/0001..0008`，其中 `0005_v11_slim_schema` 在 `V11_DROP_LEGACY_TABLES=true` 时删除 legacy 表（默认只加不减）。

---

## 3. 目录结构（实际状态）

**第三批（2026-08-30 完成）**：`main.py` 已从 6027 行拆为「app 组装层（146 行）+ `common.py` + 10 个 services + 13 个 routers」，路由清单由 `tests/test_route_manifest.py` 冻结守护。

```
AI_Product_Workspace/
├── .env / .env.example          # 配置（DEEPSEEK_API_KEY 等已配置）
├── docker-compose.yml           # 仅 mysql:8.0 服务
├── PROJECT_CONTEXT.md           # 本文件
├── output/test-runtime/         # 测试运行产物（gitignore）
└── apps/
    ├── api/                     # FastAPI 后端
    │   ├── app/
    │   │   ├── main.py          #   146 行：app 实例 + CORS/legacy 中间件 + 异常处理器 + startup + include_router + health 三端点 + _register_job_handlers() 调用
    │   │   ├── common.py        #   112 行：ok()/error() 响应信封、_request_id、serialize/model_dict、page_params/paged、_require_pandas 懒加载哨兵、_redact_validation_details
    │   │   ├── ai_context.py    #   AI 出站上下文防火墙 + 输出契约（未改动）
    │   │   ├── models.py / schemas.py / auth.py / config.py / db.py   # 未改动
    │   │   ├── services/        #   业务逻辑层（不含路由装饰器；禁止反向导入 routers）
    │   │   │   ├── access.py             #   membership/project_for/_dataset_version_for/_problem_for 等实体定位 + RBAC
    │   │   │   ├── audit.py              #   audit()/audit_user_workspaces
    │   │   │   ├── workspace_settings.py #   工作区设置/双层 token 预算/_workspace_payload/指标字典迁移
    │   │   │   ├── evidence.py           #   _check_evidence_scope/证据强制/引用范围校验
    │   │   │   ├── datasets.py           #   读文件/字段 schema/质量摘要/清洗操作/版本 payload
    │   │   │   ├── analysis_pipeline.py  #   分析产物持久化/配置校验/自动分析计划/_analysis_artifacts
    │   │   │   ├── ai_stages.py          #   _run_ai_stage 模板/_deepseek_answer/Copilot 编排胶水/AI 上下文投影
    │   │   │   ├── auto_report.py        #   项目级自动报告（pandas 聚合 + 确定性骨架）
    │   │   │   ├── documents.py          #   文档 payload + Markdown 渲染 + generate_document
    │   │   │   ├── interview.py          #   AI 采访轮次生成/强制去重 + 第 7 步蒸馏（第四批）
    │   │   │   └── job_handlers.py       #   job_executor 唯一实例 + 全部 _handle_* job handler + _register_job_handlers 定义
    │   │   ├── routers/         #   路由层：APIRouter + @router.<method>("/api/v1/...")（路径全写）
    │   │   │   ├── auth.py / workspaces.py / projects.py / datasets.py / analysis.py
    │   │   │   ├── insights.py / feedback.py / interview.py / problems.py / decisions.py
    │   │   │   ├── documents.py / jobs.py / copilot.py / ai.py
    │   │   │   └── （/ai/draft-document 在 documents.py、/ai/cluster-feedback 在 feedback.py——别名路由跟随其调用的服务函数所在 router，避免 routers 互导）
    │   │   ├── analytics/       #   engine.py / quality.py（未改动；顶层 import pandas 属既有行为）
    │   │   └── infrastructure/  #   jobs.py / llm/deepseek.py（未改动）
    │   ├── alembic/versions/    #   9 个迁移（0009 = interview_questions）
    │   ├── tests/               #   12 个测试文件，179 用例（含 route manifest 冻结测试 + 采访/守护测试）
    │   └── pyproject.toml
    └── web/                     # Next.js 14 前端
        ├── middleware.ts        #   登录门禁 + legacy 路由重定向
        ├── app/
        │   ├── (auth)/login/
        │   └── (workspace)/
        │       ├── page.tsx     #   ★ 工作台 = 流水线阶段 1–5
        │       ├── data/page.tsx        #   数据管理
        │       ├── settings/page.tsx    #   workspace 设置 + 健康状态
        │       └── stage6..stage11/     #   六个有序阶段页（第四批起 11 阶段：6=AI 采访、7=蒸馏+裁决、8=问题、9=方案、10=决策、11=PRD）
        ├── components/
        │   ├── layout/AppShell.tsx      #   侧边导航/布局
        │   ├── workflow/WorkflowFrame.tsx  # 阶段门控组件 + useWorkflowSnapshot
        │   └── analysis/                #   ChartRenderer(ECharts) / ReportMarkdown
        └── lib/
            ├── api.ts           #   fetch 封装 + 会话存取
            ├── workflow.ts      #   ★ 快照加载（11 类列表）+ 11 阶段门控计算
            ├── navigation.ts    #   导航元数据 + legacy 路由别名
            ├── settings.ts      #   设置/健康 API 封装
            └── chartOption.ts / format.ts / upload.ts
```

---

## 4. 技术栈（pyproject.toml / package.json 实际声明）

| 层 | 技术 |
|---|---|
| 后端框架 | FastAPI ≥0.111, uvicorn, pydantic v2 + pydantic-settings |
| ORM/迁移 | SQLAlchemy 2.0, Alembic 1.13, PyMySQL |
| 数据处理 | pandas 2.1+, numpy, openpyxl |
| 认证 | PyJWT (HS256), bcrypt（注意：**openai SDK 不在依赖中**，LLM 调用走 httpx fallback） |
| LLM | DeepSeek `chat/completions`（OpenAI 兼容），`deepseek-chat` 默认模型 |
| 前端 | Next.js 14.2 (App Router), React 18, TailwindCSS 3.4, ECharts 5.6, lucide-react |
| 存储 | MySQL 8（生产）/ SQLite（dev+test） |
| 测试 | pytest（后端 149 用例）；前端无测试框架 |
| Lint | ruff（line-length 120）、eslint + prettier（前端） |

---

## 5. 核心数据模型（apps/api/app/models.py）

**V1.1 核心表**（代码顶部 `V11_CORE_TABLE_NAMES` 明确列出）：
`users` / `projects` / `metric_definitions` / `datasets` / `dataset_versions` / `data_columns` / `data_quality_reports` / `cleaning_operations` / `analysis_runs` / `analysis_artifacts` / `insights` / `product_problems` / `solution_options` / `decision_proposals` / `copilot_sessions` / `copilot_messages` / `documents` / `document_versions` / `ai_runs` / `feedback_notes`

**Legacy 表**（淘汰窗口中，仍被 API 使用）：
`workspaces` / `workspace_members` / `tasks` / `task_links` / `feedback_items` / `feedback_clusters` / `feedback_cluster_items` / `approval_requests` / `jobs` / `audit_logs`

**新增于迁移 0007/0008**：
`analysis_report_narrations`（分析叙述，与 AnalysisRun 故意分离以保确定性） / `auto_analysis_reports`（项目级自动报告）

**新增于迁移 0009（第四批）**：
`interview_questions`（AI 采访问题：round_number 0=手动补充/≥1=AI 轮次，status pending|answered|skipped，source ai|manual；蒸馏时 answered 行作为 stage 7 的上下文与 evidence 来源）

关键模型语义（来自 docstring）：
- `DatasetVersion.schema_reviewed_at`（用户看过字段角色）与 `schema_auto_accepted_at`（解析 job 代接受）**是两列**——UI 必须区分「人看过」和「系统猜的」。
- `ProductProblem.source_insight_ids`：问题必须回链洞察才能 confirm，否则"凭直觉的问题"会流入决策。
- `SolutionOption.reject_reason`：选定方案时其余落选方案必须写落选理由。
- `AIRun`：所有 AI 调用的账本（feature_name/provider/model/tokens/latency/status/input_summary_json）。
- `AutoAnalysisReport.status`：`draft|succeeded|not_configured|failed|confirmed`——`succeeded` 表示 AI 叙述已验证入库，`not_configured` 表示只有确定性统计。

---

## 6. 核心业务流程：11 阶段流水线（第四批起；原 12 阶段，讨论并入采访）

前端 `lib/workflow.ts` 的 `stepCompletion()`（约 217 行）是 12 个门控的**事实定义**：

| 阶段 | 页面 | 完成条件（门控） | 后端关键端点 |
|---|---|---|---|
| 1 上传 | 工作台 `/` | 存在 activeDataset + version | `POST /datasets/upload`、`/datasets/upload-batch`（≤10 文件） |
| 2 Schema 审阅 | 工作台 | `schema_reviewed_at` 或 `schema_auto_accepted_at` | `POST /dataset-versions/{id}/schema-review`、`PATCH .../schema` |
| 3 质量报告 | 工作台 | version 有 quality_report | `GET /dataset-versions/{id}/quality-report` |
| 4 分析运行 | 工作台 | 存在 succeeded 的 AnalysisRun | `POST /analysis-runs`、`POST /analysis-runs/validate-config` |
| 5 分析产物 | 工作台 | run 有 artifacts/result_summary | `GET /analysis-runs/{id}/artifacts` |
| 5.5 AI 报告 | 工作台 | （非门控，独立 confirm） | `POST /projects/{id}/auto-report`、`POST /auto-reports/{id}/confirm` |
| 6 AI 采访 | stage6-interview | 存在 ≥1 条 answered 采访问题或手动补充 | `POST /projects/{id}/interview/rounds`（AI 每轮 3-5 问，服务端强制去重）、`GET/POST/PATCH /interview-questions`（回答/跳过/手动补充）；下半区保留自由追问（Copilot chat，`POST /copilot/sessions/{id}/messages`）。**第四批起原"洞察引擎"+"人机讨论"合并为本步** |
| 7 决策副驾 | stage7-copilot | 存在 status=confirmed 的洞察 | `POST /ai/distill-interview`（采访问答+分析产物 → 四段洞察**草稿**，证据引用 interview_question 或 analysis_artifact）→ 保存为草稿（不传 status）→ `PATCH /insights/{id}`（裁决采纳/否决；采纳**强制 evidence 非空**）。`/ai/interpret` 端点保留但 UI 不再使用 |
| 8 产品问题 | stage8-problem | problem.status=confirmed | `POST /ai/frame-problem`（草稿）→ `POST /problems`（落库，confirm 需 source_insight_ids） |
| 9 方案讨论 | stage9-solution | solution.status=selected | `POST /ai/propose-solutions` → `POST /solutions/{id}/select`（**落选方案必须写 reject_reason**） |
| 10 产品决策 | stage10-decision（无 AI） | decision.status=approved | `POST /decision-proposals/{id}/submit`（仅置 pending_approval + 建 pending 审批）→ **审批是独立动作**：`POST /approval-requests/{id}/approve|reject`（驳回必写理由；提案被编辑则审批返回 VERSION_CONFLICT）。同一账号可先提交再审批 |
| 11 PRD | stage11-prd | 文档有版本（workflow 门控不变） | `POST /documents`、`/documents/generate`、`/documents/{id}/versions`、`/documents/{id}/submit`、`GET /documents/{id}/export`（.md 下载）。页面渲染门控=存在 approved 决策 |

> **第四批洞察层重构（2026-09-01 完成）**：第 6 步从「AI 倒草稿」改为「AI 采访式收集」（原第 8 步人机讨论并入本步下半区），第 7 步改为「蒸馏+裁决」（新增 `/ai/distill-interview`，采访答案可作为 evidence 引用，`_check_evidence_scope` 新增 `interview_question` 类型），流水线 12→11 阶段（9-12 重编号为 8-11，旧路由经 `legacyRouteAliases` 308 重定向）。`InterviewQuestion` 模型 + 迁移 0009；`STAGE_COUNT=11`。
>
> **第二批流程收敛（2026-08-30 完成，纯前端）**：第 6 步只产草稿、第 7 步统一裁决；第 11 步提交与审批分离（`submit()` 不再自动 approve，页面新增待审批区块，数据来自 `GET /approval-requests`）；第 12 步渲染门控从"有已确认洞察"改为"存在 approved 决策"。后端零改动。

**上传后的自动管线**（`_handle_dataset_parse`，main.py 约 2413 行）：
解析 → 行列数/空表校验 → 质量评估 → 写字段字典 → **`schema_auto_accepted_at` 打点 + 内联跑 `_auto_analysis_plan`**（≤3 个：事件表选留存、指标表选趋势+异常，漏斗永不自动选；幂等，失败不拖垮解析）。

**数据集版本链**：重名重传追加不可变新版本（BUG-015 修复语义）；清洗（`POST /dataset-versions/{id}/cleaning-operations`）经 `cleaning_preview` 预演后由 `dataset_cleaning` job 产出新版本，每个操作单独落 `cleaning_operations` 行（可追溯/可重放）。

**删除语义**：数据集与项目删除均要求显式确认（`?confirm={id}` 或 body）；项目删除 `_purge_project`（main.py 约 1497 行）级联清理各资源并物理删除文件，全程审计。

---

## 7. AI / LLM 逻辑（本项目的核心特色）

### 7.1 出站上下文防火墙（app/ai_context.py）
- `build_ai_context()` 是唯一合法出站构造器：白名单键为 `goal / metrics / artifacts / quality / schema / question / insights` 七个；`FORBIDDEN_CONTEXT_KEYS`（raw_rows/file_path/token/database_url…）、`_FEEDBACK_CONTENT_KEYS`（feedback/content/comment/…约 25 个变体键）、行级列表键（rows/records/samples…）一律剔除；Email/电话正则脱敏，ID 类字段做稳定哈希匿名化。
- `insights` 是 2026-08-30 契约修复时**新增**的键（不是放宽）：每条只保留 `id/title/content/confidence/evidence`（`_extract_insights`，≤20 条、content≤2000 字符、evidence 走标量消毒）。仅由服务端注入（Copilot 路径加载 confirmed 洞察）；`/ai/interpret` 的 `_ai_interpret_context` 显式 pop 掉客户端传入的 insights，保持"客户端洞察一律丢弃"的既有行为（否则该键携带的 content 子键会被 `assert_safe_ai_context` 判为 feedback 文本导致 500）。
- `assert_safe_ai_context()` 复检；`validate_ai_output()` 强制输出含 `facts/hypotheses/recommendations/limitations` 四节、每条 claim 必带 evidence 数组；`validate_report_output()` 校验分章报告；另有 `PROBLEM_DRAFT_SCHEMA`/`validate_problem_draft`（stage 9 问题草稿：title/statement/impact_scope/limitations，priority 可选 P0-P3）与 `SOLUTION_DRAFTS_SCHEMA`/`validate_solution_drafts`（stage 10 方案草稿：options≤5 × title/approach/pros/cons/effort，effort 只允许 S/M/L）。反馈键清单单一来源在本文件：`services/ai_stages.py` 的请求侧消毒清单 = `_FEEDBACK_CONTENT_KEYS ∪ {sample, samples}`（第四批合一，此前是两份各自维护的相似清单）。

### 7.2 LLM 适配层（app/infrastructure/llm/deepseek.py）
- `DeepSeekAdapter.complete()`：PII 脱敏所有消息（8000 字符截断）、JSON mode（`response_format: json_object`）、指数退避重试（429/5xx/网络错）、usage 统计。
- 工具白名单：`ALLOWED_TOOL_NAMES` 12 个（`get_project_context/get_dataset_schema/run_eda/run_trend_analysis/run_funnel_analysis/run_retention_analysis/run_anomaly_detection/get_feedback_summary` 8 个只读 L1 + `create_insight_draft/create_decision_draft/create_document_draft/request_approval` 4 个需审批的占位）；`_DANGEROUS_ARGUMENTS`（sql/python/code/path/url/api_key…）在 plan 校验和执行双端拦截。
- `AnalysisPlan` pydantic 校验：steps ≤8、tool 必须在白名单、plan.project_id/dataset_id 不得越出请求范围、clarifying_question 与 steps 互斥。
- `CopilotOrchestrator`：两段 provider 调用（plan → execute(只读工具) → answer），返回 `AskClarification` 或 `Completed` 状态。

### 7.3 请求级编排（main.py）
- `_copilot_orchestrator()`（约 4165 行）：构造 workspace 绑定的工具注册表，`resolve_version` 校验数据版本归属。
- `_run_ai_stage()`（约 4763 行）：阶段 9/10 AI 起草的共享模板——feature flag 检查 → 预算预留 → provider 调用 → 结构化校验 → 事后预算复核 → AIRun 落账 → 审计。**未配 key 或 provider 故障一律降级为空草稿（`empty_ai_output`），绝不 500。** 支持三个可选参数自定义阶段契约：`response_schema`（替换提示词/解析用的 JSON schema）、`output_validator`（替换 `validate_ai_output`）、`empty_output`（降级时的空草稿形状）；缺省时行为与四段式契约完全一致。`/ai/frame-problem` 用它返回问题草稿对象、`/ai/propose-solutions` 返回 `{options: [...], limitations}`（均不再是四段式，前端 stage9/stage10 按此渲染）。
- 预算体系：`_workspace_ai_budget`（workspace settings 的 per_request/daily/max_output，三者大小关系在 PATCH 时校验）+ `_workspace_token_usage`（当日 AIRun 累计；running 状态按满额预留防并发超卖）+ `_reject_ai_budget`（429 + 审计）。copilot 消息路径对 workspace 行加锁（`with_for_update`）。
- **截断感知与单次重试（第五批）**：`_run_ai_stage` 检查 `LlmResult.finish_reason`；`finish_reason == "length"` 或 JSON 解析失败时自动重试一次（max_tokens 提为 min(max_output×2, per_request)，system 追加"禁止截断"指令），两次 tokens 累计入预算复核；仍失败则 `failed` + `LLM_TRUNCATED` / `INVALID_AI_OUTPUT`（语义变更：`raw is None` 不再标 succeeded，前端 stage7 按 error_code 显示中文文案、不再渲染空卡片）。AIRun `input_summary_json.provider_meta` 记 `{finish_reason, retried}` 元数据（不存原始响应内容）。`interview_distill` 的 prompt 带硬性规模约束（每节 ≤4 条、每条 ≤80 字、evidence 只引 1 个 id、limitations ≤3 条），正常输出 completion 约 2400 tokens，远低于 4096 上限。
- AI 端点清单：`/ai/interpret`、`/ai/frame-problem`、`/ai/propose-solutions`、`/ai/distill-interview`（第四批：采访蒸馏，UI 在 stage7 使用；`/ai/interpret` 端点保留但 UI 不再用）、`/ai/draft-document`、`/ai/cluster-feedback`、`/ai/usage`、`/dataset-versions/{id}/report-narration`、`/projects/{id}/auto-report`。采访轮次走 `POST /projects/{id}/interview/rounds`（services/interview.py，复用 `_run_ai_stage` + `INTERVIEW_QUESTIONS_SCHEMA`，服务端按规范化文本强制去重）。
- Copilot SSE 是**回放**而非实时流：事件先存 `AIRun.input_summary_json.events`，`GET /copilot/runs/{id}/events` 逐条吐出（`_sse`）。
- Copilot 上下文中的洞察由**服务端**注入：`copilot_message` 按 `session.project_id` 查询 confirmed 洞察（created_at 倒序 ≤20 条），经 `extract_ai_insights` 消毒后并入 `copilot_context`；不信任前端传的 insight_ids。

### 7.4 自动报告（`generate_auto_report`，约 5355 行）
「数字先行，叙述在后」：pandas 算每数据集聚合（EDA/分布/相关性/趋势，`asyncio.to_thread` 中执行，单文件失败隔离为 read_failure）→ 聚合经 `build_ai_context` 消毒后作为唯一 AI 输入 → AI 只写叙述 → 确定性报告体与 AI 章节一并入库（`deterministic_json` + `sections_json`）→ 人工 confirm 独立动作。

### 7.5 交付文档生成（第七批重做，AI 驱动）
- **上下文装配**（`services/documents.py:_build_document_context`）：四类证据全部走 artifacts 通道——项目内 confirmed 洞察（≤20）、已回答采访问题（≤30）、approved 决策（≤10）、最新 auto-report 的每数据集聚合（≤5，deterministic_json.datasets 逐个展开）；经 `build_ai_context` 消毒（防火墙零改动）。`_collect_source_refs` 保留原 source_refs 校验并产出 manifest 所需的上游 id 集合；`_evidence_manifest` 是两条渲染路径共用的不可变溯源块（**AI 输出永不覆盖 manifest**）。
- **job 链路**（`job_handlers._handle_document_generation`）：路由先落确定性模板 v1 并排队 job；handler 在 worker 线程 `asyncio.run(_run_ai_stage(...))`（feature=document_generation、`REPORT_OUTPUT_SCHEMA`、按 document_type 给中文章节结构 prompt、`min_output_tokens=8192`）——succeeded 则 AI 渲染中文 Markdown 追加 manifest 落 v2+；not_configured/failed/截断/预算拒绝（HTTPException 兜底）全部回退完整模板并在审计 detail 记录原因。job input 增加 `title`/`project_id`。
- **max_tokens 机制**：`_run_ai_stage` 新增可选 `min_output_tokens`（缺省 None 行为不变）——metadata 的 max_tokens 原本在函数内部由 budget 锁定，调用方无法覆盖，这是任务授权的最小调整；首次尝试 = max(max_output, min_output_tokens)（仍受 per_request 硬顶），重试翻倍基于该值。
- **实测成本**（deepseek-v4-flash，2026-09-02）：PRD 成功样本 prompt 1869 / completion 7032；周报成功样本 prompt 3746 / completion 7152——单次文档生成约 9-11k tokens；一次 LLM_PROVIDER_ERROR 降级为模板（前端如实提示）。
- 前端 stage11 已重做：类型选择器（周报/PRD/复盘）+ job 轮询 + 可编辑 textarea + 保存新版本（`POST /documents/{id}/versions {content_markdown}`）+ 项目文档列表/版本历史回看 + 模板回退检测提示。AI 助手抽屉已从前端移除（AppShell），AI 横幅仅保留 WorkflowFrame 页面级一条；后端 copilot 端点与测试保留。

---

## 8. API 面貌（第三批起按 routers/ 域拆分）

约 135 个端点（清单由 tests/test_route_manifest.py 冻结），按资源域：
- **Auth**：register/login/refresh/me（注册即建 workspace；登录失败统一报错不泄露邮箱存在性；均写审计）
- **Workspace**：list/patch/settings(GET,PATCH)/members/metrics 字典 CRUD（含 `/api/v1/settings`、`/api/v1/metrics` 别名）
- **Audit**：`GET /audit-logs`（workspace 级，newest-first）
- **Projects**：CRUD + overview + workflow-status + tasks CRUD/links + `DELETE`（显式确认）
- **Datasets**：upload/upload-batch/versions/schema(PATCH)/schema-review/preview/quality-report/cleaning-preview/cleaning-operations/DELETE（owner + 显式确认）
- **Analysis**：validate-config、runs CRUD、rerun、artifacts
- **Feedback**：items CRUD/import/imports、clusters generate/patch/link-task、notes GET/POST/PATCH（V1.1）
- **Insights / Problems / Solutions / Decisions / Approvals / Documents**：按第 6 节流程
- **Interview（第四批）**：`POST /projects/{id}/interview/rounds`、`GET/POST /interview-questions`、`PATCH /interview-questions/{id}`、`POST /ai/distill-interview`
- **AI + Copilot + Jobs**：见第 7 节

**V1.1 legacy 标记**：`_V11_LEGACY_API_PREFIXES`（workspaces/tasks/approval-requests/decision-proposals/jobs/copilot sessions/feedback-items/feedback-clusters）的响应带 `Deprecation: true`、`Sunset: 2027-01-01` 头（middleware `mark_legacy_api_surfaces`）。

**安全细节**（实测存在）：错误响应固定信封并对 validation details 递归脱敏（`_redact_validation_details`）；`_check_evidence_scope` 对 evidence 引用做存在性 + 跨 workspace/project 边界校验（BUG-004）；登录失败统一文案。

---

## 9. 前端结构与数据流

- **认证流**：login 页 `POST /auth/login` → `saveSession()`（localStorage + 镜像 cookie，cookie max-age 解析 JWT exp 对齐，`lib/api.ts:43`）→ middleware 放行；任意 401 统一 `clearSession()` + 跳 `/login`（`api.ts:22`）。
- **工作台数据流**：`loadWorkflowSnapshot()` `Promise.allSettled` 并行拉 11 类列表（含 `/approval-requests` 仅 pending、第四批新增的 `/interview-questions`）+ `/me`，容错收集 loadErrors → `hydrateActiveVersion` 补拉 schema/质量报告 → `stepCompletion()` 算门控（11 阶段；第 6 步=存在 answered 采访问题或手动补充）→ `WorkflowFrame` 渲染门控/进度。
- **工作台上传流**（`app/(workspace)/page.tsx`）：选文件 → `upload-batch` → 轮询 job（`TERMINAL_JOB_STATUS`，POLL_LIMIT=150）→ 就绪后 `POST auto-report` → 渲染 ReportMarkdown + ECharts 图表 → confirm/重新生成。
- **各阶段页**均为「门控包裹 + API 薄封装」模式；AI 起草按钮调用对应 `/ai/*` 端点，返回的 draft 填充表单，用户修改后走常规 POST/PATCH 落库。
- **设置页**：workspace 设置（时区/AI 模型/输出上限/双层 token 预算/feature flags）+ `/health/ai`、`/health/ready` 健康面板。
- **legacy 路由**：`legacyRouteAliases`（navigation.ts）由 middleware 308 重定向到新 IA。

---

## 10. 当前完成度

**已实现且验证**：
- 后端测试套件 **195 passed, 1 xfailed，0 警告**（2026-09-01 实测运行；含 route manifest 冻结测试、test_guardrails.py 守护测试：`_safe_data_file` 路径防越界、`models.now()` naive-UTC 语义、pandas 懒加载纪律、反馈键清单超集关系；第四批新增 test_interview.py 12 个采访/蒸馏测试）。覆盖：RBAC 与 workspace 隔离、数据管线（上传/版本/清洗/质量）、分析引擎全类型、AI 降级边界（无 key 绝不 500、输出契约、上下文白名单、反馈原文不外泄）、决策链规则（证据强制/落选理由/审批失效）、项目级联删除、报告叙述消毒。
- 8 个 Alembic 迁移可从零建库；`.env` 已配置 DeepSeek；前后端均可本地跑通。
- 前端 11 阶段页面、工作台、数据管理、设置页齐全（第四批起）。
- **全链路已真实手动冒烟走通**（12 阶段版 2026-08-30：上传→报告→洞察→讨论→问题→方案→决策→PRD；11 阶段版 2026-09-01：上传→报告→采访→蒸馏→裁决→问题→方案→决策→PRD）。
- **`ruff check app tests` 零告警**（第四批清掉 tests 基线 3 条 + 连带 2 条；unittest 弃用告警从 2494 → 0）。

**进行中（V1.1 迁移收尾）**：
- legacy 表/API 与新表/API 并存，代码中大量兼容分支（`_migrate_legacy_metric_dictionary`、`_drop_feedback_content`、feedback 双轨等）；`0005` 迁移默认不删 legacy 表，需显式 `V11_DROP_LEGACY_TABLES=true`。
- Copilot 的 `create_insight_draft` 等 4 个写工具在白名单中但**尚无服务端 handler**（`ReadOnlyToolRegistry.execute` 会拒绝）。

**缺失项**：
- **git 仓库还没有任何 commit**（全部文件 untracked 状态）。
- 无根 README、无 CI 流水线。
- 前端零测试（无测试框架）。
- 无国际化层（界面中文硬编码）。

---

## 11. 已知问题与技术债（按影响排序）

1. **`main.py` 5970 行巨型单文件**：全部路由 + 业务规则 + job handlers + AI 编排胶水都在一个文件。可维护性/可测试性差，是最大的结构性技术债；后续功能开发会继续向它堆积。拆分（按资源域拆 router/service）应先于大型新功能。
2. **V1.1 迁移未收尾**：双套模型/API 并存（feedback_items+clusters vs feedback_notes；tasks/approvals 待淘汰），每个新功能都要处理新旧两轨。
3. **两套并行 schema 机制**：Alembic 之外，`db.py:_repair_missing_columns()` 启动时给已存在表补列（无外键）。dev 便利但与迁移漂移风险。
4. **JobExecutor 进程内限制**：单进程假设（多 worker 会重复执行/丢任务）；无自动重试退避（仅手动 `POST /jobs/{id}/retry`，且需 `_retryable` 标记）；长任务占 BackgroundTasks 线程。
5. **AI 预算竞态窗口**：`with_for_update` 在 SQLite 是 no-op；仅 copilot 消息路径加行锁，`/ai/interpret` 等路径"预留→调用→结算"之间并发仍可能小幅超预算（已有"running 按满额预留"缓解）；`_workspace_token_usage` 每次全扫当日 AIRun，量大后变慢。
6. **代码卫生（第四批已清理大半）**：~~`models.now()` 用已废弃 `datetime.utcnow()`~~（已改为 `datetime.now(UTC).replace(tzinfo=None)`，naive-UTC 语义不变、由 test_guardrails 锁定）；~~`@app.on_event("startup")`~~（已迁移 FastAPI lifespan，行为等价）；~~tests 基线 ruff 告警~~（已清零）。仍存在：`openai` SDK 不在 pyproject 依赖（实际依赖 httpx fallback 或需手动安装）；CORS 未配置时回退 `["*"]` 且 `allow_credentials=True`。
7. **前端认证是软门禁**：middleware 只查 `apw_session=1` cookie 存在性（可伪造绕过页面守卫），真实鉴权仅在 API 层——设计上可接受但需明确这不是安全边界。JWT 在 localStorage（常规 XSS 暴露面），无服务端吊销。
8. **SSE 非实时**：copilot 事件回放式，用户体验依赖轮询 job 状态；`DeepSeekAdapter.stream()` 是伪流（一次性 complete 后整体 yield）。
9. **文件存储在本机磁盘**：`data/uploads|processed|exports`，无对象存储；`_purge_project` 物理删除不可恢复（有审计）。第四批起删除前经 `_safe_data_file` 做 DATA_ROOT 越界防护（防篡改行任意 unlink）。
10. **测试基建的小脆弱点**：`tests/conftest.py` 必须在 import app 前设置环境变量（ruff 已按文件豁免 E402 并有注释）；测试库文件在 `output/test-runtime`（rebuild on each run）。
11. **真实 key 下的 AI 输出可靠性（2026-08-30 冒烟观察，未修）**：`deepseek-v4-flash` 偶发返回无法解析为 JSON 的内容或被 `max_output` 截断——此时 `_run_ai_stage` 沿用既有"raw 为 None 即空草稿"语义把 run 标为 `succeeded` 但 output 为空形状，前端按"AI 起草不可用，可手写"降级（propose-solutions 冒烟中 2 次出现 1 次）；Copilot 编排的 plan 校验（`INVALID_ANALYSIS_PLAN`）在真实 key 下也出现过失败降级。降级路径本身行为正确，属模型输出质量/预算配置问题。
12. **项目删除与解析任务的竞态可泄漏上传文件（第四批冒烟实证，未修）**：真实 uvicorn 下上传后立即删除项目、若后台 `dataset_parse` job 尚未完成，`_purge_project` 返回 `files: 0` 且上传文件遗留在磁盘（复现：上传后 <1s 删除；等待解析完成再删则 `files: 1` 正常）。版本行会被级联删除，泄漏仅限磁盘文件。待办方向：删除时校验无 in-flight job，或解析完成后回收孤儿文件。
13. **历史事故记录（已修复）**：第三批 Phase 2 的 AST 切割脚本曾把 `_purge_project` 中对 `_safe_data_file` 的调用连同注释一并丢弃（拆分后该函数一度成为无调用者的死代码，且删除路径失去越界防护，提交 42728e5..132a25e 期间生效）。第四批重新接线并由 test_guardrails 锁定；同时纠正第三批汇报中"死代码"的定性——根源是脚本丢行，不是基线死代码。

---

## 12. 给后续开发对话的关键事实速查

**第三批（2026-08-30）拆分后的落位规则**：
- 新增 API 端点：写到对应域的 `app/routers/<域>.py`（`router = APIRouter()` + `@router.<method>("/api/v1/...")` 路径全写），在 `main.py` 加 `app.include_router(...)`；请求模型进 `schemas.py`。`tests/test_route_manifest.py` 会冻结断言全部 (path, methods, name)——路由变更必须同步重生成该清单。
- 新增业务逻辑：放到 `app/services/<域>.py`；被多个 router 共用的 helper 必须下沉 services（routers 之间禁止互导，services 禁止反向导入 routers）。`ok()/error()/model_dict/paged` 等信封工具在 `common.py`；`_require_pandas()` 是 pandas 懒加载哨兵（使用方在函数内 `pd = _require_pandas()`，运行时禁止模块顶层 import pandas）。
- job handler：`app/services/job_handlers.py`，`job_executor` 全仓库唯一实例在此；新增 handler 后在 `_register_job_handlers()` 注册（main.py 末尾恰好调用一次）。
- 新增 AI 能力：服务逻辑进 `services/ai_stages.py`（复用 `_run_ai_stage()` 模板，可传 `response_schema`/`output_validator`/`empty_output` 定义阶段契约），路由壳进 `routers/ai.py`；上下文必须过 `build_ai_context`，AI 结果一律 draft；Copilot 的 insights 上下文由服务端注入，客户端传入的一律丢弃。
- 分析类型扩展点：`analytics/engine.py`（计算）+ `services/analysis_pipeline.py`（`_analysis_artifacts` 持久化映射、`_analysis_config_validation`、`_auto_analysis_plan`）+ `deepseek.py` 工具白名单（若暴露给 Copilot）。
- 前端新页面的惯例：`app/(workspace)/` 下建目录，用 `WorkflowFrame` 的 `WorkflowHeader/WorkflowGate` 包裹，门控逻辑改 `lib/workflow.ts` 的 `stepCompletion()`，导航加 `lib/navigation.ts`。
- 测试运行：`cd apps/api && .venv/Scripts/python.exe -m pytest tests -q`（Windows；测试自备隔离 SQLite 与空 DeepSeek key）。
