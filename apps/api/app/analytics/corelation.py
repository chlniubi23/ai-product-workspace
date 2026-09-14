"""Enhanced Correlation Analysis - 增强型相关性分析.

核心改进：
1. 每个相关系数输出 p 值（显著性检验）
2. 提供剔除离群点后的"稳健相关系数"
3. 自动排除派生列对避免伪相关
4. 支持多种相关方法（Pearson, Spearman, Kendall）
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .dag import ColumnLineage, should_exclude_from_correlation

#: ``excluded_correlation_pairs_detail`` 的出站上限。明细是给**人工审计**用的
#: （键不在防火墙白名单里，出站即被丢弃），因此只留一个可读的有界样本。
EXCLUDED_PAIRS_DETAIL_LIMIT = 20


@dataclass
class CorrelationResult:
    """单对变量相关性结果."""
    var1: str
    var2: str
    pearson_r: float  # Pearson 相关系数
    pearson_p: float  # p 值（显著性）
    spearman_rho: float  # Spearman 秩相关
    spearman_p: float  # p 值
    
    # 鲁棒性指标
    robust_pearson: float  # 剔除离群点后 Pearson
    robust_spearman: float  # 剔除离群点后 Spearman
    # 解释信息（无默认值，必须在带默认值字段之前）
    interpretation: str  # 人工可读的解释
    robustness_note: str | None = None  # 鲁棒性说明
    
    @property
    def is_significant(self) -> bool:
        """在 0.05 水平上是否显著."""
        return self.pearson_p < 0.05
    
    @property
    def correlation_strength(self) -> str:
        """相关强度分类."""
        r = abs(self.pearson_r)
        if r >= 0.8:
            return "强相关"
        elif r >= 0.5:
            return "中等相关"
        elif r >= 0.3:
            return "弱相关"
        else:
            return "极弱/无相关"
    
    def to_dict(self) -> dict:
        """转换为字典输出."""
        return {
            'var1': self.var1,
            'var2': self.var2,
            'pearson_r': round(self.pearson_r, 4),
            'pearson_p': round(self.pearson_p, 6),
            'spearman_rho': round(self.spearman_rho, 4),
            'spearman_p': round(self.spearman_p, 6),
            'robust_pearson': round(self.robust_pearson, 4),
            'robust_spearman': round(self.robust_spearman, 4),
            'robustness_note': self.robustness_note,
            'is_significant': self.is_significant,
            'correlation_strength': self.correlation_strength,
            'interpretation': self.interpretation
        }


def compute_correlation_with_tests(
    df: pd.DataFrame,
    col1: str,
    col2: str,
    outliers_map: dict | None = None
) -> CorrelationResult:
    """
    计算双变量相关性（带显著性检验与鲁棒性分析）.
    
    Args:
        df: DataFrame
        col1: 列 1
        col2: 列 2
        outliers_map: 离群值映射（用于鲁棒性计算）
        
    Returns:
        CorrelationResult
    """
    # 提取两列的数值数据并处理缺失值
    data = df[[col1, col2]].dropna()
    
    if len(data) < 10:
        # 样本太少无法可靠计算
        return CorrelationResult(
            var1=col1,
            var2=col2,
            pearson_r=np.nan,
            pearson_p=np.nan,
            spearman_rho=np.nan,
            spearman_p=np.nan,
            robust_pearson=np.nan,
            robust_spearman=np.nan,
            robustness_note="样本量不足（<10）",
            interpretation="数据量不足，无法进行可靠性评估"
        )
    
    # 基础 Pearson 相关系数
    pearson_r, pearson_p = stats.pearsonr(data[col1], data[col2])
    
    # Spearman 秩相关
    spearman_rho, spearman_p = stats.spearmanr(data[col1], data[col2])
    
    # 计算离群点修正版本
    robust_pearson = pearson_r
    robust_spearman = spearman_rho
    robustness_note = None
    
    if outliers_map and col1 in outliers_map and col2 in outliers_map:
        all_outliers = set()
        
        # 收集两个列的所有离群点索引
        for outlier_list in [outliers_map.get(col1, []), outliers_map.get(col2, [])]:
            if isinstance(outlier_list, list):
                for out in outlier_list:
                    all_outliers.add(int(out['row_index']))
        
        if all_outliers:
            # 剔除离群点后重新计算
            clean_data = data.loc[~data.index.isin(all_outliers)]
            
            if len(clean_data) >= 5:  # 确保仍有足够样本
                robust_pearson, _ = stats.pearsonr(clean_data[col1], clean_data[col2])
                robust_spearman, _ = stats.spearmanr(clean_data[col1], clean_data[col2])
                
                # 稳健性说明：只要重算了就输出差异，让 LLM 能判断相关是否被离群点扭曲
                diff_pearson = abs(pearson_r - robust_pearson)
                if diff_pearson > 0.2:
                    robustness_note = (
                        f"剔除{len(all_outliers)}个离群点后，"
                        f"Pearson 从 {pearson_r:.3f} 变为 {robust_pearson:.3f} "
                        f"(差异={diff_pearson:.3f}，相关性受离群点显著影响)"
                    )
                else:
                    robustness_note = (
                        f"剔除{len(all_outliers)}个离群点后，"
                        f"Pearson 从 {pearson_r:.3f} 变为 {robust_pearson:.3f} "
                        f"(差异={diff_pearson:.3f}，相关性稳健)"
                    )
    
    # 生成解释文本
    strength = abs(pearson_r) if not np.isnan(pearson_r) else 0
    direction = "正相关" if pearson_r > 0 else "负相关"
    significant_text = "统计显著 (p<0.05)" if pearson_p < 0.05 else "不显著 (p≥0.05)"
    
    interpretation = (
        f"{col1}与{col2}: {direction} ({strength:.3f}), "
        f"{significant_text}"
    )
    
    return CorrelationResult(
        var1=col1,
        var2=col2,
        pearson_r=pearson_r,
        pearson_p=pearson_p,
        spearman_rho=spearman_rho,
        spearman_p=spearman_p,
        robust_pearson=robust_pearson,
        robust_spearman=robust_spearman,
        robustness_note=robustness_note,
        interpretation=interpretation
    )


def compute_full_correlation_matrix(
    df: pd.DataFrame,
    numeric_columns: list[str] | None = None,
    lineage_map: dict[str, ColumnLineage] | None = None,
    outliers_map: dict[str, list] | None = None,
    mechanical_r_threshold: float = 0.98,
) -> tuple[pd.DataFrame, list[CorrelationResult], list[dict], list[dict]]:
    """
    计算完整的相关矩阵（排除伪相关对）.

    Args:
        df: DataFrame
        numeric_columns: 需要分析的数值列列表
        lineage_map: 列血缘映射（用于排除派生列）
        outliers_map: 离群值映射（用于稳健相关估计）
        mechanical_r_threshold: 机械相关反向推导阈值（|r|>=该值视为派生）

    Returns:
        (相关矩阵 DataFrame, CorrelationResult 列表, 被排除的机械相关对列表,
         被血缘判定排除的列对明细 ``[{'var1','var2','reason'}]``)

    排除总数 = ``len(机械相关对) + len(血缘排除对)``。两类明细同形，调用方可直接
    拼接成 ``excluded_correlation_pairs_detail`` 供人工审计（出站时会被防火墙按
    未白名单列表丢弃 —— 这是刻意为之，见 :mod:`app.ai_context`）。
    """
    # 如果没有指定列，自动选择数值列
    if numeric_columns is None:
        numeric_columns = [
            col for col in df.columns
            if pd.api.types.is_numeric_dtype(df[col])
        ]

    results = []
    valid_pairs = []
    mechanical_excluded: list[dict] = []
    excluded_derived_pairs: list[dict] = []

    # 获取所有可能的列对
    for i, col1 in enumerate(numeric_columns):
        for col2 in numeric_columns[i+1:]:
            # 检查是否需要排除（派生列关系）
            if lineage_map:
                should_exclude, explanation = should_exclude_from_correlation(
                    col1, col2, lineage_map
                )
                if should_exclude:
                    excluded_derived_pairs.append({
                        'var1': col1,
                        'var2': col2,
                        'reason': explanation or '存在派生/血缘关系，已从相关性中排除',
                    })
                    continue  # 跳过这对

            result = compute_correlation_with_tests(df, col1, col2, outliers_map or {})

            # 统计反向推导（命名推断的兑底）：近乎完美的线性相关 |r|>=0.98
            # 多半是机械派生（如 C=A+B、B=A+10、渗透率=周活/总量），无业务意义，
            # 从发现列表中排除并记录，避免 LLM 把数学必然当成业务洞察。
            r_val = result.pearson_r
            if not np.isnan(r_val) and abs(r_val) >= mechanical_r_threshold:
                mechanical_excluded.append({
                    'var1': col1,
                    'var2': col2,
                    'pearson_r': round(float(r_val), 4),
                    'reason': f'|r|={abs(r_val):.3f} 接近 1，疑似派生/机械相关，已排除',
                })
                continue

            results.append(result)
            valid_pairs.append((col1, col2))

    # 构建相关矩阵
    matrix_df = df[numeric_columns].corr(method='pearson')

    return matrix_df, results, mechanical_excluded, excluded_derived_pairs
