# AI Product Workspace

「AI 辅助、人工主导」的产品数据分析与决策工作台：上传数据后，全部统计由 pandas 确定性计算完成，LLM 只做整合解读——自适应采访、洞察蒸馏（自动落库草稿 + 减法裁决）、两段式深度 PRD 生成，证据链全程可追溯。

| 应用 | 技术 | 部署平台 |
|---|---|---|
| `apps/web` | Next.js 14（App Router）+ Tailwind + ECharts | **Vercel** |
| `apps/api` | FastAPI + SQLAlchemy 2 + Alembic + pandas | **Railway**（配 MySQL） |

- 数据侧铁律：所有统计数字由确定性代码计算，LLM 永远不自己算数。
- AI 侧铁律：AI 只产草稿，采纳/确认永远由用户完成；每条结论必须引用真实资源 id。

## 仓库结构

```
apps/
├── api/    FastAPI 后端（Railway 部署此目录）
│   ├── app/            主应用（main.py 组装 14 个路由 + services）
│   ├── alembic/        数据库迁移（0016 已就绪，Railway release 自动执行）
│   ├── requirements.txt  Railway 构建用依赖清单
│   └── Procfile          release: alembic upgrade head / web: uvicorn
└── web/    Next.js 前端（Vercel 部署此目录，Root Directory 指到 apps/web）
```

前后端**同仓库分离部署**：Vercel 与 Railway 都支持在平台设置里指定 Root Directory，无需拆成两个仓库。

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
5. Procfile 的 `release: alembic upgrade head` 会在每次部署时自动执行迁移（0016 已就绪），无需手动操作。
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
