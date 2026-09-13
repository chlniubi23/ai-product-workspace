# AI Product Workspace

**「AI 辅助、人工主导」的产品数据分析与决策工作台**：上传数据后，全部统计由 pandas 确定性计算完成，DeepSeek LLM 只做整合解读——自适应采访、洞察蒸馏（自动落库草稿 + 减法裁决）、三遍式深度 PRD 生成，证据链全程可追溯。

| 应用 | 技术 | 部署平台 |
|---|---|---|
| `apps/web` | Next.js 14（App Router）+ Tailwind + ECharts | **Vercel** |
| `apps/api` | FastAPI + SQLAlchemy 2 + Alembic + pandas | **Railway**（配 MySQL） |

### 功能亮点

- **11 阶段流水线**：从上传→自动报告→AI 采访→洞察蒸馏→问题/方案/决策→PRD，每一步状态可视、可追溯。
- **确定性计算铁律**：所有统计数字由 pandas 引擎算出，LLM 永不自己算数；AI 仅负责叙述与起草。
- **真实工具调用**：Copilot 支持 8 个只读分析工具（EDA/趋势/留存/漏斗/异常等），严格白名单校验，危险参数拦截。
- **Agent 编排**：理解→规划→验证→执行→总结的闭环，澄清问题机制 + 预算阀门 + 截断重试。
- **结构化证据接地**：非向量 RAG，每次 AI 调用前检索项目报告聚合/分析产物/采访问答，强制 AI 输出引用真实资源 id。
- **工程化兜底**：无 API key 时优雅降级（零 500）、AI 失败模板回退、跨页恢复任务轮询、审计日志全链路记录。

### AI 能力边界

```
┌─────────────────────────────────────────────────────────────┐
│  普通代码（pandas 引擎）        │  AI（DeepSeek）           │
│  ✅ EDA / 趋势 / 留存 / 漏斗    │  ❌ 不产任何统计数字      │
│  ✅ 质量评分 / findings digest  │  ✅ 叙述报告 / 采访提问   │
│  ✅ 分组对比 / 异常检测         │  ✅ 起草洞察/问题/方案    │
│  ✅ 所有数值计算                │  ✅ 起草决策 / PRD        │
│                                 │  ⚠️ 全部产出为 draft      │
│                                 │  ⚠️ 必须引用真实资源 id   │
└─────────────────────────────────────────────────────────────┘
```

**核心设计铁律**（代码里被反复强制执行）：
- 数据侧：所有统计数字由确定性代码计算，LLM 永远不自己算数。
- AI 侧：AI 只产草稿，采纳/确认永远由用户完成；每条结论必须引用真实资源 id。
- 防火墙：所有出站 AI 上下文经白名单过滤（7 个允许键）、PII 脱敏、反馈原文不外泄。
- 多租户隔离：一切资源挂 workspace，所有读取路径做成员校验 + workspace 边界检查。

### 快速开始

**没有 MySQL 时最省事的方式**：完全不建 `.env`（后端默认走 SQLite）；或设 `ALLOW_SQLITE_FALLBACK=true`。

```powershell
# 后端（Python ≥ 3.11）
cd apps/api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
# 不设 .env 即走 SQLite；要生产级请设为 .env 填入 DEEPSEEK_API_KEY
uvicorn app.main:app --reload --port 8000

# 前端（Node ≥ 18）
cd apps/web
npm install
npm run dev
```

本地 MySQL 可用根目录的 `docker-compose.yml`（`docker compose up -d`）。

```
apps/
├── api/    FastAPI 后端（Railway 部署此目录）
│   ├── app/            主应用（main.py 组装 14 个 router + 12 个 services）
│   ├── alembic/        数据库迁移（0017 已就绪，Railway release 自动执行）
│   ├── requirements.txt  Railway 构建用依赖清单
│   └── Procfile          release: alembic upgrade head / web: uvicorn
└── web/    Next.js 前端（Vercel 部署此目录，Root Directory 指到 apps/web）
```

## 完整架构图

