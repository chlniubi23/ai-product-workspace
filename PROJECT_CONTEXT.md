# PROJECT_CONTEXT — AI Product Workspace 项目事实基准

> **本次核对：2026-09-13**（代码全量通读 + 测试实测运行 + git 状态核对）。
> 结论来源标注约定：**【实测】** = 本次 2026-09-13 实际运行/读取确认；**【沿用】** = 沿自 2026-09-06 版本且本次未逐行复核；**【新发现】** = 本次首次记录。
> 行号引用为写作时快照，代码变动后以函数/类名为准。**本文件只记录真实状态，不含推测。**

**核对基线**：git HEAD = `22657c2`（batch 27 complete — motion and data-typography polish）**+ 2026-09-13 仓库整理新增的 8 条提交**；工作区干净（见 §13）。
**最近增量**：2026-09-13 执行 Phase 0 工程收口（`docs/dev-prompts/phase0-cleanup.md`）——仅补依赖声明、ruff 归零、测试归位、清临时文件，**未改任何业务行为**；受影响条目已就地更新（§3 / §3.1 / §4 / §10 / §11 / §13）。
**其后增量**：2026-09-13 执行 README 文档对齐（仅 `README.md`，**零代码改动**）——router 数 13→14、main.py 152→151 行、DB 表清单按 `V11_CORE_TABLE_NAMES`/`V11_LEGACY_TABLE_NAMES` 校正、补全后端依赖清单、补全空的「仓库结构」章节、两处 ASCII 框按显示宽度重新对齐；受影响条目仅 §13.1。
**再后增量**：2026-09-13 执行仓库整理与提交（`.gitignore` + 8 条语义提交，见 §13.1）——**唯一代码类改动**是 `tests/test_report_narration.py` 的产物覆盖断言随 EDA 产物合并由 `>= 3` 改为 `>= 2`（业务代码零改动）；`.workbuddy/`、`.workbuddy-ai/` 已忽略且目录保留。
**本批增量**：2026-09-13 修复计算层 4 处正确性缺陷（未提交，见 §13.4 / §11）——血缘源列误判、离群值三套口径、质量分类型维度恒 0、中文长句 identifier 误判；未改 API 契约 / 防火墙白名单 / 11 阶段门控 / 迁移，测试由 366 增至 **375 passed, 1 xfailed**。
**最新增量**：2026-09-13 修复「每跑一次测试回收站堆积约 1 万个文件」（未提交，见 §13.4 / §11.15）——测试运行时迁到 `%TEMP%\apw-test-runtime`、测试库改 `journal_mode=MEMORY` + `synchronous=OFF`，全量耗时 210s → **78.74s**、回收站增量 **0**。
**第二批增量**：2026-09-13 落地双维度质量 + 可审计排除明细 + 死代码清理（未提交，见 §13.4）——`analysis_quality` 不再恒 0、`excluded_correlation_pairs_detail` 可人工审计（不出站）、抽取列防同名覆盖、删除 `enhanced_engine.py` / `test_enhanced_engine.py` / `detect_outliers_lof`、修 `types.py` 的 0 值 bug；全量 **374 passed, 1 xfailed**（375 收集）。

**协作协议（用户强制，2026-09-13 起）**：每次开发任务结束后必须核对本文件，只增量更新真正变化的条目；并向用户回报「改了什么 / 是否影响架构 / 是否需要更新本文件 / 本文件更新了哪些条目」四项——详见 §12.1。

---

## 1. 项目定位与业务主张

**AI Product Workspace** 是一个「AI 辅助、人工主导」的产品数据分析与决策工作台（中文界面）。

核心设计原则（在代码中被反复强制执行，**非文档口号**）：

- **数据计算全部确定性**：所有统计数字由 pandas 引擎（`apps/api/app/analytics/engine.py`）计算，LLM 永远不自己算数。**【实测】**
- **AI 只产草稿**：AI 输出永远是 draft 状态，永不自动 confirm；每个阶段的人工确认是独立动作。**【实测】**
- **证据链贯穿**：每条 claim 必须携带 evidence 引用（`ai_context.AI_OUTPUT_SCHEMA` / `validate_ai_output`），采纳洞察强制要求非空证据。**【实测】**
- **硬性 AI 边界**：流水线阶段 1–5 之前无任何 AI 参与，AI 仅在阶段 6+ 出现。**【实测】**
- **多租户隔离**：一切资源挂 workspace，所有读取路径做成员校验 + workspace/project 边界检查（`services/access.py`、`services/evidence.py`）。**【实测】**

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
                                       开发/测试用       httpx fallback;  data/processed
                                       SQLite fallback)  openai SDK 未声明) data/exports
```

- **通信**：纯 REST，统一响应信封 `{"data": ..., "meta": {request_id, ...}}` / `{"error": {code, message, details}}`（`app/main.py` 的 `ok()`/`error()`）。**【实测】**
- **鉴权**：HS256 JWT（`app/auth.py`），bcrypt 密码（>72 字节自动 pre-hash 标记 `bcrypt_sha256$`，兼容旧 pbkdf2 哈希并登录时升级）。JWT 存前端 localStorage；登录时镜像 `apw_session=1` cookie，Next.js middleware 只检查 cookie 存在性做**页面级软门禁**，真实鉴权在 API 层。**【实测】**
- **异步任务**：无外部队列。`app/infrastructure/jobs.py` 的 `JobExecutor` 通过 FastAPI BackgroundTasks 在进程内执行 DB 持久化的 Job（状态机 `queued→running→succeeded/failed/cancelled`），启动时 `recover_pending()` 重放未完成 job。**【实测】** handler 由 `_register_job_handlers()`（`services/job_handlers.py` 末尾）注册 **6 个**：`dataset_parse`、`analysis_run`、`feedback_import`、`feedback_cluster_generation`、`document_generation`、`auto_report_narration`。
- **数据库**：生产 MySQL 8（docker-compose 仅含 mysql 一个服务）；`DATABASE_URL` 未配置或 `ALLOW_SQLITE_FALLBACK=true` 时可回退 SQLite（`app/db.py`，fallback 状态经 `/health/ready` 暴露且**判为 not_ready**）。`db.py` 另含 `_repair_missing_columns()` 运行时补列安全网（dev 便利，与 Alembic 并行的第二套 schema 机制）。**【实测】**
- **迁移**：`apps/api/alembic/versions/0001..0017`，共 **17 个**（0013=data_columns.source；0014=drop auto_analysis_reports.superseded_at；0015=content_markdown MEDIUMTEXT；0016=interview_summaries；0017=data_columns.semantic_label/semantic_description）。其中 `0005_v11_slim_schema` 仅在 `V11_DROP_LEGACY_TABLES=true` 时删 legacy 表（默认只加不减）。**【实测】**
- **路由规模**：**【实测】** `app.main:app` 注册 **14 个 router**，路由记录 **137 条**（GET 60 / POST 53 / PATCH 18 / DELETE 6 / HEAD 4 自动生成）。清单由 `tests/test_route_manifest.py` 冻结。

---

## 3. 目录结构（实际状态，【实测】）

```
AI_Product_Workspace/
├── .env / .env.example          # 配置（DEEPSEEK_API_KEY 等已配置）
├── docker-compose.yml           # 仅 mysql:8.0 服务
├── README.md                    # 已扩写（工作区中已暂存未提交，见 §13）
├── PROJECT_CONTEXT.md           # 本文件
├── output/test-runtime/         # 旧测试运行产物（gitignore；现运行时已迁至 %TEMP%\apw-test-runtime）
└── apps/
    ├── api/                     # FastAPI 后端（151 行 main.py 组装层）
    │   ├── app/
    │   │   ├── main.py          #   151 行：app 实例 + CORS/legacy 中间件 + 异常处理器 + lifespan + include_router ×14 + health 三端点 + _register_job_handlers()
    │   │   ├── common.py        #   112 行：ok()/error() 信封、_request_id、serialize/model_dict、page_params/paged、_require_pandas 懒加载哨兵、_redact_validation_details
    │   │   ├── ai_context.py    #   1326 行：AI 出站防火墙 + 全部输出契约（11 个 schema/校验器）
    │   │   ├── models.py        #   708 行：V11 核心 20 表 + legacy 10 表
    │   │   ├── schemas.py       #   461 行
    │   │   ├── auth.py / config.py / db.py
    │   │   ├── services/        #   业务逻辑层（12 个文件；禁止反向导入 routers）
    │   │   │   ├── access.py             #   membership/project_for/_dataset_version_for/_problem_for/_ensure_project_active
    │   │   │   ├── audit.py              #   audit()/audit_user_workspaces
    │   │   │   ├── workspace_settings.py #   工作区设置/双层 token 预算/_workspace_token_usage/_reject_ai_budget
    │   │   │   ├── evidence.py           #   _check_evidence_scope/证据强制/引用范围校验
    │   │   │   ├── datasets.py           #   读文件/字段 schema/质量摘要/_safe_data_file 越界防护
    │   │   │   ├── analysis_pipeline.py  #   分析产物持久化/配置校验/自动分析计划/_run_auto_analyses
    │   │   │   ├── ai_stages.py          #   787 行：_run_ai_stage 模板/_deepseek_answer/Copilot 编排胶水/_AI_ARTIFACT_KEY_MAP
    │   │   │   ├── auto_report.py        #   556 行：报告聚合（pandas）+ 确定性骨架 + _narrate_report
    │   │   │   ├── documents.py          #   740 行：文档上下文装配 + Markdown 渲染 + 三遍生成提示词
    │   │   │   ├── interview.py          #   600 行：自适应一问一答 + 收尾小结 + 蒸馏落库
    │   │   │   ├── field_semantics.py    #   142 行：LLM 字段语义字典（仅 _handle_dataset_parse 调用）
    │   │   │   └── job_handlers.py       #   771 行：job_executor 唯一实例 + 6 个 handler + _register_job_handlers
    │   │   ├── routers/         #   14 个文件：auth/workspaces/projects/datasets/analysis/insights/interview/feedback/problems/decisions/documents/jobs/copilot/ai
    │   │   ├── analytics/       #   计算层（见 §3.1）
    │   │   └── infrastructure/  #   jobs.py（285 行）/ llm/deepseek.py（828 行）
    │   ├── alembic/versions/    #   17 个迁移（0001..0017）
    │   ├── tests/               #   28 个测试文件，375 用例
    │   ├── pyproject.toml / requirements.txt
    │   └── Procfile
    └── web/                     # Next.js 14 前端
        ├── middleware.ts        #   登录门禁 + legacy 路由重定向
        ├── app/
        │   ├── (auth)/login/
        │   └── (workspace)/
        │       ├── page.tsx     #   ★ 工作台 = 流水线阶段 1–5（904 行）
        │       ├── data/page.tsx、data/[datasetId]/page.tsx
        │       └── history/page.tsx、history/[projectId]/page.tsx
        │       └── stage6-interview .. stage11-prd/   #   六个有序阶段页
        │       └── （settings 页已随第二十六批删除，目录不存在）
        ├── components/          #   layout/AppShell.tsx(638) · workflow/WorkflowFrame.tsx(180) · common/JobProgress.tsx · analysis/{ChartRenderer,ReportMarkdown} · hooks/useCountUp.ts
        └── lib/                 #   api.ts · workflow.ts(490) · navigation.ts · chartOption.ts(295) · format.ts · upload.ts
