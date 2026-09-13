# 开发 Prompt · Phase 1：增强分析引擎完整集成（可直接交给开发对话执行）

> 复制本文件全文粘贴给开发对话即可。
> **前置依赖：必须先完成 `phase0-cleanup.md`**（依赖声明 / ruff 归零 / 测试归位 / 临时文件清理）。未完成 Phase 0 不得开始本阶段。
> 本文件中的「涉及模块」「函数名」均已对照真实代码核实，不是推测。

---

## ⚠️ 开工前必须确认的三个设计决策

以下三点**必须先向用户取得明确答复**，不得自行选择。若用户未答复，停下并提问。

### 决策 1：行级数据能否出站（最重要，触及安全边界）

现状：`analytics/outliers.py` 的对象级离群值包含 `row_index` + `value`，属**行级数据**。而 `app/ai_context.py` 的防火墙**设计上禁止行级数据出站**（`_ROW_LIST_KEYS`，且注释明确「row-level lists are dropped even when nested in an otherwise valid artifact」）。实测：`outlier_detection.by_column` 穿过 `build_ai_context` 后变成 `{}`。

两个可选方案：

- **方案 A（推荐）**：**不出行号**。改为每列有界聚合，形状如
  `{"name": "周活跃用户", "method": "iqr_and_zscore", "count": 3, "rate": 0.27, "min_value": ..., "max_value": ..., "top_values": [2950000, 1180000, ...]}`（≤5 个值，不含行索引），整体作为 `metrics` 列表的一项。
  - 保留「AI 能引用具体数值」的价值，不暴露行身份，**防火墙零改动**。
- **方案 B**：若业务确需「第 N 行的某字段异常」这类**可定位**引用，则须在 `ai_context.py` 新增一个**具名豁免键**（如 `row_references`），并配套守护测试断言「只有该键可携带行引用，其他键仍被拦截」。这是**对安全边界的放宽**，必须由用户明确拍板。

> 本 Prompt 后续描述按**方案 A** 编写。若用户选方案 B，需在 §3 第 3 条与 §5 测试要求中相应调整。

### 决策 2：是否给 `_AGGREGATE_LIST_KEYS` 新增 `pairs` 键

现状：`correlation_analysis.pairwise_correlations` 每项是**变量对聚合**（`var1/var2/pearson_r/pearson_p/spearman_rho/robust_pearson/robust_spearman/is_significant/correlation_strength/interpretation/robustness_note`，**全为标量，无行数据**）。实测：该列表穿过防火墙后整块消失，只剩 `summary` 计数。

- **推荐**：在 `ai_context._AGGREGATE_LIST_KEYS` 新增 `pairs` 键（**实测该集合当前 16 个键**：`bins/breakdown/categories/cohorts/counts/evidence/ids/labels/means/medians/metrics/periods/quantiles/rates/series/stages`）。这是**对防火墙的有意放宽**，须配套守护测试（见 §5）。
- 备选：不改防火墙，改为在 `services/ai_stages.py` 写适配映射（把相关对映射到既有允许键）——语义会变得不直观，不推荐。

### 决策 3：新分析类型是否进入自动分析计划

现状：`services/analysis_pipeline.py` 的 `_AUTO_ANALYSIS_LIMIT = 4`，`_auto_analysis_plan` 已占满（EDA 恒在 + 留存/趋势/分组 三选一 + 异常 + `group_comparison`）。

- **推荐**：新分析类型**仅手动（阶段 4）可选**，不挤占自动计划。`_auto_analysis_plan` 零改动。
- 备选：若要自动选中，须明确**替换哪一项**及理由，并同步更新 `tests/test_group_comparison.py` 等锁定自动计划的测试。

---

## 目标

把已收口的五项计算能力接入生产管线，使其在**阶段 4（分析运行）/ 阶段 5（分析产物）/ 阶段 5.5（自动报告）**真实生效，并可在 AI 上下文（报告叙述、文档生成、采访蒸馏）中被引用：

1. **派生列血缘识别**（`dag.py`）→ 排除机械伪相关
2. **显著性检验 + 稳健相关**（`corelation.py`）→ 每个相关系数带 p 值与剔除离群点后的稳健估计
3. **对象级离群值**（`outliers.py`）→ 按决策 1 的形态输出
4. **类型感知统计**（`types.py`）→ 序数列输出分布+众数、占比列输出分位数+极值
5. **双维度质量**（`quality.py` 新增段）→ 数据质量 + 分析质量分离

## 涉及模块（已核实）

