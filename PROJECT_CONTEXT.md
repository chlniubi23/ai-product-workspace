# PROJECT_CONTEXT — AI Product Workspace 项目事实基准

> 本文档记录截至 2026-09-03 对本项目的实际代码阅读结论，作为后续规划与开发对话的事实基准（批 1–8 的演进以各节内标注的批次说明为准）。
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
- **异步任务**：无外部队列。`app/infrastructure/jobs.py` 的 `JobExecutor` 通过 FastAPI BackgroundTasks 在进程内执行 DB 持久化的 Job（状态机 queued→running→succeeded/failed/cancelled），启动时 `recover_pending()` 重放未完成 job。handler 通过 `_register_job_handlers()`（main.py 末尾）注册：`dataset_parse`、`analysis_run`、`feedback_import`、`feedback_clusters`、`document_generation`、`auto_report_narration`（`dataset_cleaning` 已随第十一批删除）。
- **数据库**：生产 MySQL 8（docker-compose 只含 mysql 一个服务）；`DATABASE_URL` 未配置或 `ALLOW_SQLITE_FALLBACK=true` 时可回退 SQLite（`app/db.py`，fallback 状态通过 `/health/ready` 暴露）。`db.py` 还含 `_repair_missing_columns()` 运行时补列安全网（dev 便利，与 Alembic 并行的第二套 schema 机制）。
- **迁移**：`apps/api/alembic/versions/0001..0014`（0013 = data_columns.source；0014 = drop auto_analysis_reports.superseded_at，第十五批），其中 `0005_v11_slim_schema` 在 `V11_DROP_LEGACY_TABLES=true` 时删除 legacy 表（默认只加不减）。

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
    │   │   │   ├── datasets.py           #   读文件/字段 schema/质量摘要/版本 payload（清洗 helper 已随第十一批删除）
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
    │   ├── alembic/versions/    #   14 个迁移（0013 = data_columns.source；0014 = drop superseded_at）
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
| 测试 | pytest（后端 201 用例，2026-09-03）；前端无测试框架 |
| Lint | ruff（line-length 120）、eslint + prettier（前端） |

---

## 5. 核心数据模型（apps/api/app/models.py）

**V1.1 核心表**（代码顶部 `V11_CORE_TABLE_NAMES` 明确列出）：
`users` / `projects` / `metric_definitions` / `datasets` / `dataset_versions` / `data_columns` / `data_quality_reports` / `cleaning_operations` / `analysis_runs` / `analysis_artifacts` / `insights` / `product_problems` / `solution_options` / `decision_proposals` / `copilot_sessions` / `copilot_messages` / `documents` / `document_versions` / `ai_runs` / `feedback_notes`

**Legacy 表**（淘汰窗口中，仍被 API 使用）：
`workspaces` / `workspace_members` / `tasks` / `task_links` / `feedback_items` / `feedback_clusters` / `feedback_cluster_items` / `approval_requests` / `jobs` / `audit_logs`

**新增于迁移 0007/0008**：
`analysis_report_narrations`（分析叙述，与 AnalysisRun 故意分离以保确定性） / `auto_analysis_reports`（项目级自动报告）

**第十一批（2026-09-04）清洗功能删除**：清洗全链路（3 个端点、`dataset_cleaning` job、`apply_cleaning`/`CleaningRequest`/清洗 helper）已删除——主流程是「上传 → 代码计算 → LLM 解读」，清洗是 V1.0 质量门控时代的死流程；`cleaning_operations` 表与 `CleaningOperation` 模型**保留不 drop**（仅项目删除时的级联清理仍触达），`assess_quality` 质量报告完整保留。

**新增于迁移 0009（第四批）**：
`interview_questions`（AI 采访问题：round_number 0=手动补充/≥1=AI 轮次，status pending|answered|skipped，source ai|manual；蒸馏时 answered 行作为 stage 7 的上下文与 evidence 来源）

**新增于迁移 0010/0011（第七/九批）**：
`document_versions.ai_status/ai_error_code`（版本产出来源：NULL=旧数据、succeeded=AI、fallback=模板回退，交付页据此显示提示条）/ `projects.archived_at`（第九批「一个项目=一次工作流」：归档时间戳，与 status="archived" 成对出现）

关键模型语义（来自 docstring）：
- `DatasetVersion.schema_reviewed_at`（用户看过字段角色）与 `schema_auto_accepted_at`（解析 job 代接受）**是两列**——UI 必须区分「人看过」和「系统猜的」。
- `ProductProblem.source_insight_ids`：问题必须回链洞察才能 confirm，否则"凭直觉的问题"会流入决策。
- `SolutionOption.reject_reason`：选定方案时其余落选方案必须写落选理由。
- `AIRun`：所有 AI 调用的账本（feature_name/provider/model/tokens/latency/status/input_summary_json）。
- `AutoAnalysisReport.status`：`draft|succeeded|not_configured|failed|confirmed`——`succeeded` 表示 AI 叙述已验证入库，`not_configured` 表示只有确定性统计。

---

## 6. 核心业务流程：11 阶段流水线（第四批起；原 12 阶段，讨论并入采访）

前端 `lib/workflow.ts` 的 `stepCompletion()` 是 11 个门控的**事实定义**：