```
┌──────────────────────────────────────────────────────────────────┐
│  前端 (Vercel)                                                   │
│  Next.js 14 · React 18 · ECharts                                 │
│  ─────────────────────────────────────────────                   │
│  11 阶段门控流水线                                               │
│  1 上传 → 2 Schema → 3 质量 → 4 分析 → 5 产物 → 5.5 报告         │
│  6 AI 采访 → 7 蒸馏 + 裁决 → 8 问题 → 9 方案 → 10 决策 → 11 PRD  │
└───────────────────────────┬──────────────────────────────────────┘
                            │ REST /api/v1 + JWT Bearer
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  后端 (Railway)                                                  │
│  FastAPI · SQLAlchemy 2 · Alembic · pandas                       │
│  ─────────────────────────────────────────────                   │
│  main.py (151 行组装层)                                          │
│  ├─ 14 routers (路由按域拆分)                                    │
│  ├─ 12 services (业务逻辑层)                                     │
│  └─ JobExecutor (进程内异步队列)                                 │
│                                                                  │
│  🔒 AI 防火墙 (ai_context.py)                                    │
│  - 出站白名单 7 键: goal / metrics / artifacts / quality /       │
│    schema / question / insights                                  │
│  - PII 脱敏 (邮箱/电话/ISO 日期保护还原)                         │
│  - 反馈原文不外泄 (_FEEDBACK_CONTENT_KEYS)                       │
│                                                                  │
│  🧰 工具调用 (deepseek.py)                                       │
│  ALLOWED_TOOL_NAMES: 12 个 = 8 只读 + 4 审批占位                 │
│  AnalysisPlan pydantic 校验 + _DANGEROUS_ARGUMENTS 拦截          │
│  CopilotOrchestrator: Understand→Plan→Validate→Execute→Summarize │
│                                                                  │
│  📊 确定性计算引擎 (analytics/engine.py)                         │
│  - run_eda / run_trend_analysis / run_funnel_analysis            │
│  - run_retention_analysis / run_anomaly_detection                │
│  - run_group_comparison (Pareto 分组对比)                        │
│  - quality.assess_quality (质量评分)                             │
│  - analytics/digest.py (findings digest 提炼)                    │
│                                                                  │
│  🤖 AI 服务 (DeepSeek OpenAI 兼容)                               │
│  - HTTP fallback (openai SDK 可选)                               │
│  - JSON mode / 指数退避重试 / usage 统计                         │
│  - 预算阀门 (工作区日限额投影，输出另有硬顶)                     │
│  - 截断感知与单次重试                                            │
│  - 无 key 降级 (绝不 500)                                        │
│                                                                  │
│  🔗 证据落地机制                                                 │
│  - 报告 findings 写为真实 AnalysisArtifact                       │
│  - distill-interview 自动落库 draft 洞察                         │
│  - 强制 evidence 非空确认                                        │
│  - 幻觉 id 丢弃 + 回退                                           │
└───────────────────┬──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│  数据库                                                          │
│  MySQL 8 (生产) / SQLite (开发测试可回退)                        │
│  ─────────────────────────────────────────────                   │
│  17 个 Alembic 迁移 (最新 0017_field_semantics)                  │
│  核心表 20 张 (V11_CORE_TABLE_NAMES)：                           │
│    users / projects / metric_definitions / datasets              │
│    dataset_versions / data_columns / data_quality_reports /      │
│    cleaning_operations / analysis_runs / analysis_artifacts /    │
│    insights / product_problems / solution_options /              │
│    decision_proposals / copilot_sessions / copilot_messages /    │
│    documents / document_versions / ai_runs / feedback_notes      │
│  其它业务表 4 张：interview_questions / interview_summaries /    │
│    auto_analysis_reports / analysis_report_narrations            │
│  Legacy 表 10 张 (V11_LEGACY_TABLE_NAMES，淘汰中)：              │
│    workspaces / workspace_members / tasks / task_links /         │
│    feedback_items / feedback_clusters / feedback_cluster_items / │
│    approval_requests / jobs / audit_logs                         │
└──────────────────────────────────────────────────────────────────┘
```

## 功能亮点

### 1. 11 阶段流水线
上传→自动报告→AI 采访→洞察蒸馏→问题/方案/决策→PRD，每一步状态门控由前端 `lib/workflow.ts` `stepCompletion()` 定义，后端实时响应。

### 2. 确定性计算铁律
pandas 引擎承担所有数值计算（EDA/趋势/留存/漏斗/异常/分组对比），LLM 永不直接产出统计数字，仅做整合叙述。

### 3. 真实工具调用
- 8 个只读分析工具（EDA/趋势/留存/漏斗/异常 + feedback summary + project context + dataset schema）
- 4 个审批占位工具（create_insight_draft/create_decision_draft/create_document_draft/request_approval）
- Pydantic 校验 + `_DANGEROUS_ARGUMENTS` 双端拦截 sql/python/path/url 等

### 4. Agent 编排
`CopilotOrchestrator`：理解→规划→验证→执行→总结闭环，Clarifying Question 机制 + 预算阀门 + 截断重试（单次重试 + max_tokens 翻倍）。

### 5. 结构化证据接地
非经典向量 RAG：每次 AI 调用前从 DB 检索项目报告聚合/分析产物/采访问答/已确认洞察，经白名单防火墙注入 bounded context，AI 输出强制引用真实资源 id（UUID），幻觉 id 丢弃 + 回退模板。

