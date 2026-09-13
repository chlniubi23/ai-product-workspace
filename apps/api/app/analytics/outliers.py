"""Enhanced Outlier Detection - 升级版离群值检测.

核心改进：
1. 每个离群值输出完整的对象级信息（哪一行、什么值）
2. 支持多种检测方法（IQR、Z-score、LOF）
3. 提供可被 LLM 直接引用的描述格式

格式示例：
{
    "column": "周活跃用户",
    "outlier_type": "iqr_upper",
    "row_index": 5,
    "value": 2950000,
    "threshold": 2800000,
    "description": "AI 对话 | 周活跃用户=2950000 | 判定为 IQR 上界离群值（Q3+1.5×IQR=2800000）"
}
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class OutlierInfo:
    """单个离群值的信息."""
    column_name: str
    row_index: int  # 行索引
    value: float  # 实际值
    outlier_type: str  # 'iqr_upper', 'iqr_lower', 'zscore_high', 'zscore_low', 'lof'
    threshold: float  # 阈值
    is_upper: bool = True  # 是否上界离群
    
    @property
    def description(self) -> str:
        """生成人类可读的描述（LLM 引用格式）."""
        if self.outlier_type.startswith('iqr'):
            direction = "上界" if self.is_upper else "下界"
            return (
                f"{self.column_name}[{self.row_index}] 的值 {self.value:,.0f} "
                f"是 {direction}离群值（{direction}阈值={self.threshold:,.0f}）"
            )
        elif self.outlier_type.startswith('zscore'):
            direction = "正方向" if self.is_upper else "负方向"
            return (
                f"{self.column_name}[{self.row_index}] 的值 {self.value:,.0f} "
                f"偏离均值超过 3σ（{direction}异常）"
            )
        return f"{self.column_name}[{self.row_index}]: {self.value}"
    
    def to_dict(self) -> dict:
        """转换为字典输出."""
        return {
            'column': self.column_name,
            'row_index': self.row_index,
            'value': self.value,
            'outlier_type': self.outlier_type,
            'threshold': self.threshold,
            'is_upper': self.is_upper,
            'description': self.description
        }


def detect_outliers_iqr(
    df: pd.DataFrame, 
    columns: list[str]
) -> dict[str, list[OutlierInfo]]:
    """
    基于 IQR 方法的离群值检测.
    
    Args:
        df: DataFrame
        columns: 需要检测的数值列
        
    Returns:
        列名 -> 离群值列表映射
    """
    outliers_map = {}
    
    for col in columns:
        if col not in df.columns or not pd.api.types.is_numeric_dtype(df[col]):
            continue
            
        series = df[col].dropna()
        
        Q1 = series.quantile(0.25)
        Q3 = series.quantile(0.75)
        IQR = Q3 - Q1
        
        lower_bound = Q1 - 1.5 * IQR
        upper_bound = Q3 + 1.5 * IQR
        
        out_list = []
        
        # 检测上界离群值
        upper_mask = df[col] > upper_bound
        for idx in df[upper_mask].index:
            out_list.append(OutlierInfo(
                column_name=col,
                row_index=int(idx),
                value=float(df.loc[idx, col]),
                outlier_type='iqr_upper',
                threshold=float(upper_bound),
                is_upper=True
            ))
        
        # 检测下界离群值
        lower_mask = df[col] < lower_bound
        for idx in df[lower_mask].index:
            out_list.append(OutlierInfo(
                column_name=col,
                row_index=int(idx),
                value=float(df.loc[idx, col]),
                outlier_type='iqr_lower',
                threshold=float(lower_bound),
                is_upper=False
            ))
            
        outliers_map[col] = out_list
        
    return outliers_map


def detect_outliers_zscore(
    df: pd.DataFrame,
    columns: list[str],
    threshold: float = 3.0
) -> dict[str, list[OutlierInfo]]:
    """
    基于 Z-score 的离群值检测.
    
    Args:
        df: DataFrame
        columns: 需要检测的数值列
        threshold: 标准差倍数阈值（默认 3）
    """
    outliers_map = {}
    
    for col in columns:
        if col not in df.columns or not pd.api.types.is_numeric_dtype(df[col]):
            continue
            
        series = df[col].dropna()
        mean = series.mean()
        std = series.std()
        
        if std == 0 or np.isnan(std):
            continue
            
        z_scores = np.abs((series - mean) / std)
        
        out_list = []
        
        for idx, z in z_scores.items():
            if z > threshold:
                is_upper = df.loc[idx, col] > mean
                out_list.append(OutlierInfo(
                    column_name=col,
                    row_index=int(idx),
                    value=float(df.loc[idx, col]),
                    outlier_type='zscore_high' if is_upper else 'zscore_low',
                    threshold=float(mean + threshold * std if is_upper else mean - threshold * std),
                    is_upper=is_upper
                ))
        
        outliers_map[col] = out_list
        
    return outliers_map


def detect_outliers_lof(
    df: pd.DataFrame,
    columns: list[str],
    n_neighbors: int = 20,
    contamination: float = 0.1
) -> dict[str, list[OutlierInfo]]:
    """
    基于局部离群因子（Local Outlier Factor）的检测.
    
    Args:
        df: DataFrame
        columns: 需要检测的数值列
        n_neighbors: LOF 邻域大小
        contamination: 预估污染比例
    """
    # scikit-learn 是可选依赖（pyproject 的 `ml` extra），不进必需依赖：
    # 未安装时按空结果降级，与下方 except Exception 的语义保持一致。
    try:
        from sklearn.neighbors import LocalOutlierFactor
    except ImportError:
        return {col: [] for col in columns}

    outliers_map = {}
    
    # 只使用数值列进行 LOF 计算
    numeric_cols = [c for c in columns if pd.api.types.is_numeric_dtype(df[c])]
    
    if len(numeric_cols) < 2:
        return {col: [] for col in columns}
    
    X = df[numeric_cols].dropna()
    
    if len(X) < n_neighbors + 1:
        return {col: [] for col in columns}
    
    try:
        lof = LocalOutlierFactor(
            n_neighbors=n_neighbors,
            contamination=contamination
        )
        pred = lof.fit_predict(X)
        scores = -lof.negative_outlier_factor_  # 越大越异常
        
        for col in columns:
            if col not in numeric_cols:
                outliers_map[col] = []
                continue
                
            out_list = []
            # X.index 与 scores 均由同一次 fit_predict(X) 派生，长度必然一致。
            for idx, score in zip(X.index, scores, strict=True):
                if pred[int(idx)] == -1:  # 离群点
                    out_list.append(OutlierInfo(
                        column_name=col,
                        row_index=int(idx),
                        value=float(df.loc[idx, col]),
                        outlier_type='lof',
                        threshold=float(score),
                        is_upper=True
                    ))
            
            outliers_map[col] = out_list
            
    except Exception:
        # LOF 失败则回退到空列表
        outliers_map = {col: [] for col in columns}
        
    return outliers_map


def aggregate_outliers_info(all_outliers: dict) -> dict[str, any]:
    """
    聚合所有离群值信息用于报告.
    
    Returns:
        {
            "total_count": int,
            "by_column": {col: [OutlierInfo.to_dict()]}
        }
    """
    total = sum(len(v) for v in all_outliers.values())
    
    by_column = {}
    for col, outliers in all_outliers.items():
        if outliers:
            by_column[col] = [o.to_dict() for o in outliers]
    
    return {
        "total_count": total,
        "by_column": by_column,
        "has_outliers": total > 0
    }


#: 每列最多出站的极值个数。离群值摘要只带**有界**的数值样本，绝不带行身份。
OUTLIER_VALUE_SAMPLE_LIMIT = 5


def build_raw_outliers_map(df: pd.DataFrame, columns: list[str] | None = None) -> dict[str, list[dict]]:
    """IQR + Z-score 合并后的**行级**离群点映射（仅限进程内使用）。

    形状刻意与 ``corelation.compute_correlation_with_tests`` 的读取方式对齐
    （它按下标 ``out['row_index']`` 取行号），用于计算剔除离群点后的稳健相关系数。

    ⚠️ 返回值含行号，**禁止**直接交给 ``build_ai_context`` —— 出站请用
    :func:`build_outlier_aggregates`。
    """
    target = list(columns) if columns is not None else [
        str(col) for col in df.columns if pd.api.types.is_numeric_dtype(df[col])
    ]
    by_iqr = detect_outliers_iqr(df, target)
    by_zscore = detect_outliers_zscore(df, target)

    raw_map: dict[str, list[dict]] = {}
    for col in target:
        merged = list(by_iqr.get(col) or []) + list(by_zscore.get(col) or [])
        if merged:
            raw_map[str(col)] = [item.to_dict() for item in merged]
    return raw_map


def build_outlier_aggregates(df: pd.DataFrame, columns: list[str] | None = None) -> list[dict]:
    """按列有界聚合的离群值摘要，**不含任何行级数据**。

    这是「对象级离群值」唯一允许出站的形状（Phase 1 决策 1 · 方案 A）：保留
    「AI 能引用具体数值」的价值（范围 + 有界极值），但不暴露行身份，因此防火墙
    的 ``_ROW_LIST_KEYS`` 无需放宽。

    极值样本放在 ``series`` 键下，而不是字面意义上的 ``top_values``：防火墙只放行
    ``_AGGREGATE_LIST_KEYS`` 里的键，其它键下的列表会被静默丢弃（``series`` 是其中
    语义最贴近的允许键）。

    Returns:
        ``[{name, method, count, rate, direction, min_value, max_value, series}, ...]``
    """
    target = list(columns) if columns is not None else [
        str(col) for col in df.columns if pd.api.types.is_numeric_dtype(df[col])
    ]
    by_iqr = detect_outliers_iqr(df, target)
    by_zscore = detect_outliers_zscore(df, target)

    aggregates: list[dict] = []
    for col in target:
        merged = list(by_iqr.get(col) or []) + list(by_zscore.get(col) or [])
        if not merged:
            continue

        valid = df[col].dropna() if col in df.columns else pd.Series(dtype="float64")
        sample_count = int(len(valid))
        values = [float(item.value) for item in merged]
        directions = {item.outlier_type for item in merged}
        if directions <= {"iqr_upper", "zscore_high", "lof"}:
            direction = "upper"
        elif directions <= {"iqr_lower", "zscore_low"}:
            direction = "lower"
        else:
            direction = "both"

        aggregates.append({
            "name": str(col),
            "method": "iqr_and_zscore",
            "count": len(merged),
            "rate": round(len(merged) / sample_count, 4) if sample_count else 0.0,
            "sample_count": sample_count,
            "direction": direction,
            "min_value": round(min(values), 4),
            "max_value": round(max(values), 4),
            "series": [round(value, 4) for value in sorted(values, key=abs, reverse=True)[:OUTLIER_VALUE_SAMPLE_LIMIT]],
        })

    return aggregates