```

### 3.1 `analytics/` 计算层实际文件（**【实测】** 2026-09-13 计算层修复后）

| 文件 | 行数 | 状态 | 说明 |
|---|---|---|---|
| `engine.py` | 771 | **已修改未提交** | **生产引擎**：`run_eda` / `run_trend_analysis` / `run_funnel_analysis` / `run_retention_analysis` / `run_anomaly_detection` / `run_group_comparison` + `choose_trend_frequency` + 函数式 facade；EDA 相关性统一接入增强计算，保留 legacy correlations 契约；**本批**：payload 新增 `excluded_correlation_pairs_detail`（≤20 条，人工审计用，不出站） |
| `quality.py` | 501 | **已修改未提交** | `assess_quality`（生产契约：`overall_score`/`status` 仍是唯一门控）+ 双维度质量（`DataQualityMetrics`/`AnalysisQualityMetrics`/`ComprehensiveQualityReport`/`generate_quality_report`）；**计算层修复批**：类型维度改为未传 `expected_types` 时自动派生（`_derived_expected_type`，仅 numeric/datetime/boolean），outlier 段改用 `outliers.compute_column_outliers` |
| `parsing.py` | 213 | **已修改未提交** | v2 解析（千分位/货币/中文日期/k\|M\|万\|亿）+ `infer_column_type_v2`；**计算层修复批**：identifier 判据收紧为 `_looks_like_identifier`（无空白 + 长度 ≤ `IDENTIFIER_MAX_LENGTH=40` + 无句读标点），无空格中文长句回落 `text` |
| `text_metrics.py` | 149 | 已提交 | 自由文本数值列抽取（`{源列}__{指标}`）；只扫描 `semantic_type ∈ {text, category}` 的列，因此依赖 `parsing.py` 的 identifier 判据 |
| `digest.py` | 355 | **已修改未提交** | 规则化 findings digest（纯函数，阈值模块常量）；**计算层修复批**：outlier 文案由「IQR 离群值」改为「离群值」，与新口径（IQR∪Z 并集）一致 |
| `dag.py` | 340 | **已修改未提交** | 派生列血缘识别与伪相关排除；**计算层修复批**：`_match_builtin_rules` 只认 `rule.name`（不再把源列误判为派生列）、补上策略 2 缺失的 `derivation_description` |
| `corelation.py` | 253 | **已修改未提交** | 显著性检验（p 值）+ 稳健相关 + 多方法（**依赖 scipy**）；**本批**：血缘排除由计数改为明细列表，并新增 `EXCLUDED_PAIRS_DETAIL_LIMIT=20` |
| `outliers.py` | 437 | **已修改未提交** | 对象级离群值（IQR/Z-score 并集统一口径）；**本批**：删除 `detect_outliers_lof`（无调用点 + `pred[int(idx)]` 索引错位），**scikit-learn 由此失去唯一消费方** |
| `types.py` | 307 | **已修改未提交** | 类型感知统计（序数/占比/二元）；**本批**：`OrdinalStatistics.to_dict` 与 mode 判定改用 `is not None`，0 值不再被当缺失 |
| `text_metrics.py` | 177 | **已修改未提交** | 自由文本数值列抽取（`{源列}__{指标}`）；**本批**：派生列撞名时追加确定性后缀 `__dupN` 并写 `renamed_from`，report 记录同一 normalized 指标的 `display_variants` |

---

## 4. 技术栈（实际声明，**【实测】**）

| 层 | 技术 |
|---|---|
| 后端框架 | FastAPI ≥0.111, uvicorn[standard], pydantic v2 + pydantic-settings |
| ORM/迁移 | SQLAlchemy 2.0, Alembic 1.13, PyMySQL（requirements.txt 另含 cryptography） |
| 数据处理 | pandas 2.1+, numpy 1.26+, scipy 1.11+, openpyxl |
| 认证 | PyJWT (HS256), bcrypt（rounds=12） |
| LLM | DeepSeek `chat/completions`（OpenAI 兼容），默认模型 `deepseek-chat`；调用走 **httpx fallback**（`openai` SDK 不在依赖中） |
| 前端 | Next.js 14.2 (App Router), React 18, TailwindCSS 3.4, ECharts 5.6, lucide-react |
| 存储 | MySQL 8（生产）/ SQLite（dev+test） |
| 测试 | pytest（实测 `361 passed, 1 xfailed`，362 收集，207.04s） |
| Lint | ruff（line-length 120, target py311）；eslint + prettier（前端） |

**可选依赖**：`scikit-learn` 走 `[project.optional-dependencies] ml`（`scikit-learn>=1.4,<2`）。~~仅供 `outliers.py` 的 LOF 使用~~ **2026-09-13：`detect_outliers_lof` 已随死代码清理删除，`scikit-learn` 当前**没有消费方**；`[ml]` extra 暂保留（删除会让 `pip install -e ".[ml]"` 直接报错），pyproject 注释已同步改为"当前无消费方"。`scipy` 为必需依赖（`corelation.py` 顶层导入）。

---

## 5. 核心数据模型（`apps/api/app/models.py`，**【实测】**）

**V1.1 核心表**（`V11_CORE_TABLE_NAMES`，20 张）：
`users` / `projects` / `metric_definitions` / `datasets` / `dataset_versions` / `data_columns` / `data_quality_reports` / `cleaning_operations` / `analysis_runs` / `analysis_artifacts` / `insights` / `product_problems` / `solution_options` / `decision_proposals` / `copilot_sessions` / `copilot_messages` / `documents` / `document_versions` / `ai_runs` / `feedback_notes`

**Legacy 表**（`V11_LEGACY_TABLE_NAMES`，10 张，淘汰窗口中仍被 API 使用）：
`workspaces` / `workspace_members` / `tasks` / `task_links` / `feedback_items` / `feedback_clusters` / `feedback_cluster_items` / `approval_requests` / `jobs` / `audit_logs`

**迁移 0007/0008 新增**：`analysis_report_narrations`（分析叙述，与 AnalysisRun 故意分离以保确定性）/ `auto_analysis_reports`（项目级自动报告）
**迁移 0009**：`interview_questions`（round_number 0=手动补充 / ≥1=AI 轮次；status pending|answered|skipped；source ai|manual）
**迁移 0010/0011**：`document_versions.ai_status/ai_error_code`（NULL=旧数据 / succeeded=AI / fallback=模板）/ `projects.archived_at`
**迁移 0016**：`interview_summaries`（每项目一条，summary 存 JSON，幂等覆盖）
**迁移 0017**：`data_columns.semantic_label`(VARCHAR 120) / `semantic_description`(VARCHAR 600)；数据集整体标签存 `dataset_versions.schema_json["dataset_label"]`

**第十一批（2026-09-04）清洗功能删除**：清洗全链路（3 端点、`dataset_cleaning` job、`apply_cleaning`）已删除；`cleaning_operations` 表与模型**保留不 drop**（仅项目级联删除仍触达），`assess_quality` 质量报告完整保留。**【沿用，本次确认模型仍在】**

**关键模型语义（来自 docstring，【实测】）**：
- `DatasetVersion.schema_reviewed_at`（用户看过字段角色）与 `schema_auto_accepted_at`（解析 job 代接受）**是两列**——UI 必须区分「人看过」和「系统猜的」。
- `ProductProblem.source_insight_ids`：问题必须回链洞察才能 confirm，否则「凭直觉的问题」会流入决策。
- `SolutionOption.reject_reason`：选定方案时其余落选方案必须写落选理由。
- `AIRun`：所有 AI 调用的账本（feature_name/provider/model/tokens/latency/status/input_summary_json）。
- `AutoAnalysisReport.status`：`draft|succeeded|not_configured|failed|confirmed`——`succeeded` 表示 AI 叙述已验证入库，`not_configured` 表示只有确定性统计。
- `DocumentVersion.ai_status`：NULL=旧数据、succeeded=AI、fallback=模板回退（**绝不静默**，交付页按错误码显示中文提示条）。

---

## 6. 核心业务流程：11 阶段流水线（**【实测】** 与 `lib/workflow.ts` 一致）

前端 `lib/workflow.ts` 的 `stepCompletion()` 是 11 个门控的**事实定义**（`STAGE_COUNT = 11`）：

| 阶段 | 页面 | 完成条件（门控） | 后端关键端点 |
|---|---|---|---|
| 1 上传 | 工作台 `/` | 存在 activeDataset + version | `POST /datasets/upload`、`/datasets/upload-batch`（≤10 文件） |
| 2 Schema 审阅 | 工作台 | `schema_reviewed_at` **或** `schema_auto_accepted_at` | `POST /dataset-versions/{id}/schema-review`、`PATCH .../schema` |
| 3 质量报告 | 工作台 | version 有 quality_report | `GET /dataset-versions/{id}/quality-report` |
| 4 分析运行 | 工作台 | 存在 succeeded 的 AnalysisRun | `POST /analysis-runs`、`POST /analysis-runs/validate-config` |
| 5 分析产物 | 工作台 | run 有 artifacts/result_summary | `GET /analysis-runs/{id}/artifacts` |
| 5.5 AI 报告 | 工作台 | （非门控，独立 confirm） | `POST /projects/{id}/auto-report/compute`（秒级确定性）→ `POST /auto-reports/{id}/narrate`（异步 job，**手动触发**）→ `POST /auto-reports/{id}/confirm`；旧 `POST /projects/{id}/auto-report` 保留为兼容串联 |
| 6 AI 采访 | stage6-interview | 存在 ≥1 条 answered 采访问题或手动补充 | `POST /projects/{id}/interview/next-question`（一次一问自适应，上限 10 问，可判 interview_complete，幂等去重）、`POST /projects/{id}/interview/complete`（三段式小结落 interview_summaries）、`GET/POST/PATCH /interview-questions` |
| 7 决策副驾 | stage7-copilot | 存在 status=confirmed 的洞察 | `POST /ai/distill-interview`（服务端自动落库 draft 洞察，幂等刷新）→ `PATCH /insights/{id}`（减法裁决：rejected/编辑/confirmed；**确认强制 evidence 非空**） |
| 8 产品问题 | stage8-problem | problem.status=confirmed | `POST /ai/frame-problem`（草稿，`used_insight_ids` 与已验证洞察求交集、空/幻觉回退全部入参 id）→ `POST /problems`（confirm 需 source_insight_ids） |
| 9 方案讨论 | stage9-solution | solution.status=selected | `POST /ai/propose-solutions`（2-4 个方案，恰好一个 recommended=true）→ `POST /solutions/{id}/select`（**落选方案必须写 reject_reason**） |
| 10 产品决策 | stage10-decision | decision.status=approved | `POST /ai/draft-decision`（纯草稿，无选定方案 422 `SOLUTION_NOT_SELECTED`）→ `POST /decision-proposals/{id}/submit`（仅置 pending_approval）→ **审批独立**：`POST /approval-requests/{id}/approve\|reject`（驳回必写理由；提案被编辑则返回 `VERSION_CONFLICT`） |
| 11 PRD | stage11-prd | 文档有版本 | `POST /documents`、`/documents/generate`、`/documents/{id}/versions`、`/documents/{id}/submit`、`GET /documents/{id}/export`（.md）；完成后 `POST /projects/{id}/archive` |

**上传后的自动管线**（`services/job_handlers.py:_handle_dataset_parse`，**【实测】**）：
解析 → 行列数/空表校验 → `extract_text_metrics` 派生数值列 → 质量评估（原帧）→ `_column_schema(frame_ext)` 写字段字典（带 `source` 标记）→ `schema_auto_accepted_at` 打点 + 内联跑 `_auto_analysis_plan`（**≤4 个**：EDA 恒在 + 事件表选留存 / 指标表选趋势 / 业务表选分组 / 数值列选异常；**`group_comparison` 为第 4 个候选；漏斗永不自动选**；幂等，失败不拖垮解析）→ `db.commit()` → 尾部可选跑一次 LLM 字段语义解读（`field_semantics.interpret_fields`，隔离模式，任何异常只写审计，绝不使解析失败）。

**数据集版本链**：重名重传追加不可变新版本（BUG-015 修复语义）。
**删除语义**：数据集与项目删除均要求显式确认（`?confirm={id}` 或 body）；项目删除 `_purge_project`（`routers/projects.py`）级联清理各资源并经 `_safe_data_file` 越界防护后物理删除文件，全程审计。

---

## 7. AI / LLM 逻辑（本项目的核心特色）

### 7.1 出站上下文防火墙（`app/ai_context.py`，1326 行，**【实测】**）
- `build_ai_context()` 是唯一合法出站构造器：白名单键为 `goal / metrics / artifacts / quality / schema / question / insights` 七个；`FORBIDDEN_CONTEXT_KEYS`（raw_rows/file_path/token/database_url…）、`_FEEDBACK_CONTENT_KEYS`（feedback/content/comment/…约 25 个变体键）、行级列表键（`_ROW_LIST_KEYS`：rows/records/samples…）一律剔除；Email/电话正则脱敏（ISO 日期先保护后还原，修正了「`2026-03-30` 被手机号正则误杀」的 P0）。
- **聚合列表白名单 `_AGGREGATE_LIST_KEYS`**（**实测 17 个键**：`bins/breakdown/categories/cohorts/counts/evidence/ids/labels/means/medians/metrics/pairs/periods/quantiles/rates/series/stages`）：只有这些键下的列表才会保留，其余列表（含嵌套）被丢弃。Phase 1 新增 `pairs` 仅承载变量对标量统计（p 值/稳健系数），不承载行级数据。行级禁止键 `_ROW_LIST_KEYS` **实测 12 个**（`cells/cell_values/data/observations/raw/raw_data/raw_rows/records/rows/sample_rows/samples/user_events`）。生产产物靠 `services/ai_stages.py:_AI_ARTIFACT_KEY_MAP`（`columns→metrics`、`stats→metrics`、`cohort_results→cohorts`、`group_results→categories`、`anomalies→evidence`、`trend_points→series`、`correlation_pairs_detail→pairs`）做适配后穿过。
- `assert_safe_ai_context()` 复检；`validate_ai_output()` 强制输出含 `facts/hypotheses/recommendations/limitations` 四节、每条 claim 必带 evidence 数组。
- 同文件定义全部阶段契约：`REPORT_OUTPUT_SCHEMA`/`validate_report_output`、`DOCUMENT_OUTLINE_SCHEMA`、`DOCUMENT_SECTION_SCHEMA`、`DOCUMENT_SECTIONS_SCHEMA`、`PROBLEM_DRAFT_SCHEMA`、`SOLUTION_DRAFTS_SCHEMA`、`DECISION_DRAFT_SCHEMA`、`NEXT_QUESTION_SCHEMA`、`SUMMARY_SCHEMA`、`FIELD_SEMANTICS_SCHEMA`。
- 反馈键清单单一来源在本文件：`services/ai_stages.py` 的请求侧消毒清单 = `_FEEDBACK_CONTENT_KEYS ∪ {sample, samples}`。

### 7.2 LLM 适配层（`app/infrastructure/llm/deepseek.py`，828 行，**【实测】**）
- `DeepSeekAdapter.complete()`：PII 脱敏所有消息（8000 字符截断）、JSON mode（`response_format: json_object`）、指数退避重试（429/5xx/网络错）、usage 统计。
- 工具白名单 `ALLOWED_TOOL_NAMES` **12 个**（8 个只读 L1：`get_project_context`/`get_dataset_schema`/`run_eda`/`run_trend_analysis`/`run_funnel_analysis`/`run_retention_analysis`/`run_anomaly_detection`/`get_feedback_summary` + 4 个需审批的占位：`create_insight_draft`/`create_decision_draft`/`create_document_draft`/`request_approval`）；`_DANGEROUS_ARGUMENTS`（sql/python/code/path/url/api_key…）在 plan 校验与执行**双端**拦截。
- `AnalysisPlan` pydantic 校验：steps ≤8、tool 必须在白名单、plan.project_id/dataset_id 不得越出请求范围、clarifying_question 与 steps 互斥。
- `CopilotOrchestrator`：两段 provider 调用（plan → execute 只读工具 → answer），返回 `AskClarification` 或 `Completed`。

### 7.3 请求级编排（`services/ai_stages.py`，787 行，**【实测】**）
- `_run_ai_stage()` 是**全部 AI 起草端点的共享模板**：feature flag 检查 → 日阀门 → provider 调用（含截断重试）→ 结构化校验 → AIRun 落账 → 审计。**未配 key 或 provider 故障一律降级为空草稿，绝不 500。** 可选参数自定义阶段契约：`response_schema`/`output_validator`/`empty_output`/`min_output_tokens`。
- **预算体系（spend-then-account）**：唯一 pre-call 检查是工作区**日阀门**（`ai_daily_token_budget`，默认 `100_000_000`，实际等同无上限）；worst_case = est_prompt(≤12000) + max_tokens，超限则调用前拒绝（零消耗，details 含 daily_remaining/needed_tokens/中文 hint）；**调用后只记账、结果绝不因预算丢弃**。输出上限由模块常量 `HARD_OUTPUT_CAP = 16384` 硬顶（普通调用=设置派生 max_output 默认 4096，文档 8192，重试翻倍至多 16384，**不可配置**）。`ai_per_request_token_budget` 已不再拦截任何调用（仅作 worst_case 兜底参数）。
- **截断感知与单次重试**：检查 `finish_reason == "length"` 或 JSON 解析失败 → 自动重试一次（max_tokens = min(desired×2, 16384)，system 追加「禁止截断」）；仍失败则 `failed` + `LLM_TRUNCATED`/`INVALID_AI_OUTPUT`；AIRun `input_summary_json.provider_meta` 记 `{finish_reason, retried}`。
- AI 端点清单：`/ai/interpret`（保留但 UI 不再用）、`/ai/frame-problem`、`/ai/propose-solutions`、`/ai/draft-decision`、`/ai/distill-interview`、`/ai/draft-document`、`/ai/cluster-feedback`、`/ai/usage`、`/dataset-versions/{id}/report-narration`、`/projects/{id}/auto-report/compute`、`/auto-reports/{id}/narrate`、`/projects/{id}/auto-report`、`/projects/{id}/interview/next-question`、`/projects/{id}/interview/complete`。
- Copilot SSE 是**回放**而非实时流：事件先存 `AIRun.input_summary_json.events`，`GET /copilot/runs/{id}/events` 逐条吐出。
- Copilot 上下文中的洞察由**服务端**注入（按 `session.project_id` 查 confirmed 洞察 ≤20 条），不信任前端传的 insight_ids。

### 7.4 自动报告：先算后叙两步链路（**【实测】**）
- `POST /projects/{id}/auto-report/compute`：仅确定性部分——`_latest_project_versions` + `_compute_report_aggregates_batch`（`asyncio.to_thread`，单文件失败隔离为 read_failure）+ `_deterministic_report_parts` + `build_findings_digest`，落库 `AutoAnalysisReport`（status=`not_configured`）并立即返回；**零 AI 调用、零 token、不建 AIRun/job**。重复调用始终新建报告并**删除该项目全部旧报告**（报告唯一化，第十五批）。
- `POST /auto-reports/{id}/narrate`：排队 `auto_report_narration` job（`asyncio.run(_narrate_report(...))`）；成功 → 确定性段在前 + AI 段在后重渲染 markdown、status=`succeeded`；任何失败 → status 保持 `not_configured`、仅记 `error_code`，确定性体不动。succeeded/confirmed 报告拒绝重叙述（409 `REPORT_ALREADY_NARRATED`），在途 job 拒绝重复（409 `NARRATION_IN_PROGRESS`，防双花）。
- **findings digest（`analytics/digest.py`）**：纯函数，从报告聚合按规则提炼 ≤12 条中文发现（kind=missing|correlation|pseudo_correlation_excluded|trend_shift|concentration|duplicate|outlier|constant|calendar_gap，阈值模块常量），severity 降序 + |value| 降序。Phase 1 会把已排除的派生列/机械相关数量作为低严重度可审计发现。compute 时写入 `deterministic_json["findings"]`，并落库为**真实 `finding` artifacts**（挂各数据集最新 succeeded run，幂等——重算先删旧行）；narrate 与文档生成经 artifacts 通道注入。
- **跨页可恢复**：`_auto_report_payload` 输出 `narration_job_id`/`narration_progress`，`_document_payload` 同构输出 `generation_job_id`/`generation_progress`；前端据此自动恢复轮询。

### 7.5 交付文档生成（三遍式，**【实测】**）
- **上下文装配**（`services/documents.py:_build_document_context`）：四类证据走 artifacts 通道——confirmed 洞察（≤20）、已回答采访问题（≤30）、approved 决策（≤10）、最新报告的每数据集聚合（≤5）；另输出顶层 `solution`（selected 方案）与 `decision`（approved 决策四字段）。`_evidence_manifest` 是不可变溯源块（**AI 输出永不覆盖 manifest**）。
- **时序**：路由只 find-or-create Document 壳并排队 job，不预写模板版本；job 收尾写入**第一个也是唯一一个**版本（AI 成功=succeeded，AI 失败=fallback+提示条，job 异常=无版本且 status=generation_failed）。
- **三遍生成**（`job_handlers._handle_document_generation`）：① 大纲瘦身（`_outline_context` 只给 goal/findings 摘要/决策方案轴/字段标签，不含全量聚合）；② 分节两波（prd 前两节顺序保叙事连贯 + 其余 `asyncio.gather` + `Semaphore(3)` 并行，注入 `outline_plan` 防重复；每节独立 `_run_ai_stage`、独立 AIRun、独立阀门）；③ 连贯校对（拼装正文 >6000 字触发，`_HARMONIZE_BATCH_SIZE=6` 分批，heading 集合与顺序一致才采纳该批）。**降级链完整**：任一遍失败 → 单次旧路径 → 模板 fallback（横幅可见）。进度权重：装配 5 + 大纲 10 + 分节 70 + 校对 15 + 收尾 5。

### 7.6 字段语义标签贯通（第二十一批，**【实测】**）
`services/field_semantics.py:interpret_fields` 在解析 job 尾部用一次 AI 调用为每列生成 `label`（≤10 字）+ `description`（≤50 字）与数据集 `dataset_label`，按列名精确匹配落库 `data_columns.semantic_label/semantic_description`；隔离模式与 `_run_auto_analyses` 相同（任何异常只写审计）；无「重新解读」入口。标签经持久化聚合自动进入报告叙述、采访地基、文档生成（`digest.display_name/column_display/dataset_display`）。

---

## 8. API 面貌（**【实测】** 137 条路由记录 / 14 个 router）

- **Auth**：register/login/refresh/me（GET+PATCH，PATCH 改昵称/改密码：密码对必须同现、验当前密码、短密码 400、审计 user.renamed/password_changed、**不吊销既有 token**——无吊销体系）
- **Workspace**：list/patch/settings(GET,PATCH)/members/metrics 字典 CRUD（含 `/api/v1/settings`、`/api/v1/metrics` 别名）
- **Audit**：`GET /audit-logs`（workspace 级，newest-first）
- **Projects**：CRUD + overview + workflow-status + tasks CRUD/links + archive/unarchive + `DELETE`（显式确认）
- **Datasets**：upload/upload-batch/versions/schema(PATCH)/schema-review/preview/quality-report/DELETE（owner + 显式确认；cleaning 三端点已删除）
- **Analysis**：validate-config、runs CRUD、rerun、artifacts
- **Feedback**：items CRUD/import/imports、clusters generate/patch/link-task、notes GET/POST/PATCH（V1.1）
- **Insights / Problems / Solutions / Decisions / Approvals / Documents**：按 §6 流程
- **Interview**：见 §6 阶段 6/7
- **AI + Copilot + Jobs**：见 §7

**V1.1 legacy 标记**：`_V11_LEGACY_API_PREFIXES`（workspaces/tasks/approval-requests/decision-proposals/jobs/copilot sessions/feedback-items/feedback-clusters）的响应带 `Deprecation: true`、`Sunset: 2027-01-01` 头。

**安全细节【实测】**：错误响应固定信封并对 validation details 递归脱敏（`_redact_validation_details`）；`_check_evidence_scope` 对 evidence 引用做存在性 + 跨 workspace/project 边界校验；登录失败统一文案。

---

## 9. 前端结构与数据流

- **认证流**：login 页 → `POST /auth/login` → `saveSession()`（localStorage + 镜像 cookie）→ middleware 放行；任意 401 统一 `clearSession()` + 跳 `/login`。**【沿用】**
- **工作台数据流**：`loadWorkflowSnapshot()`（`lib/workflow.ts`，490 行）先解析项目列表（`include_archived=true`）→ 解析出 activeProject（持久化键 `apw_active_project`，失效则回退第一个活跃项目）→ **无活跃项目时跳过全部九类业务列表请求**（门控回初始态）→ `Promise.allSettled` 并行拉 9 类列表 + `/me` → `hydrateActiveVersion` 补拉 schema/质量报告 → `stepCompletion()` 算 11 阶段门控。**【实测】**
- **工作台上传流**：选文件 → `upload-batch` → 轮询 job（`POLL_LIMIT=150`）→ `POST auto-report/compute` **秒级渲染数据概况** → 显示中性提示「数据概况已生成，请先查看数据，再点击『开始 AI 解读』」→ 用户点「开始 AI 解读」才走 `POST auto-reports/{id}/narrate` + 轮询（`JobProgress` 组件）。叙述失败按错误码分类显示中文原因 + 重试。**【实测】**
- **各阶段页**均为「门控包裹 + API 薄封装」模式（`WorkflowFrame` 的 `WorkflowHeader`/`WorkflowGate`）。
- **导航**：`lib/navigation.ts`（74 行）含 `pipelineNavItems`（`ai` 标记驱动 `isAiStage`）与 `legacyRouteAliases`（middleware 308 重定向，含 `/settings` → `/`）。**【实测】**

### 9.1 ⚠️ 未提交的前端视觉大改（**【新发现】**，详见 §13.2）

工作区中 `app/globals.css`（+168 行）、`app/(workspace)/page.tsx`（±48 行）、`lib/chartOption.ts`（±83 行）有未提交改动，是**一次视觉大改**，与文档记录的 batch 22/27 设计体系**冲突**：

| 维度 | 文档记录的 batch 22/27 规范 | 未提交改动实际状态 |
|---|---|---|
| 品牌色 | `--brand: #2563eb`（单一品牌蓝） | `--brand: #6366f1`（indigo） |
| 渐变/光晕 | 「**线优于影**」、精密仪器、无渐变 | 新增 `--brand-gradient: linear-gradient(135deg,#6366f1,#8b5cf6)`、`--brand-glow` + 径向光晕、卡片/侧边栏渐变 |
| 图表品牌色 | 图表取品牌令牌 | `chartOption.ts` 硬编码 `#3b82f6`，**与 `--brand: #6366f1` 不一致**（真实缺陷） |
| 动效契约 | 统一 400ms cubicOut | 改为 500ms |
| 装饰克制 | 「装饰性 Sparkles 已全站移除」 | 页面标题加 emoji（`📊`，page.tsx 共含 `📊🎤📈🔒`）；globals.css 注释改为英文 + emoji（`🎨🌟💫🌊🔤`） |
| 注释语言 | 中文 | globals.css 新增段为英文 |