### 6. 工程化兜底
- 无 API key 时优雅降级（返回空草稿而非 500）
- AI 失败模板回退（`ai_status=fallback` 如实标注）
- 跨页恢复任务轮询（payload 带 `narration_job_id`/`generation_job_id`）
- 审计日志全链路记录（audit_logs 表 + `/audit-logs` 端点）

## 完整用户流程

1. **上传数据** (`POST /datasets/upload-batch`) → 解析 job → 质量评估 → 字段字典 → 自动分析 ≤4 个（EDA 恒在 + 按表类型选留存/趋势/分组/异常）。
2. **自动生成报告** (`POST /auto-report/compute`，纯确定性秒级) → 手动点"开始 AI 解读" (`POST /auto-reports/{id}/narrate`，异步 job)。
3. **AI 采访** (`interview/next-question`，一次一问、幂等去重、上限 10 问) → `interview/complete` 生成小结 → `distill-interview` 自动落库 draft 洞察。
4. **人工裁决** (stage7-copilot)：减法操作（弃用/编辑/确认），确认时强制 evidence 非空。
5. **问题→方案→决策**：AI 分别起草，人确认并写落选理由，独立审批流程。
6. **PRD 三遍生成**：大纲→分节并行 (`Semaphore(3)`) →连贯校对 (>6000 字触发，分批 ≤6 节)，降级链完整（任一遍失败→单次旧路径→模板回退）。
7. **导出/归档**：Markdown 下载，项目归档不可写。

## 核心技术栈（实际依赖声明）

### 后端 (`pyproject.toml`)
```
fastapi>=0.111,<1 | uvicorn[standard]>=0.30,<1 | sqlalchemy>=2.0,<3
alembic>=1.13,<2 | pymysql>=1.1,<2 | pydantic>=2.7,<3
pydantic-settings>=2.2,<3 | email-validator>=2.1,<3 | bcrypt>=4.2,<6
PyJWT>=2.8,<3 | python-multipart>=0.0.9,<1 | pandas>=2.1,<3
numpy>=1.26,<2 | scipy>=1.11,<2 | openpyxl>=3.1,<4 | httpx>=0.27,<1
```

可选 extras：`pip install -e ".[ml]"` 会额外装上 `scikit-learn>=1.4,<2`，仅供电给 `analytics/outliers.py` 的 LOF 离群检测——该库刻意不进必需依赖，缺失时 LOF 检测降级为空结果，其余流程不受影响。

### 前端 (`package.json`)
```
next@^14.2.15 | react@^18.3.1 | react-dom@^18.3.1
tailwindcss@^3.4.14 | echarts@^5.6.0 | lucide-react@^0.468.0
typescript@^5.6.3 | eslint 8.57.1 | prettier 3.3.3
```

---

## 仓库结构

```
AI_Product_Workspace/
├── README.md                       本文件
├── PROJECT_CONTEXT.md              项目事实基准（架构 / 模块 / API / 数据库 / 完成度 / 技术债）
├── docker-compose.yml              本地 MySQL 8
├── docs/dev-prompts/               开发任务 Prompt 归档
└── apps/
    ├── api/                        FastAPI 后端（Railway 部署此目录）
    │   ├── app/
    │   │   ├── main.py             组装层：CORS + legacy 标记中间件 + 异常处理器 + lifespan + 14 个 router + 3 个 health 端点
    │   │   ├── common.py           统一响应信封 ok()/error()、serialize、分页、_require_pandas 懒加载哨兵
    │   │   ├── ai_context.py       AI 出站防火墙 + 各阶段输出契约（1326 行）
    │   │   ├── models.py           V11 核心 20 表 + legacy 10 表（708 行）
    │   │   ├── schemas.py          请求 / 响应模型
    │   │   ├── auth.py             JWT (HS256) + bcrypt 密码
    │   │   ├── config.py           环境变量设置
    │   │   ├── db.py               引擎与会话 + SQLite 回退 + 启动补列安全网
    │   │   ├── routers/            14 个按域拆分的 router
    │   │   ├── services/           12 个业务逻辑模块（禁止反向导入 routers）
    │   │   ├── analytics/          纯计算层（engine / quality / parsing / text_metrics / digest / dag / corelation / outliers / types / enhanced_engine）
    │   │   └── infrastructure/     jobs.py（进程内 JobExecutor）+ llm/deepseek.py（DeepSeek 适配层）
    │   ├── alembic/versions/       17 个迁移（0001..0017，最新 0017_field_semantics）
    │   ├── tests/                  27 个测试模块 + conftest.py；test_route_manifest.py 冻结 137 条路由
    │   ├── pyproject.toml          依赖声明 + ruff / pytest 配置
    │   ├── requirements.txt        Railway 构建读取的依赖清单
    │   └── Procfile                release: alembic upgrade head / web: uvicorn
    └── web/                        Next.js 前端（Vercel 部署此目录，Root Directory 指到 apps/web）
        ├── middleware.ts           登录门禁 + legacy 路由重定向
        ├── app/
        │   ├── (auth)/login/       登录页
        │   └── (workspace)/        工作台（阶段 1–5）+ data/ + history/ + stage6-interview .. stage11-prd
        ├── components/             layout / workflow / common / analysis / copilot / hooks / ui
        ├── lib/                    api.ts · workflow.ts · navigation.ts · chartOption.ts · format.ts · upload.ts
        └── package.json            依赖与脚本（dev / build / lint / typecheck / format）
```

