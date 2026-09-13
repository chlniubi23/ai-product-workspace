"""Dependency Analysis Graph - 派生列血缘识别与排除模块.

核心功能：
1. 识别派生关系（如渗透率=周活/总周活×100）
2. 自动从相关性分析中排除派生列对
3. 输出派生说明供 LLM 引用
4. 防止伪相关发现（机械相关）

设计原则：
- 保留向后兼容的 API 接口
- 不破坏现有计算流程
- 所有修复点可单独启用/禁用
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass
class DerivedColumn:
    """派生列定义."""
    name: str  # 列名
    source_columns: list[str]  # 源列列表
    operation: str  # 操作类型 ('ratio', 'sum', 'product', 'custom')
    formula: str  # 计算公式
    description: str  # 业务说明


@dataclass 
class ColumnLineage:
    """列血缘信息."""
    column_name: str
    is_derived: bool = False
    derived_from: list[str] | None = None
    derivation_type: str | None = None
    derivation_formula: str | None = None
    derivation_description: str | None = None
    
    @property
    def lineage_summary(self) -> str:
        """生成可用于报告的血缘摘要."""
        if not self.is_derived or not self.derived_from:
            return "原始数据列"
        
        sources = ", ".join(self.derived_from)
        return f"派生列（来源：{sources}，操作：{self.derivation_type}）"


# 📌 内置派生规则库（可扩展）
DERIVED_COLUMN_RULES = [
    # 比率类派生列
    DerivedColumn(
        name="渗透率",
        source_columns=["周活跃用户", "总用户数"],
        operation="ratio",
        formula="(周活跃用户 / 总用户数) * 100",
        description="表示使用该功能的活跃用户占总用户的比例"
    ),
    DerivedColumn(
        name="ARPU", 
        source_columns=["总收入", "DAU"],
        operation="ratio",
        formula="收入 / DAU",
        description="每日平均用户收入"
    ),
    DerivedColumn(
        name="ARPPU",
        source_columns=["总收入", "付费用户数"],
        operation="ratio",
        formula="收入 / 付费用户数", 
        description="平均每付费用户收入"
    ),
    # 差值类
    DerivedColumn(
        name="增长量",
        source_columns=["当前值", "上期值"],
        operation="difference",
        formula="当前值 - 上期值",
        description="较上期的净增长量"
    ),
    DerivedColumn(
        name="环比变化 (%)",
        source_columns=["当前值", "上期值"],
        operation="growth_rate",
        formula="(当前值 - 上期值) / 上期值 * 100",
        description="较上期的增长率百分比"
    ),
]


def detect_derived_columns(df_columns: list[str]) -> dict[str, ColumnLineage]:
    """
    自动检测数据集内的派生列关系.
    
    策略：
    1. 匹配内置规则
    2. 基于列名模式推断
    3. 统计相关性异常高值反向推导
    
    Args:
        df_columns: DataFrame 中的列名列表
        
    Returns:
        列名 -> ColumnLineage 映射表
    """
    lineage_map = {}
    
    for col in df_columns:
        lineage = ColumnLineage(column_name=col)
        
        # 策略 1: 匹配内置规则
        matched = _match_builtin_rules(col, df_columns)
        if matched:
            lineage.is_derived = True
            lineage.derived_from = list(matched.source_columns)
            lineage.derivation_type = matched.operation
            lineage.derivation_formula = matched.formula
            lineage.derivation_description = matched.description
            lineage_map[col] = lineage
            continue
            
        # 策略 2: 基于命名模式推断
        pattern_match = _infer_from_naming_pattern(col, df_columns)
        if pattern_match:
            lineage.is_derived = True
            lineage.derived_from = pattern_match['source_columns']
            lineage.derivation_type = pattern_match['operation']
            lineage.derivation_formula = pattern_match['formula']
            lineage_map[col] = lineage
            continue
            
        # 否则标记为原始列
        lineage_map[col] = lineage
        
    return lineage_map


def build_lineage_map(
    df_columns: Sequence[str],
    extracted_columns: Iterable[str] | None = None,
) -> dict[str, ColumnLineage]:
    """合并「派生列」的两套判定，持久化的抽取记录为权威来源。

    生产侧已有一个权威定义：``DataColumn.source == "extracted"``（batch 14，列名
    约定 ``{源列}__{指标}``，由 dataset parse 写入），它记录的是**真实发生过的**
    文本指标抽取。:func:`detect_derived_columns` 只是**列名模式识别**，会漏判
    （例如英文 ``__`` 抽取列）也会误判（例如业务上无关但名字带「率」的列）。

    因此这里取**并集**，但以抽取记录为准：

    * 抽取列一律标记为派生列，即使模式识别没命中；
    * 模式识别命中的其他列照常保留；
    * 两者都命中时保留模式识别的推导信息（更具体），只把 ``is_derived`` 置真。

    Args:
        df_columns: DataFrame 中的列名列表
        extracted_columns: ``DataColumn.source == "extracted"`` 的列名集合

    Returns:
        列名 -> ColumnLineage 映射表（与 :func:`detect_derived_columns` 同形状）
    """

    columns = [str(col) for col in df_columns]
    lineage_map = detect_derived_columns(columns)
    extracted = {str(name) for name in (extracted_columns or ())}

    for name in columns:
        if name not in extracted:
            continue
        lineage = lineage_map.get(name) or ColumnLineage(column_name=name)
        if not lineage.is_derived:
            # ``{源列}__{指标}`` 是抽取列的命名约定，源列仍在数据集里时可直接引用，
            # 这样 should_exclude_from_correlation 的「一列派生自另一列」分支能命中。
            prefix = name.split("__", 1)[0] if "__" in name else ""
            lineage.is_derived = True
            lineage.derived_from = [prefix] if prefix and prefix != name and prefix in columns else []
            lineage.derivation_type = "extracted"
            lineage.derivation_description = "由上传数据的文本指标抽取生成（DataColumn.source=extracted）"
        lineage_map[name] = lineage

    return lineage_map


def _match_builtin_rules(target_col: str, all_cols: list[str]) -> DerivedColumn | None:
    """尝试匹配内置派生规则."""
    target_lower = target_col.lower()
    
    for rule in DERIVED_COLUMN_RULES:
        # 检查目标列是否匹配
        if target_col == rule.name or target_lower in [c.lower() for c in rule.source_columns]:
            # 验证源列是否都在数据集中
            source_found = all(
                any(rule_src.lower() in col.lower() for col in all_cols)
                for rule_src in rule.source_columns
            )
            if source_found:
                return rule
                
    return None


def _infer_from_naming_pattern(target_col: str, all_cols: list[str]) -> dict | None:
    """基于列名模式推断派生关系.
    
    策略 A（括号引用）：如「渗透率(% 占周活)」括号内的「占周活」直接引用
    了「周活跃用户」列 —— 该列必为其派生列（分子），排除伪相关。
    策略 B（变化率前缀）：如「较上周活跃变化(%)」= 对「周活跃用户」的环比。
    策略 C（通用率/百分比命名 + 候选源列）。
    """
    
    # --- 策略 A: 括号内的「占X / X占比 / 较X」引用 ---
    paren_match = re.search(r'[（(]([^）)]*)[）)]', target_col)
    if paren_match:
        hint = paren_match.group(1)
        ref_tokens = []
        for m in re.finditer(r'占([\w\u4e00-\u9fff]+)|较([\w\u4e00-\u9fff]+)', hint):
            token = m.group(1) or m.group(2)
            if token:
                ref_tokens.append(token)
        candidates = set()
        for token in ref_tokens:
            for col in all_cols:
                if col != target_col and token and token in col:
                    candidates.add(col)
        if candidates:
            op = 'growth_rate' if ('变化' in target_col or '增' in target_col) else 'ratio'
            return {
                'source_columns': sorted(candidates),
                'operation': op,
                'formula': f'derived from: {sorted(candidates)}',
                'description': f'列名括号引用提示：与 {", ".join(sorted(candidates))} 存在派生关系，相关性为机械必然'
            }
    
    # --- 策略 B: 变化率前缀（较上周X变化 / X环比增长） ---
    if re.search(r'(变化|增减|增长|环比|同比)', target_col):
        # 循环剥离前缀组合（如「较上周」「较上期同比」）与后缀
        core = target_col
        prefix_re = re.compile(r'^(较|上周|本周|同比|环比|上月|上季度|本期|较上期)')
        while True:
            stripped = prefix_re.sub('', core)
            if stripped == core:
                break
            core = stripped
        core = re.sub(r'(变化|增减|增长|比率|\(|\)|%|（|）)+$', '', core).strip()
        if core:
            candidates = [col for col in all_cols if col != target_col and core in col]
            if candidates:
                return {
                    'source_columns': sorted(candidates),
                    'operation': 'growth_rate',
                    'formula': f'(current - previous) / previous * 100 over {sorted(candidates)}',
                    'description': f'变化率指标：基于 {", ".join(sorted(candidates))} 计算，存在机械相关'
                }
    
    # --- 策略 C: 通用「率/%」命名 + 名称包含其他列名片段 ---
    if re.search(r'(率|%)', target_col):
        # 提取列名中 2 字以上的中文片段，看是否出现在其他列名中
        tokens = re.findall(r'[\u4e00-\u9fff]{2,}', re.sub(r'[（(].*?[）)]', '', target_col))
        candidates = set()
        for token in tokens:
            for col in all_cols:
                if col != target_col and token in col:
                    candidates.add(col)
        if candidates:
            return {
                'source_columns': sorted(candidates),
                'operation': 'ratio',
                'formula': f'derived from: {sorted(candidates)}',
                'description': f'比率命名提示：与 {", ".join(sorted(candidates))} 存在派生关系'
            }
                
    return None


def should_exclude_from_correlation(col1: str, col2: str, lineage_map: dict[str, ColumnLineage]) -> tuple[bool, str | None]:
    """
    判断一对列是否应该从相关性分析中排除（避免伪相关）.
    
    Args:
        col1: 列 1
        col2: 列 2  
        lineage_map: 列血缘映射
        
    Returns:
        (should_exclude, explanation)
    """
    lin1 = lineage_map.get(col1)
    lin2 = lineage_map.get(col2)
    
    # 情况 1: 两个都是原始列且无派生关系 -> 不排除
    if not lin1 or not lin2 or (not lin1.is_derived and not lin2.is_derived):
        return False, None
        
    # 情况 2: 其中一个是由另一个派生的 -> 必须排除
    if lin1.is_derived and lin2.column_name in (lin1.derived_from or []):
        return True, f"{col1}是{col2}的派生列（{lin1.derivation_description}），存在必然相关性"
    
    if lin2.is_derived and lin1.column_name in (lin2.derived_from or []):
        return True, f"{col2}是{col1}的派生列（{lin2.derivation_description}），存在必然相关性"
        
    # 情况 3: 两个都源自同一个源列 -> 可能产生机械相关，建议排除
    if lin1.is_derived and lin2.is_derived:
        sources1 = set(lin1.derived_from or [])
        sources2 = set(lin2.derived_from or [])
        common = sources1.intersection(sources2)
        if common:
            return True, f"{col1}和{col2}都源于{', '.join(common)}，可能产生机械相关"
    
    return False, None


def get_derivation_explanations(lineage_map: dict[str, ColumnLineage], columns: list[str]) -> list[ColumnLineage]:
    """获取指定列的血缘说明（用于报告）."""
    explanations = []
    for col in columns:
        if col in lineage_map and lineage_map[col].is_derived:
            explanations.append(lineage_map[col])
    return explanations
