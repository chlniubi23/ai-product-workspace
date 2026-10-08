# AI Product Workspace

**「AI 辅助、人工主导」的产品数据分析与决策工作台。**

上传数据 → 确定性引擎自动完成全部统计计算 → AI 负责采访、蒸馏洞察、起草问题/方案/决策 → 三遍式生成深度 PRD。每一条 AI 结论都强制携带证据引用，每一个统计数字都可追溯到 pandas 的确定性计算——**LLM 在这个系统里永远不自己算数**。

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Next.js 14](https://img.shields.io/badge/Next.js%2014-000000?style=flat-square&logo=nextdotjs&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-4479A1?style=flat-square&logo=mysql&logoColor=white)
![pandas](https://img.shields.io/badge/pandas-150458?style=flat-square&logo=pandas&logoColor=white)
![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-4D6BFE?style=flat-square)
![tests](https://img.shields.io/badge/tests-456%20passed%20%2B%20golden%20regression-success?style=flat-square)

## 🚀 在线体验

| | |
|---|---|
| **应用地址** | https://ai-product-workspace.vercel.app |
| **演示账号** | `demo@apw.dev` / `Demo2026!apw` |
| **API（Railway）** | https://api-production-f21d.up.railway.app/health/ready |

**三分钟体验路径**：登录 → 新建项目 → 上传任意 CSV → 等待秒级数据概况（确定性统计）→ 点「开始 AI 解读」看叙述报告与信任卡 → 进入「AI 采访」被自适应追问 → 「洞察蒸馏」裁决 AI 草稿 → 一路走到「交付文档」看三遍式生成的 PRD。

---

## 这个项目不是「又一个 RAG Demo」

大多数学生级 LLM 项目的形态是：向量库 + prompt + 流式输出。这个项目回答的是一个更难的问题：**如何把 LLM 安全地约束进一个垂直业务工作流，并让它的每句话可审计**。

### 1. AI 出站上下文防火墙（`apps/api/app/ai_context.py`，1349 行）

所有发给 LLM 的上下文只允许 7 个白名单键；反馈原文、行级数据、密钥、存储路径在出口处被结构化剥离；PII（邮箱/电话）正则脱敏且**保护 ISO 日期不被电话正则误杀**；聚合列表白名单 17 键——任意来源的走私数据会在边界被丢弃，而不是靠提示词恳求模型「不要看」。

### 2. 算数归 pandas，叙述归 LLM

EDA / 趋势 / 留存 / 漏斗 / 分组 / 异常 / 质量评分全部由确定性引擎计算（`analytics/` 11 个模块，含派生列血缘识别与伪相关排除）。LLM 拿到的是聚合结果，产出永远是 draft 状态——确认、编辑、弃用全部由人完成。

### 3. 三层反幻觉机制

- **输入侧**：AI 只见白名单聚合 + 服务端注入的证据，不信任前端传参；
- **输出侧**：每条 claim 强制携带 evidence 引用，幻觉资源 id 与入参求交集，对不上直接丢弃；
- **数值侧**：`fact_check` 把 AI 文本里出现的每个数字回查聚合索引（4 位有效数字舍入容忍），无法溯源的数字进人工复核清单——eval harness 用 3 份 golden 数据全链路度量 `fact_check_rate` / `evidence_hit_rate`。

### 4. 三遍式深度文档生成（最快 470 秒产出 10 节 PRD）

大纲瘦身（只看摘要层）→ 分节并行撰写（前两节顺序保叙事 + `Semaphore(3)` 并行其余，每章按 `key_refs` 裁剪专属上下文）→ 连贯校对（分批去重、标题契约校验、105% 字数上限）。任一遍失败都有降级链：单次旧路径 → 确定性模板，**永不静默失败**（`ai_status=fallback` 如实标注）。

### 5. 有界 Copilot（非自主 Agent）

Plan → Validate → Execute → Answer 两段式编排；12 个工具白名单（8 个只读已实现），`sql/python/path/url` 等危险参数在**规划校验与执行两端**都被拦截；plan 里的 project/dataset 不得越出请求范围。这是一条刻意的产品判断：**工作流应用 + 一个有界副驾，而不是放飞的自主 agent**。

### 6. 成本与预算工程

spend-then-account 预算模型（调用前只查日阀门、调用后只记账，已付费结果绝不丢弃）；`HARD_OUTPUT_CAP=16384` 模块常量硬顶输出（刻意不可配置）；截断感知 + 单次翻倍重试；全部 LLM 调用进 `ai_runs` 账本（tokens/延迟/finish_reason/指纹），可审计可回放。

---

## 量化成果

| 指标 | 数值 | 说明 |
|---|---|---|
| 后端测试 | **456 passed + 1 skipped + 1 xfailed**（36 个测试文件） | 覆盖 RBAC/隔离、AI 降级边界、决策链规则、防火墙契约 |
| 真实数据回归 | 9 份真实业务 CSV 的 golden profiles | 改计算口径立刻暴露偏差 |
| **AI 输出质量基线** | **蒸馏结论数字回查 100%（15/15）· 证据命中 100% · 1,464 tokens/结论** | [真实 key 全链路 eval 报告](docs/eval/eval-report-2026-10-08.json) |
| PRD 生成提速 | 19 min → **470 s（2.4×）** | 三遍式改造 + 分节并行（live 实测） |
| Token 成本 | **−56%**（82,980 tokens/份） | 大纲瘦身 + 上下文裁剪 |
| 路由面冻结 | 137 条路由由测试清单锁定 | 任何意外增删路由都会挂 CI 级测试 |
| 迁移纪律 | 17 个 Alembic 迁移从零建库可复现 | + 解析完整性对账（二次读取防「带病入库」） |

> eval 基线的诚实注脚：叙述报告的数字回查率为 60.5%（76 个数字中 46 个直接命中聚合索引）——未命中的 5 处全部是**模型自己算出来的派生值**（如"两数据集共 18 行"、"7/8 显著占比 87.5%"），fact-checker 把它们准确识别出来推入人工复核清单。防线抓的正是"模型偷偷算数"这个类别——这是它该干的活。

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│  前端 (Vercel)                                                   │
│  Next.js 14 · React 18 · ECharts                                 │
│  11 阶段门控流水线（门控事实定义 lib/workflow.ts stepCompletion）  │
└───────────────────────────┬──────────────────────────────────────┘
                            │ REST /api/v1 + JWT Bearer
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  后端 (Railway)                                                  │
│  FastAPI · SQLAlchemy 2 · pandas · 进程内 JobExecutor             │
│                                                                  │
│  main.py 组装层（151 行）                                        │
│  ├─ 14 routers  按域拆分，路由清单测试冻结                        │
│  ├─ 12 services 业务逻辑层（禁止反向依赖 routers）                │
│  ├─ analytics/  确定性计算层（血缘/离群/显著性/类型感知统计）      │
│  │                                                              │
│  ├─ 🔒 AI 防火墙 ai_context.py     出站白名单 + 全部输出契约      │
│  ├─ 🧰 工具调用 deepseek.py        白名单 + 双端危险参数拦截      │
│  ├─ 🤖 编排 CopilotOrchestrator    Plan→Validate→Execute→Answer  │
│  └─ 📒 账本 ai_runs               tokens/延迟/指纹全量记录        │
└───────────────────┬──────────────────────────────────────────────┘
                    ▼
             MySQL 8（生产）/ SQLite（测试回退）
             17 个 Alembic 迁移 · 20 核心表 + 10 legacy 表
```

### AI 能力边界（在代码中被强制执行，非文档口号）

| 确定性代码（pandas） | AI（DeepSeek） |
|---|---|
| ✅ EDA / 趋势 / 留存 / 漏斗 / 分组 / 异常 | ❌ 不产出任何统计数字 |
| ✅ 质量评分 / findings digest / 离群与血缘 | ✅ 叙述报告 / 自适应采访提问 |
| ✅ 所有数值计算与口径标注（stat_note） | ✅ 起草洞察 / 问题 / 方案 / 决策 / PRD |
| | ⚠️ 全部产出为 draft，确认动作永远属于人 |
| | ⚠️ 每条结论必须引用真实资源 id |

### 11 阶段流水线

上传 → Schema 审阅 → 质量报告 → 分析运行 → 分析产物 →（AI 报告：先算后叙）→ AI 采访（自适应一问一答）→ 洞察蒸馏（减法裁决）→ 产品问题 → 方案讨论 → 产品决策 + 独立审批 → 交付文档（PRD/周报/复盘）→ 归档。

---

## 工程质量细节（测试运维视角）

- **AI 输出质量评估 harness**：golden 数据集 + 全链路产出度量（`fact_check_rate` / `evidence_hit_rate` / `token_per_conclusion`），`APW_EVAL=1` 可复跑；首批真实 key 基线见上表与 `docs/eval/`；
- **GitHub Actions CI**：push 即跑后端 ruff + 456 项测试 + 前端 typecheck/lint；路由清单测试为版本无关实现，跨 FastAPI 0.115（本地）与 0.142（CI/生产）双重验证；
- **解析完整性对账**：上传文件二次独立读取核对行列数，不一致抛 `PARSE_INTEGRITY_FAILED`，绝不带病入库；逐列语义/解析率落 `parse_manifest` 凭证；
- **冻结式防回归**：137 条路由 (path, methods, name) 全量冻结在 `test_route_manifest.py`；
- **ruff 零告警基线**，无 `noqa` 豁免；
- **审计日志**全链路（谁在何时确认了什么），`GET /audit-logs` 可查。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2 · Alembic · pandas · scipy · PyMySQL |
| 前端 | Next.js 14 (App Router) · React 18 · TailwindCSS · ECharts 5 |
| LLM | DeepSeek（OpenAI 兼容；JSON mode / 指数退避 / httpx 回退） |
| 数据库 | MySQL 8（生产）/ SQLite（开发测试） |
| 部署 | Vercel（前端）+ Railway（API + MySQL + 持久卷），GitHub push 双端自动部署 |
| 质量 | pytest 456+ · ruff · eslint/prettier · golden 回归 · eval harness |

## 本地开发

```powershell
# 后端（Python ≥ 3.11）——不配 .env 即走 SQLite，最省事
cd apps/api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
uvicorn app.main:app --reload --port 8000

# 前端（Node ≥ 18）
cd apps/web
npm install
npm run dev        # .env.local 设 NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
```

需要 MySQL 时：根目录 `docker compose up -d`，或在 `apps/api/.env` 配 `DATABASE_URL=mysql+pymysql://...`。要启用 AI 能力需配 `DEEPSEEK_API_KEY`（不配也完全可用，AI 优雅降级为空草稿）。

## 部署要点（本仓库已在跑的配置）

- **Railway（`apps/api`）**：服务 Root Directory = `apps/api`（monorepo 关键）；MySQL 插件 + `/data` 持久卷；环境变量 `DATABASE_URL`（必须 `mysql+pymysql://` 前缀）、`APP_SECRET_KEY`、`DEEPSEEK_API_KEY`、`API_CORS_ORIGINS`（**默认值是 `http://localhost:3000` 而非通配符**，生产必须显式设前端域名）、`DATA_ROOT=/data`。
  注意：**Railway 不执行 Procfile 的 `release:` 行**，线上 schema 由应用启动时 `Base.metadata.create_all` 自举；若要走迁移流程先 `alembic stamp head` 再 `upgrade`。
- **Vercel（`apps/web`）**：Root Directory = `apps/web`；`vercel.json` 已声明 `framework: nextjs`；环境变量 `NEXT_PUBLIC_API_BASE_URL = https://<railway域名>/api/v1`（构建期注入，改动需重新部署）。
- 详细的部署 Runbook 与踩坑记录见 [PROJECT_CONTEXT.md §14](PROJECT_CONTEXT.md)。

## 仓库结构

```
AI_Product_Workspace/
├── PROJECT_CONTEXT.md        项目事实基准：架构/模块/API/数据库/技术债，逐批次维护
├── docker-compose.yml        本地 MySQL 8
└── apps/
    ├── api/                  FastAPI 后端
    │   ├── app/
    │   │   ├── ai_context.py       AI 出站防火墙 + 11 个阶段输出契约
    │   │   ├── routers/            14 个按域拆分的 router（137 条路由）
    │   │   ├── services/           12 个业务模块（ai_stages/documents/interview/...）
    │   │   ├── analytics/          确定性计算层（engine/dag/outliers/fact_check/...）
    │   │   └── infrastructure/     JobExecutor + DeepSeek 适配层
    │   ├── alembic/versions/       17 个迁移
    │   └── tests/                  36 个测试文件（含路由清单/golden/eval harness）
    └── web/                  Next.js 前端
        ├── middleware.ts           登录门禁
        ├── app/(workspace)/        工作台 + 6 个阶段页 + 数据管理 + 历史
        └── lib/workflow.ts         11 阶段门控事实定义
```

## 安全说明

- `.env` 从未入库（全历史扫描验证）；密钥全部走平台环境变量；
- 前端 middleware 仅做页面级软门禁，真实鉴权与 workspace 隔离全在 API 层（成员校验 + 边界检查）；
- 出站 LLM 上下文经防火墙白名单 + PII 脱敏；用户反馈原文永不外泄给模型。