| 阶段 | 页面 | 完成条件（门控） | 后端关键端点 |
|---|---|---|---|
| 1 上传 | 工作台 `/` | 存在 activeDataset + version | `POST /datasets/upload`、`/datasets/upload-batch`（≤10 文件） |
| 2 Schema 审阅 | 工作台 | `schema_reviewed_at` 或 `schema_auto_accepted_at` | `POST /dataset-versions/{id}/schema-review`、`PATCH .../schema` |
| 3 质量报告 | 工作台 | version 有 quality_report | `GET /dataset-versions/{id}/quality-report` |
| 4 分析运行 | 工作台 | 存在 succeeded 的 AnalysisRun | `POST /analysis-runs`、`POST /analysis-runs/validate-config` |
| 5 分析产物 | 工作台 | run 有 artifacts/result_summary | `GET /analysis-runs/{id}/artifacts` |
| 5.5 AI 报告 | 工作台 | （非门控，独立 confirm） | `POST /projects/{id}/auto-report/compute`（秒级确定性，第十批）、`POST /auto-reports/{id}/narrate`（异步 AI 解读 job）、`POST /auto-reports/{id}/confirm`；旧 `POST /projects/{id}/auto-report` 保留为兼容串联 |
| 6 AI 采访 | stage6-interview | 存在 ≥1 条 answered 采访问题或手动补充 | `POST /projects/{id}/interview/rounds`（AI 每轮 3-5 问，服务端强制去重）、`GET/POST/PATCH /interview-questions`（回答/跳过/手动补充）。**第四批起原"洞察引擎"+"人机讨论"合并为本步；第七批移除下半区自由追问** |
| 7 决策副驾 | stage7-copilot | 存在 status=confirmed 的洞察 | `POST /ai/distill-interview`（采访问答+分析产物 → 四段洞察**草稿**，证据引用 interview_question 或 analysis_artifact）→ 保存为草稿（不传 status）→ `PATCH /insights/{id}`（裁决采纳/否决；采纳**强制 evidence 非空**）。`/ai/interpret` 端点保留但 UI 不再使用 |
| 8 产品问题 | stage8-problem | problem.status=confirmed | `POST /ai/frame-problem`（草稿）→ `POST /problems`（落库，confirm 需 source_insight_ids） |
| 9 方案讨论 | stage9-solution | solution.status=selected | `POST /ai/propose-solutions` → `POST /solutions/{id}/select`（**落选方案必须写 reject_reason**） |
| 10 产品决策 | stage10-decision（无 AI） | decision.status=approved | `POST /decision-proposals/{id}/submit`（仅置 pending_approval + 建 pending 审批）→ **审批是独立动作**：`POST /approval-requests/{id}/approve|reject`（驳回必写理由；提案被编辑则审批返回 VERSION_CONFLICT）。同一账号可先提交再审批 |
| 11 PRD | stage11-prd | 文档有版本（workflow 门控不变） | `POST /documents`、`/documents/generate`、`/documents/{id}/versions`、`/documents/{id}/submit`、`GET /documents/{id}/export`（.md 下载）；完成后 `POST /projects/{id}/archive` 归档。页面渲染门控=存在 approved 决策 |

> **第十三批「链式地基闭合」（2026-09-04 完成）**：采访与蒸馏的地基升级为「最新报告 + 底层产物细节」——`services/interview.py:_latest_report_context` 取项目报告（优先 confirmed，否则最新）的 `deterministic_json.datasets` 展开为 `dataset_summary` artifacts（形状照抄 documents，≤5 个），`_grounding_artifacts` = 报告聚合 + 原始产物（窗口 12→20，天然容纳落库的 finding artifacts）；采访 prompt 要求围绕报告重点提问。digest findings 在 compute 时落库为**真实 `finding` artifacts**（挂各数据集最新 succeeded run，幂等——重算先删旧行；无 succeeded run 的数据集只留在报告 digest；重跑分析会清掉 artifacts，需重新生成报告恢复），documents/interview 的 digest 注入优先读落库 artifacts（真实 id 可引用）。报告「已取代」语义（迁移 0012，**第十五批已退役**）：compute 现改为直接删除旧报告（见下批说明）。P0 修复：ISO 日期不再被手机号正则误杀（见 §7.1）。高基数标识列（unique_count ≥ 0.9×row_count ≥10 行）在报告聚合中标记 `identifier` 并丢弃 top 值分布，digest 集中度规则不再产生噪音。

> **第九批「一个项目 = 一次工作流」（2026-09-03 完成）**：`Project` 增加 `archived_at`；新增 `POST /projects/{id}/archive|unarchive`（幂等、editor+、绕过自身守卫）。归档项目只读：`project_for` 对 editor+ 返回 409 `PROJECT_ARCHIVED`（viewer 读取不受影响），绕过 `project_for` 的编辑端点（insight/decision/document/dataset/task/feedback 的 patch/submit/approve/retry 等）逐一补 `_ensure_project_active` 守卫。前端：activeProjectId 持久化到 localStorage（`apw_active_project`，切换时派发 `apw-project-changed` 事件），快照按当前项目过滤（失效 id 回退到第一个活跃项目），新增 `/history` 列表页与 `/history/[projectId]` 只读回看页。

> **第四批洞察层重构（2026-09-01 完成）**：第 6 步从「AI 倒草稿」改为「AI 采访式收集」（原第 8 步人机讨论并入本步下半区），第 7 步改为「蒸馏+裁决」（新增 `/ai/distill-interview`，采访答案可作为 evidence 引用，`_check_evidence_scope` 新增 `interview_question` 类型），流水线 12→11 阶段（9-12 重编号为 8-11，旧路由经 `legacyRouteAliases` 308 重定向）。`InterviewQuestion` 模型 + 迁移 0009；`STAGE_COUNT=11`。
>
> **第二批流程收敛（2026-08-30 完成，纯前端）**：第 6 步只产草稿、第 7 步统一裁决；第 11 步提交与审批分离（`submit()` 不再自动 approve，页面新增待审批区块，数据来自 `GET /approval-requests`）；第 12 步渲染门控从"有已确认洞察"改为"存在 approved 决策"。后端零改动。

**上传后的自动管线**（`services/job_handlers.py:_handle_dataset_parse`）：
解析 → 行列数/空表校验 → 质量评估 → 写字段字典 → **`schema_auto_accepted_at` 打点 + 内联跑 `_auto_analysis_plan`（第十二批起 ≤4 个：EDA 恒在 + 事件表选留存、指标表选趋势、业务表（低基数类别+数值）选 group 分组 + `group_comparison` Pareto 分组对比、数值列选异常；漏斗永不自动选；幂等，失败不拖垮解析）。第十二批同时修复 `_replace_version_columns` 的关系缓存缺陷（见 §11.16）——此前所有非 EDA 自动分析因列校验拿不到列而从未真正落库。

**数据集版本链**：重名重传追加不可变新版本（BUG-015 修复语义）；~~清洗（`POST /dataset-versions/{id}/cleaning-operations`）~~ 已随第十一批删除（版本只经上传产生）。

**删除语义**：数据集与项目删除均要求显式确认（`?confirm={id}` 或 body）；项目删除 `_purge_project`（`routers/projects.py`）级联清理各资源并经 `_safe_data_file` 越界防护后物理删除文件，全程审计。

---

## 7. AI / LLM 逻辑（本项目的核心特色）