---

## 10. 当前完成度（**【实测】** 2026-09-13）

**已实现且验证**：
- 后端测试套件 **374 passed, 1 xfailed**（375 收集，**78.74s**，实测运行；= 计算层修复批的 375 − 删除的 9 个 `test_enhanced_engine` 用例 + 本批新增 6）。速度从 ~210s 降到 ~78s，来自测试库 `journal_mode=MEMORY` + `synchronous=OFF`。覆盖：RBAC 与 workspace 隔离、数据管线（上传/版本/质量）、分析引擎全类型、增强相关性/类型感知/离群摘要与 AI 防火墙契约、**计算层 4 项正确性缺陷（血缘方向 / 离群单一口径 / 类型合规 / 中文长句 identifier 误判）**、**双维度质量与排除明细可审计**、**测试运行时位置与日志模式守护**、AI 降级边界（无 key 绝不 500、输出契约、上下文白名单、反馈原文不外泄）、决策链规则、项目级联删除、报告叙述消毒、采访/蒸馏、文档并行生成、字段语义、用户资料。
- 17 个 Alembic 迁移可从零建库；`.env` 已配置 DeepSeek；前后端均可本地跑通。
- 前端 11 阶段页面、工作台、数据管理、历史回看齐全（**设置页已随第二十六批删除**）。
- **全链路已真实手动冒烟走通**（11 阶段版 2026-09-01）。
- **`ruff check app tests` 零告警**（Phase 1 修改后复测仍为零）。
- **前端 `typecheck` 与 `lint` 通过**；保留用户既有视觉改动，并将 `chartOption.ts` 中品牌硬编码色统一为 `--brand: #6366f1` 对应的 indigo 体系。