| 文件 | 作用 |
|---|---|
| `apps/api/app/analytics/engine.py` | 生产引擎；替换 EDA 内的相关块（见 §3.2） |
| `apps/api/app/analytics/quality.py` | `assess_quality` **契约不可改**；双维度仅作附加键 |
| `apps/api/app/analytics/dag.py` / `corelation.py` / `outliers.py` / `types.py` | 计算层（已存在，按 §3 调整输出形状） |
| `apps/api/app/services/analysis_pipeline.py` | `_analysis_artifacts`（按 `kind` 分派）/ `SUPPORTED_ANALYSIS_TYPES` / `_analysis_config_validation` / `_auto_analysis_plan` |
| `apps/api/app/services/ai_stages.py` | `_AI_ARTIFACT_KEY_MAP` / `_reduce_artifact_payload_for_ai`（适配层对齐点） |
| `apps/api/app/ai_context.py` | `_AGGREGATE_LIST_KEYS`（仅按决策 2 增 `pairs`） |
| `apps/api/app/services/auto_report.py` | `_compute_report_aggregates`（报告聚合携带新口径） |
| `apps/api/app/analytics/digest.py` | 可选：新增发现类型（如 `pseudo_correlation_excluded`） |
| `apps/api/app/routers/analysis.py` | `validate-config` 校验分支（复用 `_analysis_config_validation`，通常无需改） |
| `apps/api/app/infrastructure/llm/deepseek.py` | `ALLOWED_TOOL_NAMES`（仅当要暴露给 Copilot） |
| `apps/web/lib/chartOption.ts` | 新图表类型渲染 |
| `apps/api/tests/test_route_manifest.py` | **仅当新增端点时**同步重生成冻结清单 |

## 需要修改的逻辑

### 1. 合并「派生列」的两套定义（必须做，否则语义冲突）

- 生产侧已有权威定义：`DataColumn.source == "extracted"`（batch 14，列名约定 `{源列}__{指标}`），由 `job_handlers._handle_dataset_parse` 写入。
- `dag.detect_derived_columns` 是**纯列名模式识别**（中文比率关键词、括号引用、变化率前缀）。
- **要求**：以 `source == "extracted"` 为**权威来源**，`dag` 的识别结果在其上**叠加**（两者取并集，`source=extracted` 优先）。不得出现两套互相矛盾的「派生列」判定。
- `digest.py` 的 `_missing_findings` / `_constant_findings` 已按「列名含 `__`」跳过派生列，须保持该行为。

### 2. 消除重复的相关性来源（必须做）

- `AnalysisEngine.run_eda` 当前已产出 `payload["correlations"]`（朴素 Pearson，形状 `{left, right, correlation}`，无 p 值、无伪相关排除）。
- **不得新增第二条相关性通路**。要求：用 `corelation.compute_full_correlation_matrix` **替换** EDA 内的相关计算块，并且：
  - **必须保留** `payload["correlations"]` 的 `{left, right, correlation}` 形状与语义——`services/auto_report.py:_compute_report_aggregates`（读 `eda_payload["correlations"]` 构造 `correlation_pairs`）与 `analytics/digest.py:_correlation_findings` 依赖它，改形状会静默破坏报告与 digest。
  - 新增字段（`pearson_p`、`robust_pearson`、`is_significant`、`robustness_note` 等）以**并列追加**方式加入同一 payload（如新增 `payload["correlation_pairs_detail"]`），不改动既有键。
  - 伪相关（派生列对 / `|r| >= 0.98` 的机械相关）**从 `correlations` 中排除**，并把排除数量写入 `payload["excluded_correlation_pairs"]`（便于审计与 digest 披露）。

### 3. 新产物形状必须对齐防火墙（必须做）

实测结论（`PROJECT_CONTEXT.md` §11.5）：

- 以**原始列名为键的 dict** 输出会被静默丢弃（列名 `content`/`comment` 命中 `_FEEDBACK_CONTENT_KEYS`）。
- 非白名单键下的**列表**会被整块丢弃。

**要求**：所有新产物一律采用
- 形状：`[{name: ..., ...}, ...]` 列表（不是 `{列名: {...}}`）
- 键位：放在 `metrics` / `categories`（既有白名单键）或按决策 2 新增的 `pairs` 下
- 内容：**不含行级数据**（按决策 1 的形态）

具体到各产物：