### 7.1 出站上下文防火墙（app/ai_context.py）
- `build_ai_context()` 是唯一合法出站构造器：白名单键为 `goal / metrics / artifacts / quality / schema / question / insights` 七个；`FORBIDDEN_CONTEXT_KEYS`（raw_rows/file_path/token/database_url…）、`_FEEDBACK_CONTENT_KEYS`（feedback/content/comment/…约 25 个变体键）、行级列表键（rows/records/samples…）一律剔除；Email/电话正则脱敏（第十三批修正：ISO 日期先保护后还原——`_PHONE_RE` 此前把 `2026-03-30` 整串替换为 `[phone]`，已在 `ai_context._safe_scalar` 与 `deepseek.redact_pii` 两处改用「保护-脱敏-还原」，真手机号/邮箱仍被脱敏），ID 类字段做稳定哈希匿名化。
- `insights` 是 2026-08-30 契约修复时**新增**的键（不是放宽）：每条只保留 `id/title/content/confidence/evidence`（`_extract_insights`，≤20 条、content≤2000 字符、evidence 走标量消毒）。仅由服务端注入（Copilot 路径加载 confirmed 洞察）；`/ai/interpret` 的 `_ai_interpret_context` 显式 pop 掉客户端传入的 insights，保持"客户端洞察一律丢弃"的既有行为（否则该键携带的 content 子键会被 `assert_safe_ai_context` 判为 feedback 文本导致 500）。
- `assert_safe_ai_context()` 复检；`validate_ai_output()` 强制输出含 `facts/hypotheses/recommendations/limitations` 四节、每条 claim 必带 evidence 数组；`validate_report_output()` 校验分章报告；另有 `PROBLEM_DRAFT_SCHEMA`/`validate_problem_draft`（stage 9 问题草稿：title/statement/impact_scope/limitations，priority 可选 P0-P3）与 `SOLUTION_DRAFTS_SCHEMA`/`validate_solution_drafts`（stage 10 方案草稿：options≤5 × title/approach/pros/cons/effort，effort 只允许 S/M/L）。反馈键清单单一来源在本文件：`services/ai_stages.py` 的请求侧消毒清单 = `_FEEDBACK_CONTENT_KEYS ∪ {sample, samples}`（第四批合一，此前是两份各自维护的相似清单）。

### 7.2 LLM 适配层（app/infrastructure/llm/deepseek.py）
- `DeepSeekAdapter.complete()`：PII 脱敏所有消息（8000 字符截断）、JSON mode（`response_format: json_object`）、指数退避重试（429/5xx/网络错）、usage 统计。
- 工具白名单：`ALLOWED_TOOL_NAMES` 12 个（`get_project_context/get_dataset_schema/run_eda/run_trend_analysis/run_funnel_analysis/run_retention_analysis/run_anomaly_detection/get_feedback_summary` 8 个只读 L1 + `create_insight_draft/create_decision_draft/create_document_draft/request_approval` 4 个需审批的占位）；`_DANGEROUS_ARGUMENTS`（sql/python/code/path/url/api_key…）在 plan 校验和执行双端拦截。
- `AnalysisPlan` pydantic 校验：steps ≤8、tool 必须在白名单、plan.project_id/dataset_id 不得越出请求范围、clarifying_question 与 steps 互斥。
- `CopilotOrchestrator`：两段 provider 调用（plan → execute(只读工具) → answer），返回 `AskClarification` 或 `Completed` 状态。

### 7.3 请求级编排（services/ai_stages.py + routers/ai.py）
- `_copilot_orchestrator()`（services/ai_stages.py）：构造 workspace 绑定的工具注册表，`resolve_version` 校验数据版本归属。
- `_run_ai_stage()`（services/ai_stages.py）：interpret/蒸馏/采访/问题/方案/文档等 AI 起草端点的共享模板——feature flag 检查 → 总阀门 → provider 调用（含截断重试）→ 结构化校验 → AIRun 落账 → 审计。**未配 key 或 provider 故障一律降级为空草稿（`empty_ai_output`），绝不 500。** 支持可选参数自定义阶段契约：`response_schema`（替换提示词/解析用的 JSON schema）、`output_validator`（替换 `validate_ai_output`）、`empty_output`（降级时的空草稿形状）、`min_output_tokens`（抬高首次输出上限，文档生成用）。`/ai/frame-problem` 用它返回问题草稿对象、`/ai/propose-solutions` 返回 `{options: [...], limitations}`（均不再是四段式，前端 stage8/stage9 按此渲染）。
- **预算体系（第八批重构：直花 + 硬顶 + 总阀门）**：`_run_ai_stage` 调用前只查日阀门（`_workspace_token_usage` 累计当日 running/succeeded 行 + 本调用 worst_case = est_prompt(≤12000) + max_tokens，超过 `ai_daily_token_budget` → 调用前拒绝、零消耗、details 含 daily_remaining/needed_tokens/中文 hint）；**调用后只记账、结果绝不因预算丢弃**（旧"调用后复核拒绝"已删除）。输出上限由模块常量 `HARD_OUTPUT_CAP=16384` 硬顶（普通调用=设置派生 max_output 默认 4096，文档 8192，重试翻倍至多 16384，不可配置）；`ai_per_request_token_budget` 不再拦截任何调用（设置与校验保留，仅退役拦截职责）。`_workspace_token_usage` 的 running 行按其记录的 worst_case 预留（旧行退回 per_request）。copilot 消息路径的独立预检与行锁保留。
- **截断感知与单次重试（第五批，第八批并入硬顶/阀门语义）**：`_run_ai_stage` 检查 `LlmResult.finish_reason`；`finish_reason == "length"` 或 JSON 解析失败时自动重试一次（max_tokens = min(desired×2, `HARD_OUTPUT_CAP`)，system 追加"禁止截断"指令；重试前只过日阀门，放不下则保留已付费的首试、诚实失败），两次 tokens 累计记账；仍失败则 `failed` + `LLM_TRUNCATED` / `INVALID_AI_OUTPUT`（`raw is None` 不再标 succeeded，前端 stage7 按 error_code 显示中文文案、不再渲染空卡片）。AIRun `input_summary_json.provider_meta` 记 `{finish_reason, retried}` 元数据（不存原始响应内容）。`interview_distill` 的 prompt 带硬性规模约束（每节 ≤4 条、每条 ≤80 字、evidence 只引 1 个 id、limitations ≤3 条），正常输出 completion 约 2400 tokens。
- AI 端点清单：`/ai/interpret`、`/ai/frame-problem`、`/ai/propose-solutions`、`/ai/distill-interview`（第四批：采访蒸馏，UI 在 stage7 使用；`/ai/interpret` 端点保留但 UI 不再用）、`/ai/draft-document`、`/ai/cluster-feedback`、`/ai/usage`、`/dataset-versions/{id}/report-narration`、`/projects/{id}/auto-report/compute`、`/auto-reports/{id}/narrate`、`/projects/{id}/auto-report`（兼容串联，第十批拆分）。采访轮次走 `POST /projects/{id}/interview/rounds`（services/interview.py，复用 `_run_ai_stage` + `INTERVIEW_QUESTIONS_SCHEMA`，服务端按规范化文本强制去重）。
- Copilot SSE 是**回放**而非实时流：事件先存 `AIRun.input_summary_json.events`，`GET /copilot/runs/{id}/events` 逐条吐出（`_sse`）。
- Copilot 上下文中的洞察由**服务端**注入：`copilot_message` 按 `session.project_id` 查询 confirmed 洞察（created_at 倒序 ≤20 条），经 `extract_ai_insights` 消毒后并入 `copilot_context`；不信任前端传的 insight_ids。