**进行中（V1.1 迁移收尾）**：
- legacy 表/API 与新表/API 并存，代码中大量兼容分支（`_migrate_legacy_metric_dictionary`、feedback 双轨等）；`0005` 迁移默认不删 legacy 表。
- Copilot 的 `create_insight_draft` 等 4 个写工具在白名单中但**尚无服务端 handler**（`ReadOnlyToolRegistry.execute` 会拒绝）。

**缺失项**：
- git 仓库已初始化并按批提交（conventional commits，未配置远端、未 push）；**有未提交工作区改动**（§13）。
- 前端零测试（无测试框架）；无 CI 流水线；无国际化层（界面中文硬编码）。

---

## 11. 已知问题与技术债（按影响排序）

**⚠️ 本次新发现（2026-09-13，来自未提交工作，详见 §13）**：

> 下列 1–3 项已由 **Phase 0 工程收口**（`docs/dev-prompts/phase0-cleanup.md`，2026-09-13 执行）修复；原先的防火墙/未接入问题已由 **Phase 1 集成**处理。其余长期项仍然存在。

1. ~~**【新发现】依赖未声明会炸部署**~~ **已修复**：`scipy>=1.11,<2` 已进 `[project.dependencies]` + `requirements.txt`；`scikit-learn>=1.4,<2` 进 `[project.optional-dependencies] ml`，`outliers.py` 改为函数内可选导入并降级空结果。
2. ~~**【新发现】ruff 基线被破坏**~~ **已修复**：`ruff check app tests` 回到**零告警**，无 `noqa`/`per-file-ignores`。
3. ~~**【新发现】新测试实际不运行**~~ **已修复**：文件移入 `apps/api/tests/test_enhanced_engine.py`，并新增 `tests/test_enhanced_integration.py`。**2026-09-13 该测试文件已随 `enhanced_engine.py` 一并删除**；当前全量 375 收集。
4. ~~**【新发现】增强分析引擎未集成**：原本与生产 `AnalysisEngine` 并行。~~ **Phase 1 已集成**：生产 `AnalysisEngine.run_eda` 统一走增强相关性实现；三个新类型（`correlation_analysis` / `type_profile` / `outlier_objects`）接入 `_analysis_artifacts` 与配置校验，按决策 3 仅手动可选，不进入 `_auto_analysis_plan`。
5. ~~**【新发现】新引擎输出契约与防火墙冲突**：相关对/类型统计/离群对象原形状会被静默剥离，且行级离群信息触及安全边界。~~ **Phase 1 已修复**：`pairs` 加入 `_AGGREGATE_LIST_KEYS`（17 键）；相关详情映射为 `correlation_pairs_detail→pairs`；类型/离群产物统一为 `metrics` 列表；离群出站摘要不含 `row_index`/行引用；`_ROW_LIST_KEYS` 与反馈黑名单未放宽。
6. **【新发现】前端视觉大改与既有设计体系冲突**：用户决定保留；Phase 1 已把 `chartOption.ts` 的硬编码 `#3b82f6` 统一到 `--brand: #6366f1` 的 indigo 体系，并新增 `correlation_heatmap` / `count_bar` 图表分支。

