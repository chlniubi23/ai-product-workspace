"""Enhanced Analysis Engine - 增强型分析引擎.

整合所有修复点：
1. 派生列血缘识别与排除
2. 对象级离群值检测
3. 显著性检验 + 稳健相关系数
4. 类型感知统计（序数/占比列）
5. 数据质量 + 分析质量双维度评估
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .corelation import compute_full_correlation_matrix
from .dag import ColumnLineage, detect_derived_columns
from .outliers import aggregate_outliers_info, detect_outliers_iqr, detect_outliers_zscore
from .quality import generate_quality_report
from .types import compute_type_aware_stats


class EnhancedAnalysisEngine:
    """增强型分析引擎."""
    
    def __init__(self, df: pd.DataFrame):
        """
        Args:
            df: DataFrame，需包含数值和分类列
        """
        self.df = df
        self.column_count = len(df.columns)
        self.row_count = len(df)
        
        # 衍生属性
        self.lineage_map: dict[str, ColumnLineage] = {}
        self.outliers_result: dict = {}
        self.correlation_results: list = []
        self.type_stats: dict = {}
        self.quality_report: dict = {}
    
    def run_comprehensive_analysis(self) -> dict[str, Any]:
        """
        执行完整的增强型分析.
        
        Returns:
            {
                'dataset_summary': {...},
                'column_lineage': {...},
                'outlier_detection': {...},
                'correlation_analysis': {...},
                'type_aware_statistics': {...},
                'quality_assessment': {...}
            }
        """
        results = {}
        
        # 1. 识别派生列血缘
        self._compute_lineage()
        results['column_lineage'] = self._format_lineage_output()
        
        # 2. 对象级离群值检测
        self._detect_outliers()
        results['outlier_detection'] = self.outliers_result
        
        # 3. 增强相关性分析
        self._compute_correlations()
        results['correlation_analysis'] = self._format_correlations(results)
        
        # 4. 类型感知统计
        self._compute_type_stats()
        results['type_aware_statistics'] = self.type_stats
        
        # 5. 质量评估（分析质量基于类型感知统计口径，而非列血缘）
        quality_report = generate_quality_report(
            self.df,
            self.outliers_result,
            self.correlation_results,
            self.type_stats,
        )
        results['quality_assessment'] = quality_report.to_dict()
        
        return results
    
    def _compute_lineage(self):
        """识别派生列."""
        cols = list(self.df.columns)
        self.lineage_map = detect_derived_columns(cols)
    
    def _format_lineage_output(self) -> dict:
        """格式化列血缘输出."""
        output = {}
        for col, lineage in self.lineage_map.items():
            info = {
                'is_derived': lineage.is_derived,
                'lineage_summary': lineage.lineage_summary
            }
            if lineage.is_derived:
                info.update({
                    'derived_from': lineage.derived_from,
                    'derivation_type': lineage.derivation_type,
                    'derivation_formula': lineage.derivation_formula,
                    'derivation_description': lineage.derivation_description
                })
            output[col] = info
        return output
    
    def _detect_outliers(self):
        """执行对象级离群值检测."""
        numeric_cols = [
            col for col in self.df.columns 
            if pd.api.types.is_numeric_dtype(self.df[col])
        ]
        
        outliers_by_iqr = detect_outliers_iqr(self.df, numeric_cols)
        outliers_by_zscore = detect_outliers_zscore(self.df, numeric_cols)
        
        # 合并结果
        combined = {}
        raw_map: dict[str, list] = {}
        for col in numeric_cols:
            all_outliers = []
            if col in outliers_by_iqr:
                all_outliers.extend(outliers_by_iqr[col])
            if col in outliers_by_zscore:
                all_outliers.extend(outliers_by_zscore[col])
            
            if all_outliers:
                combined[col] = all_outliers
                # 以 dict 形式保存原始离群点，供相关性稳健估计引用行号
                raw_map[col] = [o.to_dict() for o in all_outliers]
        
        self._raw_outliers_map = raw_map
        self.outliers_result = aggregate_outliers_info(combined)
    
    def _compute_correlations(self):
        """执行相关性分析."""
        numeric_cols = [
            col for col in self.df.columns 
            if pd.api.types.is_numeric_dtype(self.df[col])
        ]
        
        matrix_df, results, mechanical_excluded, excluded_derived_pairs = compute_full_correlation_matrix(
            self.df,
            numeric_cols,
            self.lineage_map,
            outliers_map=getattr(self, "_raw_outliers_map", {}),
        )
        
        self.correlation_results = results
        self.mechanical_excluded = mechanical_excluded
        self.excluded_derived_pairs = excluded_derived_pairs
        
        # 保存矩阵为字典格式
        self.correlation_matrix = matrix_df.to_dict()
    
    def _format_correlations(self, context_results: dict) -> dict:
        """格式化相关性结果."""
        formatted = []
        for result in self.correlation_results:
            item = result.to_dict()
            
            # 添加上下文信息
            formatted.append(item)
        
        return {
            'pairwise_correlations': formatted,
            'mechanical_excluded': getattr(self, 'mechanical_excluded', []),
            'summary': {
                'n_pairs_analyzed': len(formatted),
                'n_significant': sum(1 for r in formatted if r['is_significant']),
                'has_robust_analysis': any(r.get('robustness_note') for r in formatted),
                'excluded_derived_pairs': self._count_excluded_pairs(),
                'excluded_mechanical_pairs': len(getattr(self, 'mechanical_excluded', [])),
            }
        }
    
    def _count_excluded_pairs(self) -> int:
        """统计被排除的派生列对."""
        excluded = 0
        for col1 in self.df.columns:
            for col2 in self.df.columns:
                if col1 < col2:
                    should_exclude, _ = self._should_exclude_pair(col1, col2)
                    if should_exclude:
                        excluded += 1
        return excluded
    
    def _should_exclude_pair(self, col1: str, col2: str) -> tuple[bool, str | None]:
        """检查一对列是否应该排除."""
        from .dag import should_exclude_from_correlation
        return should_exclude_from_correlation(
            col1, col2, self.lineage_map
        )
    
    def _compute_type_stats(self):
        """计算类型感知统计."""
        self.type_stats = compute_type_aware_stats(self.df)
    
    def get_dataset_summary(self) -> dict:
        """数据集基本信息."""
        return {
            'n_rows': self.row_count,
            'n_columns': self.column_count,
            'data_types': {
                col: str(dtype) for col, dtype in self.df.dtypes.items()
            },
            'missing_values': self.df.isna().sum().to_dict(),
            'duplicate_rows': int(self.df.duplicated().sum())
        }


# 📌 用于快速调用的便捷函数
def run_enhanced_analysis(df: pd.DataFrame) -> dict[str, Any]:
    """
    快捷函数：运行完整增强分析.
    
    Args:
        df: DataFrame
        
    Returns:
        完整的分析报告字典
    """
    engine = EnhancedAnalysisEngine(df)
    return engine.run_comprehensive_analysis()


def compare_correlations_with_outliers(
    df: pd.DataFrame,
    col1: str,
    col2: str
) -> dict:
    """
    对比有/无离群点的相关性差异.
    
    Args:
        df: DataFrame
        col1: 列 1
        col2: 列 2
        
    Returns:
        {
            'original_correlation': float,
            'original_p_value': float,
            'robust_correlation': float,
            'robust_p_value': float,
            'outliers_found': int,
            'impact_note': str
        }
    """
    data = df[[col1, col2]].dropna()
    
    # 原始相关性
    from scipy import stats
    orig_r, orig_p = stats.pearsonr(data[col1], data[col2])
    
    # 检测离群点
    outliers_map = detect_outliers_iqr(df, [col1, col2])
    
    outlier_indices = set()
    for col_name in [col1, col2]:
        if col_name in outliers_map:
            for out in outliers_map[col_name]:
                outlier_indices.add(int(out.row_index))
    
    # 剔除后重新计算
    clean_data = data.loc[~data.index.isin(outlier_indices)]
    
    robust_r = np.nan
    robust_p = np.nan
    
    if len(clean_data) >= 5:
        robust_r, robust_p = stats.pearsonr(clean_data[col1], clean_data[col2])
    
    impact_note = f"剔除{len(outlier_indices)}个离群点后，从{orig_r:.3f}变为{robust_r:.3f}"
    
    return {
        'original_correlation': round(orig_r, 4),
        'original_p_value': round(orig_p, 6),
        'robust_correlation': round(robust_r, 4),
        'robust_p_value': round(robust_p, 6) if not np.isnan(robust_p) else None,
        'outliers_found': len(outlier_indices),
        'impact_note': impact_note,
        'interpretation': "如果差异较大，说明原相关性可能被离群点扭曲"
    }