### 7.4 自动报告：先算后叙两步链路（第十批重做，2026-09-03 完成；第十二批加 findings digest）
「数据概况秒级可见，AI 解读异步补上」——原 `POST /projects/{id}/auto-report` 同步串（pandas 聚合 + AI 叙述一个 HTTP 调用）拆为：
- **`POST /projects/{id}/auto-report/compute`**：仅确定性部分——`_latest_project_versions` + `_compute_report_aggregates_batch`（`asyncio.to_thread`，单文件失败隔离为 read_failure）+ `_deterministic_report_parts`，落库 `AutoAnalysisReport`（status=`not_configured`、`deterministic_json` 完整、`content_markdown`=确定性体）并立即返回；**零 AI 调用、零 token、不建 AIRun/job**（测试锁定）。重复调用始终新建一条报告（与原语义一致）。
- **`POST /auto-reports/{id}/narrate`**：对已有报告排队 `auto_report_narration` job（`job_handlers._handle_auto_report_narration`，`asyncio.run(_narrate_report(...))`，模式同文档生成）；`_narrate_report`（services/auto_report.py）从 `deterministic_json.datasets` 重建消毒上下文，走 `_run_ai_stage`（feature=`auto_report_narration`、现有叙述 prompt + `REPORT_OUTPUT_SCHEMA`、flag `auto_report_enabled`、`min_output_tokens=8192`，预算阀门复用第八批语义）。成功 → `sections_json` = 确定性段在前 + AI 段在后重渲染 markdown、status=`succeeded`、记录 `ai_run_id`；任何失败 → status 保持 `not_configured`、仅记 `error_code`，确定性体不动。succeeded/confirmed 报告拒绝重叙述（409 `REPORT_ALREADY_NARRATED`），同一报告在途 job 拒绝重复叙述（409 `NARRATION_IN_PROGRESS`，防双花）。
- 旧 `POST /projects/{id}/auto-report` **保留为兼容串联**（compute + 同步 narrate，AIRun feature 变为 `auto_report_narration`）；一处语义变化：预算阀门 429 时 compute 报告保留（原先是零报告），聚合数字零成本且可用。实测成本（deepseek-v4-flash，2026-09-03 冒烟）：单次叙述 prompt 1438 / completion 2909-5346；compute 无 ai_runs 记录。

**跨页可恢复（第十六批，2026-09-04 完成）**：`_auto_report_payload(report, db)` 输出 `narration_job_id`（`_active_narration_job`——原布尔 helper 重构返回 job 本体，非空即 queued/running 的叙述 job；终端后自然为 null）；`_document_payload` 同构输出 `generation_job_id`。前端工作台：payload 带活跃 job id 时自动恢复叙述轮询（复用 pollNarration，零点击、无重复排队，409 NARRATION_IN_PROGRESS 防重复兜底），进行中隐藏「补生成/重试」按钮；stage11 挂载水合后对 `generation_job_id` 自动续接生成轮询并重水合。运行时验证（真 key）：上传→概况秒现→切数据页 60s→回工作台自动显示「AI 解读生成中…」→24s 后无点击更新为完整报告。

**findings digest 与 group_comparison（第十二批，2026-09-04 完成）**：
- **`analytics/digest.py: build_findings_digest(aggregates)`**：纯函数，从报告聚合（`deterministic_json.datasets` 同形）按规则提炼 ≤12 条中文发现（kind=missing|correlation|trend_shift|concentration|duplicate，阈值 10%/0.6/30%/60%/5% 为模块常量），severity 降序 + |value| 降序排序；compute 时写入 `deterministic_json["findings"]`，并替换确定性报告「关键发现」段的内容（空 digest 回退旧罗列）。
- **注入点仅两处**：narrate（`_report_ai_context` 把 digest 逐条作为 `{"id": "finding-N", "artifact_type": "finding"}` artifact 走 artifacts 通道，payload 键 kind/dataset/severity/rate/metrics 全在防火墙白名单内——**ai_context.py 零改动**；system prompt 要求逐条覆盖）与文档生成（`_build_document_context` 第五类证据，同形状 ≤12 条）。蒸馏/采访不注入（合成 id 会被证据归一丢弃）。
- **`AnalysisEngine.run_group_comparison(group_column, value_column, aggregation="mean", top_n=10)`**：按类别列分组聚合数值列（count/mean/sum/min/max + sum 口径占比，按占比降序 top_n）；分组行列表用白名单键 `categories`（任务建议键名 `groups` 不在 `_AGGREGATE_LIST_KEYS`，防火墙禁改，故偏离）；`SUPPORTED_ANALYSIS_TYPES`/`_analysis_artifacts`（bar 图 option）/`_analysis_config_validation` 均有对应分支。
- `_compute_report_aggregates` 内联调 run_group_comparison，把 top 组摘要并入该数据集聚合的 `breakdown` 键（同为白名单键）；自动分析计划上限 3→4，业务表追加 group_comparison（reason=`..._for_pareto`）。前端 `chartOption.ts` 新增 bar 渲染（group_comparison 占比柱状图）。

