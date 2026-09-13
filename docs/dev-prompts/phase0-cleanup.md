# 开发 Prompt · Phase 0：收口（可直接交给开发对话执行）

> 复制本文件全文粘贴给开发对话即可。本阶段**只做工程收口，不接入主管线、不改变任何业务行为**。

---

## 目标

`apps/api/app/analytics/` 下有一批未提交、未接入主管线的增强分析引擎代码（2026-09-12 新增，详见 `PROJECT_CONTEXT.md` §13）。当前工作区存在三项工程缺陷：依赖未声明、lint 基线被破坏、测试不被收集。本阶段**只修复这三项并清理临时文件**，让这批代码进入「可测试、可审查、可部署」的状态。

**本阶段完成后，增强引擎仍然是未接入状态**——接入是 Phase 1 的事。

## 涉及模块

| 文件                                               | 操作                                                   |
| ------------------------------------------------ | ---------------------------------------------------- |
| `apps/api/pyproject.toml`                        | 补依赖声明                                                |
| `apps/api/requirements.txt`                      | 补依赖声明（与 pyproject 保持一致）                              |
| `apps/api/app/analytics/corelation.py`           | 仅修 lint（scipy 导入保留）                                  |
| `apps/api/app/analytics/enhanced_engine.py`      | 仅修 lint                                              |
| `apps/api/app/analytics/outliers.py`             | sklearn 改可选导入 + 仅修 lint                              |
| `apps/api/app/analytics/dag.py`                  | 仅修 lint                                              |
| `apps/api/app/analytics/types.py`                | 仅修 lint                                              |
| `apps/api/app/analytics/quality.py`              | 仅修新增段的 lint（**不动 `assess_quality`/`QualityReport`**） |
| `apps/api/app/analytics/test_enhanced_engine.py` | **移动到** `apps/api/tests/test_enhanced_engine.py`     |
| `apps/api/demo_fix.py`                           | **删除**                                               |
| `apps/api/verify_fix.py`                         | **删除**                                               |
| `apps/api/weekly_report.csv`                     | 移入 `apps/api/tests/fixtures/samples/` 或删除（二选一）       |

## 需要修改的逻辑

### 1. 依赖声明

- `pyproject.toml` 的 `[project.dependencies]` 与 `requirements.txt` **同步新增** `scipy>=1.11,<2`。
  - 依据：`corelation.py` 顶层 `from scipy import stats`；`enhanced_engine.py:256` 函数内 `from scipy import stats`。
  - 两个文件必须一致（`requirements.txt` 注释已声明「Mirrors [project.dependencies]」，是 Railway 构建实际读取的文件）。
- `pyproject.toml` 的 `[project.optional-dependencies]` 新增 `ml = ["scikit-learn>=1.4,<2"]`。
- `outliers.py:detect_outliers_lof` 的 `from sklearn.neighbors import LocalOutlierFactor` 改为**函数内可选导入**：`ImportError` 时返回 `{col: [] for col in columns}`，与现有 `except Exception` 降级语义一致。
  - **禁止**把 `scikit-learn` 加进必需依赖：LOF 是可选能力，其余 4 个检测函数（IQR / Z-score）不依赖它。

### 2. ruff 归零

- 当前 `ruff check app tests` 报 **81 错**，全部来自上表文件（`quality.py` 新增段 5 个 + 5 个新文件 + 测试文件）。
- 先执行 `ruff check app tests --fix`（可自动修 66 个，主要是 `UP006`/`UP045` 旧式注解：`Dict`→`dict`、`List`→`list`、`Optional[X]`→`X | None`）。
- 剩余手工修复，至少包含：
  - `quality.py:437`、`quality.py:441` 的 `SIM102`（嵌套 `if` 合并为单个 `if`）——注意保持原判断逻辑等价（两处都是 `inferred == "ordinal"` 的重复判断，合并时勿改变语义）。
  - 其余按 ruff 提示逐条修复。
- **禁止**用 `per-file-ignores`、`# noqa`、放宽 `[tool.ruff.lint]` 规则等方式绕过——项目基线是**零告警**，`alembic/**` 与 `tests/conftest.py` 的既有豁免是唯一例外。