**计算层正确性缺陷（2026-09-13 只读脚本实测复现 → 同批修复，未提交，见 §13.4）**：

1. ~~**血缘把源列误判为派生列**：`dag._match_builtin_rules` 的 `target_lower in rule.source_columns` 分支会把规则里的**源列本身**判成派生列 → `should_exclude_from_correlation` 排除错误的列对、方向说反，真正的派生列反而漏判。~~ **已修复**：只认 `rule.name`，并要求源列存在且不等于目标列。实测：`should_exclude(周活跃用户, 总用户数)` 由 `True`（理由反向）变为 `False`；`should_exclude(周活跃用户, 渗透率(% 占周活))` 由 `False`（漏判）变为 `True` 且方向正确。
2. ~~**离群值三套口径**：`quality.assess_quality`（并集掩码）vs `outliers.build_outlier_aggregates`（IQR 列表 + Z 列表相加 → 重复计数、rate 可 >1、series 出重复值）vs `auto_report`/`digest`（仅 IQR）——同一概念三个数。~~ **已修复**：新增唯一实现 `outliers.compute_column_outliers`（并集掩码按行去重、`rate = count / 非空样本数`、极值样本按值去重有界），quality / raw map / 出站聚合 / auto_report 四处全部改由它驱动；digest 文案同步为「离群值」。实测同一列：旧 count `2` → 新 `1`，`series` 由重复两条变为单条。
3. ~~**质量分类型维度恒为 0**：生产 `assess_quality(df)` 从不传 `expected_types` → `type_error_count` 恒 0 → `0.4*missing+0.25*dup+0.2*type+0.15*outlier` 的惩罚上限只有 80，分数系统性虚高。~~ **已修复**：未显式传参时用 `infer_column_type_v2` 自动派生（仅 numeric/datetime/boolean；category/text/identifier 跳过），显式传入仍以传入为准。实测「9 个数字 + 1 个中文字符串」的列：旧分 `100.0` → 新分 `98.0`，`type_error_count` `0` → `1`。
4. ~~**无空格中文长句被误判为 identifier**：`parsing.infer_column_type_v2` 只检查「近唯一 + 无空格」，于是中文长句判 identifier → `_is_textlike` 为假 → `extract_text_metrics` 不扫描，句内指标全部抽不出来。~~ **已修复**：identifier 需同时满足无空白、长度 ≤ 40、无句读标点（`_looks_like_identifier`）；长句回落 `text` 并被扫描。实测 45 字中文列由 `identifier` 变 `text`，`备注__本周DAU110k留存率` 成功抽出（coverage 1.0）。