| 产物 | 目标形状 |
|---|---|
| 类型感知统计 | `metrics` 列表，每项 `{name, inferred_type, type_label, ...统计量}`；序数列含 `distribution`/`mode`/`mode_percentage`，占比列含 `quantiles`/`min_value`/`max_value` |
| 派生列血缘 | `metrics` 列表，每项 `{name, is_derived, lineage_summary, derived_from, derivation_type, derivation_formula}` |
| 相关性对 | 按决策 2：`pairs` 列表（或映射到既有键） |
| 离群值 | 按决策 1 方案 A：`metrics` 列表，每项 `{name, method, count, rate, min_value, max_value, top_values}` |
| 双维度质量 | 见 §4，走 `summary_json` 而非 AI 产物 |

### 4. 双维度质量必须非破坏接入（关键，改错会破坏门控）

- `assess_quality` 的 `overall_score` 与 `status` 是**阶段 3 门控**（`stepCompletion` 读 `version.quality_report`）与 **`_prepare_analysis_run` 的质量门**（`version.quality_report.status == "failed"` 时拒绝分析，除 `accept_quality_risk`）的判据。
- **要求**：
  - `assess_quality` 的签名、`overall_score` 算法、`status` 分档（`passed`/`needs_review`/`failed`，阈值 95/80）**一律不变**。
  - 双维度结果只写入 `DataQualityReport.summary_json` 的**附加键**（建议 `summary_json["dual_quality"] = {data_quality, analysis_quality, summary, recommendation}`）。
  - `services/datasets.py:_quality_summary` 的返回三元组 `(score, status, summary)` 语义不变。
  - **回归锁定**：改造前后，同一份数据的 `overall_score` 与 `status` 必须逐值一致（见 §5 测试）。

### 5. 新分析类型全链路接线

按决策 3（推荐仅手动可选）：

1. `SUPPORTED_ANALYSIS_TYPES` 新增类型名（建议：`correlation_analysis`、`type_profile`、`outlier_objects`；命名风格与既有 `group_comparison` 保持一致）。
2. `_analysis_artifacts(frame, version, analysis_type, config)` 新增对应分支：
   - 调用计算层（`corelation` / `types` / `outliers`，经 `dag` 的血缘结果）；
   - 构造 `payload_json`（按 §3 形状）；
   - 设置 `chartType` 与完整 `option`（ECharts）。
3. `_analysis_config_validation(version, analysis_type, config)` 新增分支：校验必填配置（如类型感知统计无需配置；相关性分析可选 `columns` 白名单；离群值需 `metric_column` 或全数值列）。
4. `_auto_analysis_plan`：按决策 3 决定是否改动（推荐**不改**）。
5. 若要让 Copilot 也能调用：在 `infrastructure/llm/deepseek.py` 的 `ALLOWED_TOOL_NAMES`（**实测 12 个**：8 只读 + 4 审批占位）+ `build_default_tool_registry` 中新增只读工具。已核实 `tests/test_ai_boundary.py` **未**锁定该集合（grep 无命中），但仍须确认新增工具在 `test_ai_boundary.py` / `test_stage_drafts.py` 下不破坏既有断言。
6. 新增端点才需同步 `tests/test_route_manifest.py` 的冻结清单——**优先不新增端点**，复用 `POST /analysis-runs`。

### 6. 报告链路携带新口径（可选但推荐）

- `services/auto_report.py:_compute_report_aggregates` 在 `metrics` 条目上追加类型标签与离群值摘要（**有值才带键**，保持无标签/无离群时输出与改造前逐字节一致——这是既有约定，见 batch 21 的做法）。
- `analytics/digest.py` 可选新增发现类型（如「已排除 N 对伪相关」），须保持 `DIGEST_MAX_FINDINGS = 12` 与既有排序规则。

### 7. 前端图表渲染

- `apps/web/lib/chartOption.ts` 的 `toChartOption` 目前支持 `funnel` / `retention` / `line_with_anomalies` / `bar` / `line`（`switch (payload.chartType)`）；**已核实未知类型返回 `null`，调用方（`app/(workspace)/page.tsx:207`）会优雅回退到表格视图**，所以漏加分支不会崩 UI，但新分析类型会看不到图。
- 新分析类型的图表需在此新增分支，并复用既有调色板与动效常量（`PALETTE` / `ANIMATION` / `tooltip()` / `baseGrid()`），**不要新增前端依赖**。
- ⚠️ **注意**：该文件当前有**未提交改动**（一次与 batch 22/27 设计体系冲突的视觉大改，详见 `PROJECT_CONTEXT.md` §9.1）。开工前先与用户确认这批改动是保留还是回退——若后续回退，本节改动会被一起冲掉。

## 注意事项