### 7.4b 计算层 v2 能力清单与封版说明（第十四批，2026-09-04 完成——计算层就此封版）

「数据上传后全部价值提取由代码完成，LLM 只做整合解读」。五项地基能力全部落地：

1. **解析鲁棒性**（`analytics/parsing.py`）：`parse_numeric`（千分位/货币 ¥￥$/百分比（保留数值本身）/k|K|M|m|B|万|亿 后缀/负号/括号负数）、`parse_datetime_value`（ISO/斜杠/中文 `2026年3月30日`|`2026年3月`/带时间）、`parse_boolean`（是/否、true/false、0/1、Y/N）。`infer_column_type_v2(series)` 返回 `{semantic_type: numeric|datetime|boolean|category|text|identifier, parse_rate, unique_ratio, constant, identifier}`——采样逐值解析、parse_rate ≥0.8 判定（numeric→datetime→boolean 顺序）；identifier=唯一率 ≥0.9 且无空格的字符串列；constant 为正交标志。**`quality.infer_column_type` 已委托 v2**（numeric/datetime/boolean 同名映射，其余返回 `categorical`），上传推断全链路一致。
2. **文本指标抽取**（`analytics/text_metrics.py: extract_text_metrics(frame, text_columns=None, min_label_coverage=0.3)`）：固定「label(2-12 位含字母/汉字)+数值+可选单位」模式逐行抽取；label 归一（去空格小写）后出现率 ≥30% 非空行才派生列 `{源列}__{指标}`（行对齐，未匹配 NaN）；返回 (扩展帧副本, extraction_report)；**默认只扫 text/category 语义列、排除 constant 列**（日期/ID 列不产垃圾 token）。同时把 v2 numeric/datetime/boolean 字符串列**物化为解析值**（"¥12,000"→12000.0、中文日期→datetime）——否则 schema 标 float 而单元格仍是文本，下游数值计算必崩（实现期发现）。
3. **语义分类与溯源**：`DataColumn.source`（迁移 0013，`original|extracted`）；parse 流程 = 原帧质量评估 + `extract_text_metrics` → `_column_schema(frame_ext)`（含派生列）→ schema item 带 `source`；`schema_json["text_metric_extraction"]` 存逐指标覆盖率报告；`version.column_count` 保持原文件列数。
4. **分布深化**：`_compute_report_aggregates` 在扩展帧上聚合，数值列（含派生）统计含 `outliers`（IQR 1.5 倍计数）、`skewness`、`bins`+`counts`（pd.cut 直方）；metrics entry 标 `source` 与 `constant`；trend 增 `gaps`（日历缺口期数，按频率步长推算）。
5. **digest v2**：新增三条规则——常数列每数据集合并为一条（kind=constant）、IQR 离群占比 ≥5%（kind=outlier，≥15% 升 severity 3）、趋势日历缺口 ≥1 期（kind=calendar_gap）；派生指标列（名含 `__`）命中的 trend_shift 在语句中标注「（抽取指标）」。

回归基准：`tests/fixtures/samples/business_table_text_metrics.csv`（30 行 × 8 列合成周报表：ISO+中文日期、千分位货币、百分比、类别、标识符、常数、含缺失的文本指标列）；`tests/test_parsing.py`（34 个）+ `tests/test_compute_v2.py`（4 个集成）。派生列前端标注：数据集详情页字段字典对 `source=extracted` 显示「抽取 NN%」tag（覆盖率来自 extraction report）。**不做**：跨数据集 join、Cramér's V/交叉表、缺失共现、季节性分解、新分析范式——等真实需求立项。

### 7.5 交付文档生成（第七批重做，AI 驱动）
- **上下文装配**（`services/documents.py:_build_document_context`）：四类证据全部走 artifacts 通道——项目内 confirmed 洞察（≤20）、已回答采访问题（≤30）、approved 决策（≤10）、最新 auto-report 的每数据集聚合（≤5，deterministic_json.datasets 逐个展开）；经 `build_ai_context` 消毒（防火墙零改动）。`_collect_source_refs` 保留原 source_refs 校验并产出 manifest 所需的上游 id 集合；`_evidence_manifest` 是两条渲染路径共用的不可变溯源块（**AI 输出永不覆盖 manifest**）。
- **job 链路**（`job_handlers._handle_document_generation`）：路由先落确定性模板（中文）并排队 job；handler 在 worker 线程 `asyncio.run(_run_ai_stage(...))`（feature=document_generation、`REPORT_OUTPUT_SCHEMA`、按 document_type 给中文章节结构 prompt、`min_output_tokens=8192`）——succeeded 则 AI 渲染中文 Markdown 追加溯源块落新版本；任何失败（not_configured/failed/截断/预算拒绝）回退完整中文模板。`DocumentVersion.ai_status/ai_error_code`（迁移 0010）记录产出来源：NULL=旧数据、succeeded=AI、fallback=模板——交付页按错误码显示中文提示条，**模板回退绝不静默**。`generate_document` 按 (project_id, document_type) find-or-create（第八批）：重生成复用同一文档追加版本、更新标题，不再堆积同名文档。job input 含 `title`/`project_id`。
- **max_tokens 机制**：`_run_ai_stage` 的可选 `min_output_tokens` 抬高首次输出上限——首次 = max(max_output, min_output_tokens)，重试翻倍，**均受 `HARD_OUTPUT_CAP=16384` 硬顶**（第八批起不再受 per_request 约束）。
- **实测成本**（deepseek-v4-flash，2026-09-02）：PRD 成功样本 prompt 1869 / completion 7087；周报成功样本 prompt 3746 / completion 7152——单次文档生成约 9-11k tokens；一次 LLM_PROVIDER_ERROR 降级为模板（前端如实提示）。
- 前端 stage11 为极简交付页（第八批；第九批补状态水合）：文档类型选择 + 标题 + 生成（job 轮询，旧内容在生成期间保持可见；第十六批起切页回来凭 `generation_job_id` 自动恢复轮询，生成期间按钮禁用）+ 可编辑正文 + 导出 Markdown；**挂载/切项目/切文档类型时按 (project, document_type) 水合已生成文档（GET /documents?project_id=，同 type 取 created_at 最新），切页不丢失已生成内容**，恢复的内容在编辑框上方标注「内容恢复自最近生成的版本 {时间}」；`ai_status !== "succeeded"` 的版本在正文上方显示按错误码分类的中文提示条（预算阀门/provider 错误/未配置），**模板回退绝不静默**。保存新版本按钮与文档/版本列表已按第八批极简化移除（端点保留）；AI 助手抽屉已从前端移除（AppShell），AI 横幅仅保留 WorkflowFrame 页面级一条；后端 copilot 端点与测试保留。