> ~~**仍未修（同批实测，本次未纳入范围）**：`dual_quality.analysis_quality` 在生产路径恒为 0；`analytics/enhanced_engine.py` 仍是被测试引用的死代码；`outliers.detect_outliers_lof` 无调用点且 `pred[int(idx)]` 索引错位。~~ **三项已全部处理（2026-09-13 第二批，见 §13.4）**：`analysis_quality` 由解析 job 用真实产物回写；`enhanced_engine.py` + `test_enhanced_engine.py` 已删除；`detect_outliers_lof` 已删除（连带 `scikit-learn` 失去唯一消费方，`[ml]` extra 暂保留）。

**长期项（**【沿用】** 自 2026-09-06，本次未逐条复核）**：

7. **V1.1 迁移未收尾**：双套模型/API 并存（feedback_items+clusters vs feedback_notes；tasks/approvals 待淘汰），每个新功能都要处理新旧两轨。
8. **两套并行 schema 机制**：Alembic 之外 `db.py:_repair_missing_columns()` 启动补列（无外键），dev 便利但与迁移漂移风险。
9. **JobExecutor 进程内限制**：单进程假设（多 worker 会重复执行/丢任务）；无自动重试退避（仅手动 `POST /jobs/{id}/retry`，且需 `_retryable` 标记）；长任务占 BackgroundTasks 线程。
10. **AI 预算并发窗口**：`with_for_update` 在 SQLite 是 no-op；running 行按各自 worst_case 预留后，daily 最多被在途调用突破一个 worst_case（已接受的设计）；`_workspace_token_usage` 每次全扫当日 AIRun，量大后变慢；`copilot_message` 的独立预检仍是旧 `reserved > daily` 形式，未统一到 worst_case 投影。
11. **代码卫生残余**：`openai` SDK 不在依赖（实际依赖 httpx fallback）；CORS 未配置时回退 `["*"]` 且 `allow_credentials=True`。
12. **前端认证是软门禁**：middleware 只查 `apw_session=1` cookie 存在性（可伪造绕过页面守卫），真实鉴权仅在 API 层——**设计上可接受但需明确这不是安全边界**。JWT 在 localStorage（常规 XSS 暴露面），无服务端吊销。
13. **SSE 非实时**：copilot 事件回放式；`DeepSeekAdapter.stream()` 是伪流（一次性 complete 后整体 yield）。
14. **文件存储在本机磁盘**：`data/uploads|processed|exports`，无对象存储；`_purge_project` 物理删除不可恢复（有审计）。删除前经 `_safe_data_file` 做 DATA_ROOT 越界防护。
15. **测试基建小脆弱点**：`tests/conftest.py` 必须在 import app 前设置环境变量（ruff 按文件豁免 E402）。~~测试库文件在 `output/test-runtime`（每次 rebuild）。~~ **2026-09-13 已迁移**：测试运行时（数据库 + 上传产物）改放 `%TEMP%\apw-test-runtime`，并对测试库设 `PRAGMA journal_mode=MEMORY` / `synchronous=OFF` —— 本机上**除 `%TEMP%` 以外的任何目录**删除文件都会被系统级代理转成回收站条目（实测 `AppData\Local`、`AppData\Roaming`、用户主目录、OneDrive 目录均 `+1`，仅 `%TEMP%` 为 `+0`），原先每次跑测试会向回收站写入约 **1 万个** `api-test.db-journal`。两条不变量由 `tests/test_test_environment.py` 锁定；`APW_TEST_ROOT` 可覆盖位置。
16. **真实 key 下 AI 输出可靠性残余**：`deepseek-v4-flash` 仍偶发返回非 JSON 或 provider 错误（降级路径行为正确）；Copilot 编排 plan 校验（`INVALID_ANALYSIS_PLAN`）真实 key 下偶发失败降级，未修。
17. **项目删除与解析任务竞态可泄漏上传文件（未修）**：上传后 <1s 删除项目、后台 `dataset_parse` job 未完成时，`_purge_project` 返回 `files: 0` 且上传文件遗留磁盘（版本行被级联删除，泄漏仅限磁盘文件）。
18. **文档生成上下文丢失洞察正文（未修）**：`_build_document_context` 的洞察 payload 用 `content` 键，而 `content` 在 `_FEEDBACK_CONTENT_KEYS` 黑名单内——装配时洞察正文被静默剥离，文档 AI 实际只能看到标题/置信度/证据骨架。修复方向：改用非保留键（如 `body`）或开专用通道。
19. **`ai_per_request_token_budget` 已无拦截职责**却仍在设置契约中，易误导。
20. **并行分节与日预算阀门的交互**：wave2 多个并行节各自 worst_case（≈2 万 tokens/节）叠加可能超出当日剩余预算——被拒节零消耗、降级为大纲要点拼接。默认日预算已上调至 100M，正常用量下不再触发；未做预算感知的波内调度。
21. **历史事故记录（已修复，留档）**：第三批 AST 切割脚本曾丢弃 `_purge_project` 中的 `_safe_data_file` 调用（提交 42728e5..132a25e 期间删除路径失去越界防护），第四批重新接线并由 `test_guardrails` 锁定；第二十一批 `stage6` 页面曾残留函数外孤儿代码块导致前端三检阻塞（已删）。

---

## 12. 给后续开发对话的关键事实速查