- **严格遵守分层纪律**：routers 之间禁止互导；services 禁止反向导入 routers；共用 helper 下沉 `services/`。
- **防火墙改动范围最小化**：只允许按决策 2 新增 `pairs` 键。**不得**改动 `_ROW_LIST_KEYS`、`FORBIDDEN_CONTEXT_KEYS`、`_FEEDBACK_CONTENT_KEYS`。
- **禁止模块顶层 `import pandas`**：运行时用 `common._require_pandas()` 懒加载（`analytics/` 包内的模块按既有惯例处理，但 `services/` 必须遵守）。
- 所有新产物必须能通过 `build_ai_context()` + `assert_safe_ai_context()`，且出站内容**不含行级数据**。
- 确定性数字只由 pandas 计算，**不得让 LLM 产出任何统计数字**。
- 每个 AI 调用必须走 `_run_ai_stage()`（不得绕过预算阀门与 AIRun 落账）。
- 不得新增 Python/前端依赖（scipy/sklearn 已在 Phase 0 处理）。
- 新增/修改的模型字段须配 Alembic 迁移（若确需落库新字段；优先复用 `analysis_artifacts.payload_json`，避免加列）。
- 不要提交 git。

## 测试要求

新增 `apps/api/tests/test_enhanced_integration.py`，至少覆盖：

1. **派生列排除**：构造 `渗透率(%)` 与 `周活跃用户` 强相关数据，断言该对**不出现在** `payload["correlations"]` 中，且 `excluded_correlation_pairs > 0`。
2. **显著性与稳健相关**：断言每个相关项带 `pearson_p`；构造含离群点的数据，断言提供 `robust_pearson` 且 `robustness_note` 非空。
3. **类型感知统计**：序数列（如「满意度(1-5)」）输出含 `distribution` 与 `mode`；占比列输出含 `quantiles` 与 `min_value`/`max_value`。
4. **双维度质量回归锁定（关键）**：同一份数据，改造前后的 `assess_quality(...).overall_score` 与 `.status` **逐值相等**；且 `summary_json["dual_quality"]` 存在且含 `data_quality`/`analysis_quality`。
5. **防火墙契约测试（关键）**：把新产物传入 `build_ai_context()`，断言
   - 输出中**不存在**任何行级数据（不含 `row_index` 键、不含逐行列表）；
   - `pairs` 键按决策 2 的预期行为（新增则存活；未新增则被剥离且不报错）；
   - 断言 `assert_safe_ai_context()` 不抛错。
6. **产物形状**：断言产物为列表而非「以列名为键的 dict」（防止回归到会被防火墙丢弃的形状）。
7. **集成落库**：走 `_analysis_artifacts(frame, version, kind, config)` 断言返回的 `payload_json` 形状正确、`chartType`/`option` 存在。

**回归要求（必须全绿）**：

```bash
cd apps/api
.venv/Scripts/python.exe -m pytest tests -q          # 既有 352 passed, 1 xfailed 不得减少
.venv/Scripts/python.exe -m ruff check app tests     # 零告警
```

重点确认以下既有测试不被破坏（它们锁定了自动计划与 digest 行为）：
`tests/test_analysis.py`、`tests/test_group_comparison.py`、`tests/test_digest.py`、`tests/test_compute_v2.py`、`tests/test_ai_boundary.py`、`tests/test_route_manifest.py`、`tests/test_auto_report_split.py`。

**前端检查**：

```bash
cd apps/web
npm run typecheck
npm run lint
```

## 完成标准

- [ ] 三个设计决策均已获得用户明确答复，并在实现中体现。
- [ ] 五项能力在阶段 4 可**手动**跑通并落库真实 `AnalysisArtifact`（`payload_json` 形状符合 §3）。
- [ ] 派生列两套定义已合并，`source=extracted` 为权威来源。
- [ ] EDA 只有**一条**相关性通路，`payload["correlations"]` 的 `{left,right,correlation}` 形状未变。
- [ ] `assess_quality` 的 `overall_score`/`status` 逐值回归一致；双维度仅在 `summary_json["dual_quality"]`。
- [ ] 新增 `tests/test_enhanced_integration.py` 全绿；`pytest tests -q` 用例数**净增**且无回归。
- [ ] `ruff check app tests` 零告警；前端 `typecheck` + `lint` 通过。
- [ ] **真 key 冒烟一次**：报告叙述中出现的相关系数带 p 值/稳健说明，且出站内容**不含任何行级数据**（可查 `AIRun.input_summary_json` 佐证）。
- [ ] 阶段 5.5 报告与阶段 11 PRD 生成能引用新发现（digest 命中新类型）。
- [ ] `git status` 无临时文件；`git diff` 未触碰 `_ROW_LIST_KEYS`/`FORBIDDEN_CONTEXT_KEYS`/`_FEEDBACK_CONTENT_KEYS`。