---

## 8. API 面貌（第三批起按 routers/ 域拆分）

约 136 个端点（清单由 tests/test_route_manifest.py 冻结），按资源域：
- **Auth**：register/login/refresh/me（注册即建 workspace；登录失败统一报错不泄露邮箱存在性；均写审计）
- **Workspace**：list/patch/settings(GET,PATCH)/members/metrics 字典 CRUD（含 `/api/v1/settings`、`/api/v1/metrics` 别名）
- **Audit**：`GET /audit-logs`（workspace 级，newest-first）
- **Projects**：CRUD + overview + workflow-status + tasks CRUD/links + `DELETE`（显式确认）
- **Datasets**：upload/upload-batch/versions/schema(PATCH)/schema-review/preview/quality-report/DELETE（owner + 显式确认；cleaning 三端点已随第十一批删除，`GET /datasets` 的 versions payload 自第十一批起携带水合的 `quality_report`，前端统计卡显示真实数字）
- **Analysis**：validate-config、runs CRUD、rerun、artifacts
- **Feedback**：items CRUD/import/imports、clusters generate/patch/link-task、notes GET/POST/PATCH（V1.1）
- **Insights / Problems / Solutions / Decisions / Approvals / Documents**：按第 6 节流程
- **Interview（第四批）**：`POST /projects/{id}/interview/rounds`、`GET/POST /interview-questions`、`PATCH /interview-questions/{id}`、`POST /ai/distill-interview`
- **Archive（第九批）**：`POST /projects/{id}/archive`、`POST /projects/{id}/unarchive`（幂等，editor+）
- **AI + Copilot + Jobs**：见第 7 节

**V1.1 legacy 标记**：`_V11_LEGACY_API_PREFIXES`（workspaces/tasks/approval-requests/decision-proposals/jobs/copilot sessions/feedback-items/feedback-clusters）的响应带 `Deprecation: true`、`Sunset: 2027-01-01` 头（middleware `mark_legacy_api_surfaces`）。

**安全细节**（实测存在）：错误响应固定信封并对 validation details 递归脱敏（`_redact_validation_details`）；`_check_evidence_scope` 对 evidence 引用做存在性 + 跨 workspace/project 边界校验（BUG-004）；登录失败统一文案。

---

## 9. 前端结构与数据流

- **认证流**：login 页 `POST /auth/login` → `saveSession()`（localStorage + 镜像 cookie，cookie max-age 解析 JWT exp 对齐，`lib/api.ts:43`）→ middleware 放行；任意 401 统一 `clearSession()` + 跳 `/login`（`api.ts:22`）。
- **工作台数据流**：`loadWorkflowSnapshot()` `Promise.allSettled` 并行拉 11 类列表（含 `/approval-requests` 仅 pending、第四批新增的 `/interview-questions`）+ `/me`，容错收集 loadErrors → `hydrateActiveVersion` 补拉 schema/质量报告 → `stepCompletion()` 算门控（11 阶段；第 6 步=存在 answered 采访问题或手动补充）→ `WorkflowFrame` 渲染门控/进度。
- **工作台上传流**（`app/(workspace)/page.tsx`，第十批两步链路）：选文件 → `upload-batch` → 轮询 job（`TERMINAL_JOB_STATUS`，POLL_LIMIT=150）→ `POST auto-report/compute` **秒级渲染数据概况**（ReportMarkdown + ECharts 图表，标签「确定性统计」）→ 紧接着 `POST auto-reports/{id}/narrate` 拿 job id 并轮询，期间报告下方显示「AI 解读生成中…」轻量进行条、**页面其余部分与导航完全可操作**（离开页面不影响服务端 job；回来从报告列表读到最新状态）；叙述失败/旧 `not_configured` 报告显示按错误码分类的中文原因 + 「重试 AI 解读」/「补生成 AI 解读」按钮（重调 narrate）→ confirm/重新生成走同一 compute+narrate 链。
- **各阶段页**均为「门控包裹 + API 薄封装」模式；AI 起草按钮调用对应 `/ai/*` 端点，返回的 draft 填充表单，用户修改后走常规 POST/PATCH 落库。
- **设置页**：workspace 设置（时区/AI 模型/输出上限/双层 token 预算/feature flags）+ `/health/ai`、`/health/ready` 健康面板。
- **当前项目作用域（第九批）**：`getActiveProjectId/setActiveProjectId`（lib/workflow.ts）持久化到 localStorage `apw_active_project` 并派发 `apw-project-changed`；`loadWorkflowSnapshot` 先解析项目列表（include_archived）再发起作用域请求（insights/problems/solutions/decision-proposals/documents/datasets/analysis-runs/interview-questions 追加 `?project_id=`，approvals 保持 workspace 级）；持久化 id 失效（删除/归档）回退到第一个活跃项目。`setActiveProjectId` 幂等（第九批修复，2026-09-04）：事件仅在实际变更时派发；快照回退比较使用归一化 null（`activeProject?.id ?? null`），修复了「归档最后一个活跃项目后历史页无限刷新」的死循环（原守卫 `null !== undefined` 恒真 + 无条件派发事件形成循环）。无活跃项目时快照业务列表为空（第九批逻辑修复，2026-09-04）——`activeProjectId === null` 时跳过全部十类业务列表请求，已归档数据不进入全局快照，门控回到初始状态；`projects?include_archived=true` 与 `/me` 不受影响（历史页与切换器仍需要）。`useWorkflowSnapshot` 监听该事件自动刷新。`/history` 列表页 + `/history/[projectId]` 只读回看页（自拉九类列表，复用 ReportMarkdown 渲染报告与文档）。
- **legacy 路由**：`legacyRouteAliases`（navigation.ts）由 middleware 308 重定向到新 IA。

---

## 10. 当前完成度