**落位规则（第三批拆分后）**：
- 新增 API 端点：写到对应域的 `app/routers/<域>.py`（`router = APIRouter()` + `@router.<method>("/api/v1/...")` 路径全写），在 `main.py` 加 `app.include_router(...)`；请求模型进 `schemas.py`。`tests/test_route_manifest.py` 冻结断言全部 (path, methods, name)——路由变更必须同步重生成该清单。
- 新增业务逻辑：放到 `app/services/<域>.py`；被多个 router 共用的 helper 必须下沉 services（**routers 之间禁止互导，services 禁止反向导入 routers**）。`ok()/error()/model_dict/paged` 在 `common.py`；`_require_pandas()` 是 pandas 懒加载哨兵（使用方在函数内 `pd = _require_pandas()`，**运行时禁止模块顶层 import pandas**）。
- job handler：`app/services/job_handlers.py`，`job_executor` 全仓库唯一实例在此；新增 handler 后在 `_register_job_handlers()` 注册（`main.py` 末尾恰好调用一次）。
- **新增 AI 能力**：服务逻辑进 `services/ai_stages.py`（复用 `_run_ai_stage()` 模板，可传 `response_schema`/`output_validator`/`empty_output`/`min_output_tokens` 定义阶段契约），路由壳进 `routers/ai.py`；上下文必须过 `build_ai_context`，AI 结果一律 draft；Copilot 的 insights 上下文由服务端注入，客户端传入的一律丢弃。**例外**：字段语义解读是 job 内可选增强（`services/field_semantics.py`，无路由），结果按列名落库而非 draft。
- **分析类型扩展点**：`analytics/engine.py`（计算）+ `services/analysis_pipeline.py`（`_analysis_artifacts` 持久化映射、`_analysis_config_validation`、`SUPPORTED_ANALYSIS_TYPES`、`_auto_analysis_plan`）+ `infrastructure/llm/deepseek.py` 的 `ALLOWED_TOOL_NAMES`（若暴露给 Copilot）+ 前端 `lib/chartOption.ts`（新图表渲染）。
- **离群值只有一个口径**：任何离群值的计数 / 比率必须走 `analytics/outliers.compute_column_outliers`（返回 `ColumnOutlierStats`），**不得**再自己写 IQR/Z 掩码或把两个方法的列表相加；进程内行级用途走 `build_raw_outliers_map`，出站只走 `build_outlier_aggregates`（有界标量、不含行号）。默认阈值取自 `OUTLIER_IQR_MULTIPLIER` / `OUTLIER_Z_THRESHOLD`，z 值统一 `ddof=0`。
- **列类型判据联动**：`parsing.infer_column_type_v2` 的 `identifier` 判据被 `text_metrics._is_textlike` 直接消费（只有 `text` / `category` 会被文本指标抽取扫描）。改动 identifier 判据前先确认这条链路：放宽会把自由文本判成 identifier 而**静默丢掉**列内指标。
- **测试运行时只能放在 `%TEMP%` 之下**（`tests/conftest.py:_resolve_test_root`，可被 `APW_TEST_ROOT` 覆盖）：本机在 `%TEMP%` 以外删除任何文件都会被系统级代理转成回收站条目（见 §11.15），且测试库必须保持 `journal_mode=MEMORY` + `synchronous=OFF`（由 `tests/test_test_environment.py` 锁定）。给测试新增落盘文件时也要放进这个目录，不要写进仓库树。
- **AI 上下文防火墙**：新产物若含列表，必须放在 `_AGGREGATE_LIST_KEYS` 允许键下，或先在 `services/ai_stages.py` 写适配映射；**行级数据禁止出站**（`_ROW_LIST_KEYS`）；产物形状优先 `[{name, ...}]` 列表而非「以列名为键的 dict」（避免撞 `_FEEDBACK_CONTENT_KEYS`）。
- **前端新页面惯例**：`app/(workspace)/` 下建目录，用 `WorkflowFrame` 的 `WorkflowHeader/WorkflowGate` 包裹，门控逻辑改 `lib/workflow.ts` 的 `stepCompletion()`，导航加 `lib/navigation.ts`。
- **归档语义**：归档只能走 `POST /projects/{id}/archive|unarchive`（`ProjectPatch` 不含 status）；归档项目的 editor+ 写路径全部 409 `PROJECT_ARCHIVED`；前端「当前项目」持久化键 `apw_active_project`。
- **数据页语义**：清洗全链路已删除；`GET /datasets` 的 versions 携带水合 `quality_report`；数据集详情页字段定义为只读（后端 PATCH schema 端点保留）。
- **测试运行**：`cd apps/api && .venv/Scripts/python.exe -m pytest tests -q`（Windows；测试自备隔离 SQLite 与空 DeepSeek key）。
- **Lint**：`cd apps/api && .venv/Scripts/python.exe -m ruff check app tests`（基线：零告警，`line-length=120`）；前端 `npm run typecheck` + `npm run lint`。

### 12.1 开发交付协议（用户强制要求）

**触发时机**：每次完成开发任务之后（含「无代码改动 / 只写文档」的任务）。

**第一步——核对本文件**。若本次开发使下列任一项发生变化，则**就地增量更新**本文件对应章节，**只改真正变化的条目，不重写整份文件**：

| 变化类型 | 对应章节 |
|---|---|
| 项目架构 | §2 |
| 核心模块 / 目录结构 | §3、§3.1 |
| API（新增/删除/改签名） | §8；若路由数变化须同步重新生成 `tests/test_route_manifest.py` 冻结清单 |
| 数据库（模型/迁移） | §5（新增迁移同步 §2 的迁移计数） |
| AI / Agent / RAG | §7 |
| 业务流程（11 阶段门控） | §6 |
| 功能完成度 | §10 |
| 已知问题 / 技术债 | §11 |
| 开发约束 / 落位规则 | §12 |
| 工作区未提交状态 | §13（含 §13.3 可验证指标：ruff / pytest / 路由数 / 迁移数） |

**第二步——向用户回报固定四项**（缺一不可，按序回答）：

1. 本次修改了什么
2. 是否影响项目架构
3. 是否需要更新 PROJECT_CONTEXT.md
4. PROJECT_CONTEXT.md 更新了哪些内容

---

## 13. 提交状态与工作区清单（**【实测】** 2026-09-13 整理后）

> 2026-09-13 执行仓库整理：原 §13.1 / §13.2 记录的全部内容已按语义分组提交（共 **8 条**，自 `22657c2` 起）；
> `.workbuddy/`、`.workbuddy-ai/` 已加入 `.gitignore`（目录仍保留在磁盘）。
> **其后又产生一批未提交改动（计算层修复批，见 §13.4），因此当前工作区**不是**干净的。**

### 13.1 本批提交（8 条）

| # | 提交 | 覆盖内容 |
|---|---|---|
| 1 | `chore(gitignore)` | 忽略 `.workbuddy/`、`.workbuddy-ai/`（AI 工作区数据，仅忽略不删除；既有规则未动） |
| 2 | `chore(deps)` | `scipy>=1.11,<2` 进必需依赖；新增 `[project.optional-dependencies] ml`（scikit-learn） |
| 3 | `feat(analytics)` | 新增 `corelation.py` / `dag.py` / `outliers.py` / `types.py` / `enhanced_engine.py` |
| 4 | `feat(analytics)` | `engine.py` EDA 接入增强相关性（保留 `correlations` 契约 + `correlation_pairs_detail`）；`quality.py` 双维度质量；`digest.py` 伪相关审计发现 |
| 5 | `feat(api)` | `ai_context.py` 新增 `pairs` 键；`services/` 四个模块接线（ai_stages/analysis_pipeline/auto_report/datasets）；`test_report_narration.py` 产物覆盖断言随 EDA 产物合并由 3→2 更新 |
| 6 | `test(api)` | `test_enhanced_engine.py`（9 用例）+ `test_enhanced_integration.py`（5 用例）+ `fixtures/samples/weekly_report.csv` |
| 7 | `style(web)` | `globals.css` / `page.tsx` / `chartOption.ts`：indigo 品牌令牌 + 渐变光晕 + `correlation_heatmap` / `count_bar` 图表分支 |
| 8 | `docs` | `README.md` 对齐修正、本文件、`docs/dev-prompts/` 两份 Prompt 归档 |

### 13.2 有意排除（未提交，理由）

| 路径 | 原因 |
|---|---|
| `.workbuddy/`（含 `memory/`） | AI 工作区数据与记忆，`.gitignore` 覆盖；**目录保留在磁盘，未删除** |
| `.workbuddy-ai/` | 同上（当前不存在，规则为预防性添加） |
| `.env` / `apps/api/data/uploads\|processed\|exports/*` / `output/` / `*.sql` | 既有 `.gitignore` 已覆盖（密钥 / 上传数据 / 测试产物），本次未改动这些规则 |
| `apps/web/node_modules/`、`apps/api/.venv/` | 既有 `.gitignore` 已覆盖 |

> 提交前已扫描待提交文件：无密钥、无私钥、无真实凭据。发现 `docs/dev-prompts/phase0-cleanup.md` 中一处本地绝对路径含操作系统用户名，已改写为 `<本地用户目录>` 占位后再提交。

### 13.3 可验证指标（**【实测】** 2026-09-13）

| 指标 | 整理批提交前 | 计算层修复批 | 计算层第二/三批（当前） |
|---|---|---|---|
| `ruff check app tests` | **0 错** | **0 错** | **0 错** |
| `pytest tests -q` | **366 passed, 1 xfailed**（236.79s） | **375 passed, 1 xfailed**（210.02s） | **374 passed, 1 xfailed**（**78.74s**，提速约 2.7 倍） |
| `pytest --collect-only -q` | 367 collected | 376 collected | **375 collected** |
| 首次复跑异常 | `test_report_narration.py` 1 failed（`seen >= 3` 期望过期）→ 修正为 `>= 2` | 无失败 | 无失败 |
| 回收站增量（跑一轮全量） | **约 1 万条** `api-test.db-journal` | 未测（测试运行时仍在仓库树内） | **0 条**（实测 155 → 155） |
| `git status --porcelain -uall` | 空（除被忽略项） | 7 改 1 新 | **18 改 3 新 2 删（§13.4，2026-09-14 复核更正）** |
| 路由数 | 137（14 router） | 137（未改动） | 137（**未改动**） |
| Alembic 迁移 | 17（0001..0017） | 17（未改动） | 17（**未改动**） |

