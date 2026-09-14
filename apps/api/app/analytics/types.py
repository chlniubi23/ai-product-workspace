"""Type-Aware Statistics - 类型感知统计.

核心改进：
1. 自动识别列类型（数值/序数/分类/占比/比率）
2. 根据类型选择合适的统计方法：
   - 序数量表（满意度 1-5）：输出分布 + 众数，避免误导性的均值
   - 占比/比率（渗透率）：输出分位数 + 极值对象，不做跨行平均
   - 数值指标：标准描述统计
3. 为 LLM 提供可引用的具体样本
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

import pandas as pd


class ColumnType(Enum):
    """列类型枚举."""
    NUMERIC = "numeric"
    ORDINAL = "ordinal"  # 序数量表（如满意度）
    CATEGORICAL = "categorical"
    RATIO = "ratio"  # 占比类（如渗透率）
    PERCENTAGE = "percentage"  # 百分比（如变化率）
    BINARY = "binary"  # 二元变量


#: 枚举值 -> 中文标签。单一来源：``ColumnTypeInfo.type_label`` 与下游产物共用。
TYPE_LABELS = {
    "numeric": "数值型",
    "ordinal": "序数量表",
    "categorical": "分类变量",
    "ratio": "占比指标",
    "percentage": "百分比指标",
    "binary": "二元变量",
}


@dataclass
class ColumnTypeInfo:
    """列类型信息."""
    column_name: str
    inferred_type: ColumnType
    dtype: str
    confidence: float
    
    @property
    def type_label(self) -> str:
        """中文标签."""
        return TYPE_LABELS.get(str(self.inferred_type.value), "未知")


def infer_column_type(series: pd.Series) -> ColumnTypeInfo:
    """
    推断单列的数据类型.
    
    Args:
        series: Pandas Series
        
    Returns:
        ColumnTypeInfo
    """
    name = series.name if hasattr(series, 'name') else "Unknown"
    non_null = series.dropna()
    
    unique_vals = non_null.nunique()
    total_count = len(non_null)
    
    # 策略 1: 检查是否是二元变量
    if unique_vals <= 2 and total_count > 10:
        possible_bool = {'yes', 'no', '是', '否', 'y', 'n'}
        if set(non_null.astype(str).str.lower()) & possible_bool:
            return ColumnTypeInfo(
                column_name=name,
                inferred_type=ColumnType.BINARY,
                dtype=str(non_null.dtype),
                confidence=0.95
            )
    
    # 策略 2: 检查是否是序数量表（基于列名模式和唯一值范围）
    name_lower = str(name).lower()
    ordinal_indicators = ['满意', '评分', '等级', 'rank', 'rating']
    
    has_ordinal_indicator = any(ind in name_lower for ind in ordinal_indicators)
    # 序数量表：整型或浮点量表值（如 4.4），取值范围小且唯一值有限
    is_integer_range = (
        pd.api.types.is_integer_dtype(non_null) and
        2 <= unique_vals <= 10
    )
    is_scale_range = (
        pd.api.types.is_numeric_dtype(non_null) and
        not is_integer_range and
        len(non_null) > 0 and
        non_null.min() >= 0 and non_null.max() <= 10 and
        unique_vals <= 20
    )
    
    if has_ordinal_indicator and (is_integer_range or is_scale_range):
        return ColumnTypeInfo(
            column_name=name,
            inferred_type=ColumnType.ORDINAL,
            dtype=str(non_null.dtype),
            confidence=0.9
        )
    
    # 策略 3: 检查是否是占比/比率（基于列名和取值范围）
    ratio_indicators = ['渗透', '率', '%', '占比', 'rate', 'ratio']
    has_ratio_indicator = any(ind in name_lower for ind in ratio_indicators)
    
    # between() 只能用于数值列；字符串列直接跳过范围检查
    if pd.api.types.is_numeric_dtype(non_null):
        is_percentage_range = (
            non_null.between(0, 100, inclusive='both').mean() > 0.7
        )
    else:
        is_percentage_range = False
    
    if has_ratio_indicator and is_percentage_range:
        return ColumnTypeInfo(
            column_name=name,
            inferred_type=ColumnType.RATIO,
            dtype=str(non_null.dtype),
            confidence=0.85
        )
    
    # 策略 4: 默认数值或分类
    if pd.api.types.is_numeric_dtype(non_null):
        return ColumnTypeInfo(
            column_name=name,
            inferred_type=ColumnType.NUMERIC,
            dtype=str(non_null.dtype),
            confidence=0.9
        )
    else:
        return ColumnTypeInfo(
            column_name=name,
            inferred_type=ColumnType.CATEGORICAL,
            dtype=str(non_null.dtype),
            confidence=0.95
        )


@dataclass
class OrdinalStatistics:
    """序数量表的统计信息."""
    column_name: str
    distribution: dict[str, int]  # 每个值的计数
    percentages: dict[str, float]  # 每个值的百分比
    mode: str | None  # 众数
    mode_percentage: float | None  # 众数占比
    mean: float | None  # 均值（仅供参考）
    median: float | None  # 中位数
    
    def to_dict(self) -> dict:
        return {
            'column': self.column_name,
            'type': 'ordinal_distribution',
            'distribution': self.distribution,
            'percentages': self.percentages,
            'mode': self.mode,
            'mode_percentage': round(self.mode_percentage, 4) if self.mode_percentage is not None else None,
            # ``is not None`` 而非真值判断：序数列的均值/中位数完全可能是 0（例如
            # 以 0 为主的满意度量表），用 ``if self.mean`` 会把合法的 0 当成缺失。
            'mean': round(self.mean, 2) if self.mean is not None else None,
            'median': round(self.median, 2) if self.median is not None else None,
            'note': '推荐重点关注众数和分布形态，而非均值'
        }


@dataclass
class RatioStatistics:
    """占比/比率指标的统计信息."""
    column_name: str
    min_value: float
    max_value: float
    min_row_idx: int  # 最小值所在行
    max_row_idx: int  # 最大值所在行
    quantiles: dict[str, float]  # [25%, 50%, 75%]
    mean: float  # 仍计算但标注局限性
    n_total: int  # 总行数
    
    @property
    def range_info(self) -> str:
        """范围说明."""
        return f"最小值{self.min_value:.2f} (第{self.min_row_idx}行), 最大值{self.max_value:.2f} (第{self.max_row_idx}行)"
    
    def to_dict(self) -> dict:
        return {
            'column': self.column_name,
            'type': 'ratio_statistics',
            'min_value': self.min_value,
            'max_value': self.max_value,
            'min_row_index': self.min_row_idx,
            'max_row_index': self.max_row_idx,
            'range_info': self.range_info,
            'quantiles': self.quantiles,
            'mean': round(self.mean, 4),
            'n_total': self.n_total,
            'note': '建议优先关注中位数与分位数，跨行平均需谨慎解读'
        }


def compute_ordinal_statistics(df: pd.DataFrame, series: pd.Series) -> OrdinalStatistics:
    """计算序数量表的详细分布统计."""
    valid_data = series.dropna().astype(float)
    
    # 统计每个值的出现次数
    value_counts = valid_data.value_counts().to_dict()
    percentages = {k: v / len(valid_data) * 100 for k, v in value_counts.items()}
    
    # 众数（``is not None``：数值 0 也可能是众数，真值判断会把它当缺失）
    mode_val = valid_data.mode().iloc[0] if len(valid_data) > 0 else None
    mode_pct = value_counts.get(mode_val, 0) / len(valid_data) * 100 if mode_val is not None else None
    
    # 均值和中位数（仅供参考）
    mean_val = valid_data.mean() if len(valid_data) > 0 else None
    median_val = valid_data.median() if len(valid_data) > 0 else None
    
    return OrdinalStatistics(
        column_name=series.name,
        distribution={str(k): int(v) for k, v in value_counts.items()},
        percentages={str(round(k, 2)): round(v, 2) for k, v in percentages.items()},
        mode=str(mode_val) if mode_val is not None else None,
        mode_percentage=float(mode_pct) if mode_pct is not None else None,
        mean=float(mean_val) if mean_val is not None else None,
        median=float(median_val) if median_val is not None else None
    )


def compute_ratio_statistics(df: pd.DataFrame, series: pd.Series) -> RatioStatistics:
    """计算占比/比率指标的统计信息（带上下文）."""
    valid_data = series.dropna()
    
    min_val = valid_data.min()
    max_val = valid_data.max()
    
    # 找到对应的行索引
    min_idx = valid_data.idxmin()
    max_idx = valid_data.idxmax()
    
    # 分位数
    quantiles = {
        '25%': float(valid_data.quantile(0.25)),
        '50%': float(valid_data.quantile(0.5)),  # 中位数
        '75%': float(valid_data.quantile(0.75))
    }
    
    return RatioStatistics(
        column_name=series.name,
        min_value=float(min_val),
        max_value=float(max_val),
        min_row_idx=int(min_idx),
        max_row_idx=int(max_idx),
        quantiles={k: round(v, 4) for k, v in quantiles.items()},
        mean=float(valid_data.mean()),
        n_total=len(df)
    )


def compute_type_aware_stats(df: pd.DataFrame) -> dict[str, dict]:
    """
    对 DataFrame 所有列进行类型感知统计.
    
    Returns:
        {col_name: statistics_dict}
    """
    stats_result = {}
    
    for col in df.columns:
        series = df[col]
        
        # 推断类型
        col_type = infer_column_type(series)
        # 枚举转字符串值，保证 JSON 可序列化且下游可用字符串比较
        type_info = asdict(col_type)
        type_info['inferred_type'] = col_type.inferred_type.value
        
        # 根据类型计算不同的统计量
        if col_type.inferred_type == ColumnType.ORDINAL:
            ord_stats = compute_ordinal_statistics(df, series)
            stats_result[col] = {**ord_stats.to_dict(), **type_info}
            
        elif col_type.inferred_type in [ColumnType.RATIO, ColumnType.PERCENTAGE]:
            ratio_stats = compute_ratio_statistics(df, series)
            stats_result[col] = {**ratio_stats.to_dict(), **type_info}
            
        else:
            # 其他类型：标准数值统计
            stats_result[col] = {
                'column': col,
                'type': col_type.type_label,
                'inferred_type': col_type.inferred_type.value,
                'statistics': {
                    'mean': float(series.mean()) if pd.api.types.is_numeric_dtype(series) else None,
                    'median': float(series.median()) if pd.api.types.is_numeric_dtype(series) else None,
                    'std': float(series.std()) if pd.api.types.is_numeric_dtype(series) else None,
                    'min': float(series.min()) if pd.api.types.is_numeric_dtype(series) else None,
                    'max': float(series.max()) if pd.api.types.is_numeric_dtype(series) else None,
                    'count': int(series.count()),
                    'null_count': int(series.isna().sum())
                }
            }
    
    return stats_result