**已实现且验证**：
- 后端测试套件 **285 passed, 1 xfailed，0 警告**（2026-09-04 实测运行；第十五批 test_grounding.py 改写为报告唯一化语义并新增 REPORT_MISSING 优雅降级用例；第十四批新增 test_parsing.py 34 个与 test_compute_v2.py 4 个计算层 v2 测试；第十二批新增 test_digest.py 与 test_group_comparison.py 19 个计算加强测试；第十批新增 test_auto_report_split.py 10 个先算后叙测试，第九批新增 test_archive.py 9 个归档/守卫测试）；含 route manifest 冻结测试、test_guardrails.py 守护测试、test_interview.py 采访/蒸馏测试、第七批 test_document_generation.py、第八批 test_budget_model.py 直花/硬顶/总阀门测试）。覆盖：RBAC 与 workspace 隔离、数据管线（上传/版本/清洗/质量）、分析引擎全类型、AI 降级边界（无 key 绝不 500、输出契约、上下文白名单、反馈原文不外泄）、决策链规则（证据强制/落选理由/审批失效）、项目级联删除、报告叙述消毒。
- 14 个 Alembic 迁移可从零建库（0014 = drop superseded_at）；`.env` 已配置 DeepSeek；前后端均可本地跑通。
- 前端 11 阶段页面、工作台、数据管理、设置页齐全（第四批起）。
- **全链路已真实手动冒烟走通**（12 阶段版 2026-08-30：上传→报告→洞察→讨论→问题→方案→决策→PRD；11 阶段版 2026-09-01：上传→报告→采访→蒸馏→裁决→问题→方案→决策→PRD）。
- **`ruff check app tests` 零告警**（第四批清掉 tests 基线 3 条 + 连带 2 条；unittest 弃用告警从 2494 → 0）。

**进行中（V1.1 迁移收尾）**：
- legacy 表/API 与新表/API 并存，代码中大量兼容分支（`_migrate_legacy_metric_dictionary`、`_drop_feedback_content`、feedback 双轨等）；`0005` 迁移默认不删 legacy 表，需显式 `V11_DROP_LEGACY_TABLES=true`。
- Copilot 的 `create_insight_draft` 等 4 个写工具在白名单中但**尚无服务端 handler**（`ReadOnlyToolRegistry.execute` 会拒绝）。

**缺失项**：
- git 仓库已初始化并按批提交（conventional commits，未配置远端、未 push）；无根 README、无 CI 流水线。
- 前端零测试（无测试框架）。
- 无国际化层（界面中文硬编码）。

---

## 11. 已知问题与技术债（按影响排序）

1. ~~**`main.py` 5970 行巨型单文件**~~（第三批已解决：拆为 149 行组装层 + `common.py` + 13 routers + 10 services，路由清单由 manifest 测试冻结）。
2. **V1.1 迁移未收尾**：双套模型/API 并存（feedback_items+clusters vs feedback_notes；tasks/approvals 待淘汰），每个新功能都要处理新旧两轨。
3. **两套并行 schema 机制**：Alembic 之外，`db.py:_repair_missing_columns()` 启动时给已存在表补列（无外键）。dev 便利但与迁移漂移风险。
4. **JobExecutor 进程内限制**：单进程假设（多 worker 会重复执行/丢任务）；无自动重试退避（仅手动 `POST /jobs/{id}/retry`，且需 `_retryable` 标记）；长任务占 BackgroundTasks 线程。
5. **AI 预算并发窗口（第八批后残余）**：`with_for_update` 在 SQLite 是 no-op；running 行按各自 worst_case 预留后，daily 最多被在途调用突破一个 worst_case（已接受的设计）；`_workspace_token_usage` 每次全扫当日 AIRun，量大后变慢；copilot_message 的独立预检仍是旧 `reserved > daily` 形式，未统一到 worst_case 投影。
6. **代码卫生（第四批已清理大半）**：~~`models.now()` 用已废弃 `datetime.utcnow()`~~（已改为 `datetime.now(UTC).replace(tzinfo=None)`，naive-UTC 语义不变、由 test_guardrails 锁定）；~~`@app.on_event("startup")`~~（已迁移 FastAPI lifespan，行为等价）；~~tests 基线 ruff 告警~~（已清零）。仍存在：`openai` SDK 不在 pyproject 依赖（实际依赖 httpx fallback 或需手动安装）；CORS 未配置时回退 `["*"]` 且 `allow_credentials=True`。
7. **前端认证是软门禁**：middleware 只查 `apw_session=1` cookie 存在性（可伪造绕过页面守卫），真实鉴权仅在 API 层——设计上可接受但需明确这不是安全边界。JWT 在 localStorage（常规 XSS 暴露面），无服务端吊销。
8. **SSE 非实时**：copilot 事件回放式，用户体验依赖轮询 job 状态；`DeepSeekAdapter.stream()` 是伪流（一次性 complete 后整体 yield）。
9. **文件存储在本机磁盘**：`data/uploads|processed|exports`，无对象存储；`_purge_project` 物理删除不可恢复（有审计）。第四批起删除前经 `_safe_data_file` 做 DATA_ROOT 越界防护（防篡改行任意 unlink）。
10. **测试基建的小脆弱点**：`tests/conftest.py` 必须在 import app 前设置环境变量（ruff 已按文件豁免 E402 并有注释）；测试库文件在 `output/test-runtime`（rebuild on each run）。
11. **真实 key 下的 AI 输出可靠性（截断部分已于第五批修复）**：~~截断后仍标 succeeded + 空草稿~~（第五批起截断/解析失败 → `failed` + `LLM_TRUNCATED`/`INVALID_AI_OUTPUT` + 单次重试，前端如实提示；第八批蒸馏加 prompt 规模约束后正常输出约 2400 tokens）。残余：`deepseek-v4-flash` 仍偶发返回非 JSON 或 provider 错误（第七/八批冒烟各遇一次，降级路径行为正确）；Copilot 编排 plan 校验（`INVALID_ANALYSIS_PLAN`）真实 key 下偶发失败降级，未修。
12. **项目删除与解析任务的竞态可泄漏上传文件（第四批冒烟实证，未修）**：真实 uvicorn 下上传后立即删除项目、若后台 `dataset_parse` job 尚未完成，`_purge_project` 返回 `files: 0` 且上传文件遗留在磁盘（复现：上传后 <1s 删除；等待解析完成再删则 `files: 1` 正常）。版本行会被级联删除，泄漏仅限磁盘文件。待办方向：删除时校验无 in-flight job，或解析完成后回收孤儿文件。
13. **历史事故记录（已修复）**：第三批 Phase 2 的 AST 切割脚本曾把 `_purge_project` 中对 `_safe_data_file` 的调用连同注释一并丢弃（拆分后该函数一度成为无调用者的死代码，且删除路径失去越界防护，提交 42728e5..132a25e 期间生效）。第四批重新接线并由 test_guardrails 锁定；同时纠正第三批汇报中"死代码"的定性——根源是脚本丢行，不是基线死代码。
14. **文档生成上下文丢失洞察正文（第八批冒烟实证，未修）**：`_build_document_context` 的洞察 payload 用 `content` 键，而 `content` 在 `_FEEDBACK_CONTENT_KEYS` 黑名单内——`build_ai_context` 装配时洞察正文被静默剥离，文档 AI 实际只能看到洞察标题/置信度/证据骨架（采访回答的 question/answer 键不受影响）。修复方向：洞察 payload 改用非保留键（如 `body`）或为洞察开专用通道；因涉防火墙（第八批禁改）未动。
15. **设置项 `ai_per_request_token_budget` 已无拦截职责但仍在设置 UI 展示**（第八批起仅作 worst_case 预留的兜底参数），用户可能误以为它限流；建议后续在设置页标注或移除展示。
16. **`_replace_version_columns` 关系缓存缺陷（第十二批发现并修复，2026-09-04）**：新列经裸 `db.add(DataColumn(dataset_version_id=...))` 落库（FK 不经 back_populates 更新已加载的 `version.columns` 缓存），导致自动管线内所有非 EDA 分析（留存/趋势/异常/分组）的列校验拿到空列集而全部以 `reason=config` 被跳过——**自自动管线引入以来这些分析从未真正落库**（既有测试只锁 plan 未锁 run，故未暴露）。修复：新列经 `version.columns.append()` 追加；test_group_comparison.py 以业务表上传断言 group_comparison run 真实落库锁定。