### 13.4 计算层修复批 + 测试环境批（**已修改未提交**，2026-09-13 第二/三批）

第一批修复 §11「计算层正确性缺陷」的 4 项（血缘方向 / 离群单一口径 / 类型合规 / identifier 误判）并新增 9 个针对性用例；
第二批解决「每跑一次测试回收站堆积约 1 万个文件」（见 §11.15），并新增 2 个守护用例；
第三批落地双维度质量与可审计排除明细，并清理计算层死代码（本任务，新增 6 个用例）。

| 文件 | 规模（`git diff`） | 性质 |
|---|---|---|
| `apps/api/app/analytics/outliers.py` | +167 / -28 | **新增唯一口径** `ColumnOutlierStats` + `compute_column_outliers`（并集掩码按行去重、`rate=count/非空样本数`、极值样本按值去重有界、z 用 `ddof=0`）；`build_raw_outliers_map` / `build_outlier_aggregates` 改由它驱动 |
| `apps/api/app/analytics/dag.py` | +36 / -17 | `_match_builtin_rules` 只认 `rule.name` 且源列须存在、不等于目标列；`should_exclude_from_correlation` 主语固定为派生列；补上策略 2 缺失的 `derivation_description` |
| `apps/api/app/analytics/quality.py` | +42 / -27 | outlier 段改用统一函数（默认阈值取自 `OUTLIER_*` 常量）；新增 `_derived_expected_type`，未显式传 `expected_types` 时按列自动派生（仅 numeric/datetime/boolean） |
| `apps/api/app/analytics/parsing.py` | +37 / -5 | 新增 `IDENTIFIER_MAX_LENGTH=40` / `IDENTIFIER_SENTENCE_PUNCTUATION` + `_looks_like_identifier`，identifier 判据收紧 |
| `apps/api/app/services/auto_report.py` | +8 / -6 | `entry["outliers"]` 改用同一函数（原为 IQR-only） |
| `apps/api/app/analytics/digest.py` | +9 / -2 | outlier 文案「IQR 离群值」→「离群值」，与新口径一致 |
| `apps/api/tests/test_field_semantics.py` | +1 / -1 | 随上条同步断言文案（**因预期口径变化而更新的断言**） |
| `apps/api/tests/test_analytics_correctness.py` | 新增（9 用例） | 4 项缺陷各有用例锁定 + EDA 出站无 `row_index` 的防火墙回归 |
| `apps/api/app/services/analysis_pipeline.py` | +55 / -6 | **第三批**：`_run_auto_analyses` 返回值新增 `artifacts`；新增 `_analysis_quality_payload` / `_refresh_dual_quality`（解析 job 收尾回写真实分析质量）；`_correlation_analysis_artifact` 补 `excluded_correlation_pairs_detail` |
| `apps/api/app/services/job_handlers.py` | +4 / 0 | **第三批**：`_handle_dataset_parse` 在 `_run_auto_analyses` 之后、commit 之前调用 `_refresh_dual_quality` |
| `apps/api/app/analytics/text_metrics.py` | +26 / -1 | **第三批**：抽取列撞名时追加确定性后缀 `__dupN` 并写 `renamed_from`；report 记录同一 normalized 指标的 `display_variants` |
| `apps/api/app/analytics/types.py` | +6 / -3 | **第三批**：`OrdinalStatistics.to_dict` 与 mode 判定改用 `is not None`，0 值不再被当缺失 |
| `apps/api/app/analytics/corelation.py` | +16 / -6 | **第三批**：血缘排除由计数改为 `{'var1','var2','reason'}` 明细列表；新增 `EXCLUDED_PAIRS_DETAIL_LIMIT=20` |
| `apps/api/app/analytics/engine.py` | +13 / -5 | **第三批**：EDA payload 新增 `excluded_correlation_pairs_detail`（有界，人工审计用、不出站） |
| `apps/api/app/analytics/enhanced_engine.py`、`apps/api/tests/test_enhanced_engine.py` | **删除** | **第三批死代码清理**：全仓库已无 `enhanced_engine` / `run_enhanced_analysis` / `EnhancedAnalysisEngine` 引用 |
| `apps/api/app/analytics/outliers.py`（lof 段） | **删除** | **第三批**：`detect_outliers_lof` 无调用点且 `pred[int(idx)]` 索引错位；**连带 `scikit-learn` 失去唯一消费方** |
| `apps/api/tests/test_test_environment.py` | 新增（2 用例） | **测试环境批**：守护两条不变量——运行时必须位于系统临时目录下、测试库 `journal_mode` 必须是 `memory` |
| `apps/api/tests/conftest.py` | +54 / -4 | **测试环境批**：`_resolve_test_root()` 把运行时迁到 `%TEMP%\apw-test-runtime`（`APW_TEST_ROOT` 可覆盖），并对测试库执行 `PRAGMA journal_mode=MEMORY` / `synchronous=OFF` |
| `.gitignore` | +3 / -1 | `output/` 条目注释更新（测试运行时已迁至系统临时目录，该条目只覆盖历史残留） |

**本批实测**（**2026-09-14 复核更正**：原文误写 377 passed/378 收集/76.81s，与 §10、§13.3 及独立复验不符）：`ruff check app tests` 0 错；`pytest tests -q` → **374 passed, 1 xfailed（375 收集）**，耗时随机器波动、以最近一次实跑为准（约 78–105s；基线 366/1 → +9 计算层用例 + 2 环境守护用例，**零回归**）。**回收站增量实测为 0**（改造前每轮约 +1 万条）。

**被影响的行为（预期，已实测量化）**：
- 质量分：类型维度真正参与惩罚，「多数可解析 + 少量脏值」的列会被扣分（实测 100.0 → 98.0）；`overall_score`/`status` 的**判定口径未变**（仍是 `>=95 passed / >=80 needs_review / else failed`），但它们只是前端徽标与 `quality_score` 展示，**不构成任何硬门控**（阶段 3 门控只要求"存在质量报告"）。
- 离群值：同一列 count 由"重复计数"回落为按行去重（实测 2 → 1），`rate` 分母由总行数改为非空样本数，出站 `series` 不再出现重复值。
- 相关性：不再错误排除源列对，且真正派生列的对被正确排除（实测排除对数与 `excluded_correlation_pairs` 在样例数据上未变，已有断言仍然成立）。
- digest：outlier 条目文案变化，数值随新口径变化。
- 文本指标：无空格中文长句列重新被扫描，可能新增 `{源列}__{指标}` 派生列。

### 13.5 Phase 1 已确认并落地的设计决策

1. **行级数据不出站（方案 A）**：离群值仍可在进程内用 `row_index` 计算稳健相关，但 AI/报告出站只保留每列有界摘要（`name/method/count/rate/min_value/max_value/series`），不含行号或行引用。
2. **新增 `pairs` 键**：`_AGGREGATE_LIST_KEYS` 从 16→17；仅允许变量对及其标量统计（p 值、稳健系数等），配套 `test_enhanced_integration.py` 守护。
3. **新分析类型仅手动可选**：`correlation_analysis` / `type_profile` / `outlier_objects` 接入 `SUPPORTED_ANALYSIS_TYPES` 与 `_analysis_artifacts`，`_auto_analysis_plan` 和 `_AUTO_ANALYSIS_LIMIT=4` 零改动。
4. **保留前端视觉大改并统一令牌**：`chartOption.ts` 复用 indigo 品牌色（`#6366f1`）并新增 `correlation_heatmap` / `count_bar` 分支；`typecheck` 与 `lint` 通过。

### 13.6 开发 Prompt 归档（已随 commit 8 入库）

见 `docs/dev-prompts/phase0-cleanup.md`（收口）与 `docs/dev-prompts/phase1-integration.md`（完整集成）；两份文档已提交，不再属于未跟踪文件。

- **Phase 0（收口）已执行完毕**（2026-09-13）：依赖声明 / ruff 归零 / 测试归位 / 临时文件清理四项完成。
- **Phase 1（增强分析引擎集成）已执行完毕**（2026-09-13）：EDA 统一接入增强相关性；新增 3 个手动分析类型；双维度质量附加落库；防火墙 `pairs` 与有界离群摘要接线；digest/报告/前端图表同步；5 个新增集成测试全绿。上述四项设计决策已按 §13.5 落地。

### 13.7 历史提交基线

`22657c2`（batch 27 complete — motion and data-typography polish）是本批 8 条提交之前的最后一个提交；本项目共 100 条提交到达该基线。