## 本地开发

后端（Python ≥ 3.11）：

```powershell
cd apps/api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy ..\..\.env.example .env   # 填入 DEEPSEEK_API_KEY 与 DATABASE_URL
uvicorn app.main:app --reload --port 8000
```

前端（Node ≥ 18）：

```powershell
cd apps/web
npm install
copy ..\..\.env.example .env   # 或单独建 .env.local 设置 NEXT_PUBLIC_API_BASE_URL
npm run dev
```

本地 MySQL 可用根目录的 `docker-compose.yml`（`docker compose up -d`），或直接让后端回退 SQLite（`ALLOW_SQLITE_FALLBACK=true`）。

## 部署到 Railway（后端）

1. GitHub 仓库导入 Railway → New Project → Deploy from GitHub repo。
2. Service 设置：**Root Directory = `apps/api`**（构建自动识别 Python + requirements.txt，启动读取 Procfile）。
3. 添加 MySQL：Railway Marketplace 里创建 MySQL 8 插件，并把它的连接变量合入 API 服务（或自建外置 MySQL）。
4. 在 API 服务的 Variables 里配置（全部必填项见 `.env.example`）：

   | 变量 | 说明 |
   |---|---|
   | `DATABASE_URL` | `mysql+pymysql://user:pass@host:3306/dbname`（Railway MySQL 插件变量 `MYSQL_URL` 需要手动加 `+pymysql`） |
   | `APP_SECRET_KEY` | 强随机串（`python -c "import secrets;print(secrets.token_urlsafe(48))"`） |
   | `API_CORS_ORIGINS` | 前端 Vercel 域名，如 `https://your-app.vercel.app`（多个用逗号分隔） |
   | `DEEPSEEK_API_KEY` | DeepSeek 平台密钥 |
   | `DATA_ROOT` | `/data`（建议同时给服务挂 Volume 挂到 `/data`，否则重新部署会丢失上传文件） |

   可选：`APP_ENV=production`、`JWT_ACCESS_TOKEN_MINUTES`、`MAX_UPLOAD_SIZE_MB`。
5. Procfile 的 `release: alembic upgrade head` 会在每次部署时自动执行迁移（0017 已就绪），无需手动操作。
6. 部署完成后用 `https://<railway-domain>/health` 验证。

## 部署到 Vercel（前端）

1. New Project → 导入同一 GitHub 仓库。
2. Project 设置：**Root Directory = `apps/web`**（框架自动识别 Next.js）。
3. Environment Variables：`NEXT_PUBLIC_API_BASE_URL = https://<railway-domain>/api/v1`（注意带 `/api/v1` 后缀；`NEXT_PUBLIC_` 变量在构建期注入，改完需要 Redeploy）。
4. Deploy 完成后打开站点，用注册流程创建第一个账号。

## 上线检查清单

- [ ] Railway `/health` 返回 `{"status":"ok"}`，`/health/ready` 显示 mysql 已连接
- [ ] Vercel 站点注册第一个 Owner 账号成功（说明 CORS 与 JWT 均已通）
- [ ] 上传一份 CSV → 报告秒级出现 → DeepSeek 解读正常（`DEEPSEEK_API_KEY` 生效）
- [ ] 若上传文件需要在重新部署后保留：给 Railway 服务挂载 Volume 到 `DATA_ROOT`

## 安全说明

- `.env` 不入库（`.gitignore` 已覆盖），所有敏感配置走平台环境变量。
- 前端仅持有 JWT 与会话镜像 cookie；真实鉴权与数据隔离全部在 API 层。
- 生产环境务必设置强 `APP_SECRET_KEY`，并将 `API_CORS_ORIGINS` 收紧到实际前端域名（默认回落 `["*"]` 仅供开发）。