---

## 12. 给后续开发对话的关键事实速查

**第三批（2026-08-30）拆分后的落位规则**：
- 新增 API 端点：写到对应域的 `app/routers/<域>.py`（`router = APIRouter()` + `@router.<method>("/api/v1/...")` 路径全写），在 `main.py` 加 `app.include_router(...)`；请求模型进 `schemas.py`。`tests/test_route_manifest.py` 会冻结断言全部 (path, methods, name)——路由变更必须同步重生成该清单。
- 新增业务逻辑：放到 `app/services/<域>.py`；被多个 router 共用的 helper 必须下沉 services（routers 之间禁止互导，services 禁止反向导入 routers）。`ok()/error()/model_dict/paged` 等信封工具在 `common.py`；`_require_pandas()` 是 pandas 懒加载哨兵（使用方在函数内 `pd = _require_pandas()`，运行时禁止模块顶层 import pandas）。
- job handler：`app/services/job_handlers.py`，`job_executor` 全仓库唯一实例在此；新增 handler 后在 `_register_job_handlers()` 注册（main.py 末尾恰好调用一次）。
- 链式地基（第十三批，第十五批简化）：采访/蒸馏的地基 = 最新报告的聚合 + 底层产物细节；digest findings 同时落库为真实 finding artifacts，证据链（洞察 evidence 指向真实资源 id）与地基链（每步基于上一步结果思考）分离。**报告唯一化（第十五批，2026-09-04 完成）**：一个项目任意时刻只有一份分析报告——compute 创建新报告后直接删除该项目全部旧报告（无论是否已确认，确认历史由 audit_logs 保留），取代标记 superseded_at 随迁移 0014 删除；narrate job 遇报告已删以 REPORT_MISSING 诚实失败（不重试）；前端报告历史只显示当前一份，文案注明「重新生成会替换旧报告」。dev 库升级需 `alembic upgrade head`（0014）。
- 归档语义（第九批）：归档只能走 `POST /projects/{id}/archive|unarchive`（ProjectPatch 不含 status）；归档项目的 editor+ 写路径全部 409 `PROJECT_ARCHIVED`（project_for 与各路由的 `_ensure_project_active` 守卫），viewer 读与 owner 删除不受限。前端「当前项目」持久化键为 localStorage `apw_active_project`。
- 数据页语义（第十一批，2026-09-04 完成）：清洗全链路已删除（`cleaning_operations` 表与模型保留、不提交 cleanup 迁移）；`GET /datasets` 的 versions 携带水合 `quality_report`（统计卡真实数字）；数据页「项目上下文」只读展示当前活跃项目（`getActiveProjectId()` + `apw-project-changed` 事件跟随刷新），切换/新建项目统一在工作台完成，上传绑定当前活跃项目；数据集详情页字段定义为只读（后端 PATCH schema 端点保留）。
- 计算加强（第十二批，2026-09-04 完成）：新分析类型 `group_comparison`（engine `run_group_comparison`，自动计划上限 4，业务表自动选中）；`analytics/digest.py` 的 findings digest 写入报告 `deterministic_json.findings` 并注入叙述/文档 AI 上下文（firewall 白名单零改动）；新增测试 `test_digest.py`/`test_group_comparison.py`（后端 240 用例）。
- 新增 AI 能力：服务逻辑进 `services/ai_stages.py`（复用 `_run_ai_stage()` 模板，可传 `response_schema`/`output_validator`/`empty_output` 定义阶段契约），路由壳进 `routers/ai.py`；上下文必须过 `build_ai_context`，AI 结果一律 draft；Copilot 的 insights 上下文由服务端注入，客户端传入的一律丢弃。
- 分析类型扩展点：`analytics/engine.py`（计算）+ `services/analysis_pipeline.py`（`_analysis_artifacts` 持久化映射、`_analysis_config_validation`、`_auto_analysis_plan`）+ `deepseek.py` 工具白名单（若暴露给 Copilot）。
- 前端新页面的惯例：`app/(workspace)/` 下建目录，用 `WorkflowFrame` 的 `WorkflowHeader/WorkflowGate` 包裹，门控逻辑改 `lib/workflow.ts` 的 `stepCompletion()`，导航加 `lib/navigation.ts`。
- 测试运行：`cd apps/api && .venv/Scripts/python.exe -m pytest tests -q`（Windows；测试自备隔离 SQLite 与空 DeepSeek key）。