### 3. 测试归位

- `apps/api/app/analytics/test_enhanced_engine.py` → `apps/api/tests/test_enhanced_engine.py`。
  - 原因：`pyproject.toml` 的 `[tool.pytest.ini_options] testpaths = ["tests"]`，放在 `app/analytics/` 下**永远不会被收集**。
  - **已实测：该文件单独运行是绿的（`9 passed in 8.51s`，9 个用例）**，所以移动本身不会引入失败；但移动后必须确认它并入全量套件后仍然全绿。
- 移动后修改：
  - `from .enhanced_engine import ...` → `from app.analytics.enhanced_engine import ...`
  - 删除文件末尾的 `if __name__ == "__main__":` 演示块（第 310 行起至文件末，约 44 行）与其上方的「运行测试命令」注释块。
  - 保留全部 9 个 `test_*` 用例与断言，**不得删除或弱化任何断言**。
- 若 `weekly_report.csv` 保留，则放入 `apps/api/tests/fixtures/samples/` 并在测试中以相对路径引用（参考既有 `tests/fixtures/samples/business_table_text_metrics.csv` 的用法）；否则删除。

### 4. 清理临时文件

- 删除 `apps/api/demo_fix.py`、`apps/api/verify_fix.py`：两者均为临时验证脚本，含硬编码绝对路径（`<本地用户目录>\...`），不属于代码库。
- `weekly_report.csv` 按第 3 条处理，**不得留在 `apps/api/` 根目录**。

## 注意事项

- **不改任何函数签名与算法逻辑**；本阶段是纯工程整理。`enhanced_engine`/`dag`/`corelation`/`outliers`/`types` 的行为必须逐字节不变。
- **不动 `quality.py` 的既有生产契约**：`assess_quality`、`QualityReport`、`infer_column_type`、`run_quality_checks`/`check_data_quality` 别名一律不改。只修新增段（`DataQualityMetrics` 起）的 lint。
- `quality.py` 当前已有未提交改动，**不要覆盖或回退**它。
- 不要动 `apps/api/app/analytics/engine.py`（生产引擎）与任何 `routers/`、`services/`。
- 不要提交 git（由用户决定提交时机）。

## 测试要求

在本阶段结束后，以下命令必须全部通过：

```bash
cd apps/api

# 1. lint 零告警
.venv/Scripts/python.exe -m ruff check app tests

# 2. 既有测试无回归：移动后应为 361 passed, 1 xfailed（= 原 352 + 新 9）
.venv/Scripts/python.exe -m pytest tests -q

# 3. 新测试被收集（总数应为 362，原 353 + 新 9）
.venv/Scripts/python.exe -m pytest --collect-only -q | tail -3

# 4. 模块可导入
.venv/Scripts/python.exe -c "from app.analytics.enhanced_engine import run_enhanced_analysis; print('OK')"
```

**额外要求（sklearn 降级验证）**：在**未安装 scikit-learn** 的环境中（可临时 `pip uninstall scikit-learn` 后验证，或新建干净 venv），第 4 条命令仍须成功，且 `detect_outliers_lof` 返回空列表而非抛错。

## 完成标准

- [ ] `ruff check app tests` 输出 **0 错**（无新增 `noqa`/`per-file-ignores`）。
- [ ] `pytest tests -q` 为 `361 passed, 1 xfailed`（原 352 无回归 + 新增 9 全绿）。
- [ ] `pytest --collect-only -q` 收集数为 **362**，且 `tests/test_enhanced_engine.py` 在列表中。
- [ ] 未安装 scikit-learn 时 `enhanced_engine` 仍可导入。
- [ ] `pyproject.toml` 与 `requirements.txt` 的依赖声明一致（含 scipy）。
- [ ] `git status` 中不再出现 `demo_fix.py` / `verify_fix.py` / `weekly_report.csv`（根目录）。
- [ ] `git diff` 中不存在对 `assess_quality`、`engine.py`、`routers/`、`services/` 的任何改动。
