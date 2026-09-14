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


#: 每列最多出站的极值个数。离群值摘要只带**有界**的数值样本，绝不带行身份。
OUTLIER_VALUE_SAMPLE_LIMIT = 5

#: IQR / Z-score 的默认阈值。``quality.assess_quality``、本模块的出站聚合与
#: ``services.auto_report`` 共用同一组默认值——这是"同一列只应有一个离群值
#: 个数"的前提。
OUTLIER_IQR_MULTIPLIER = 1.5
OUTLIER_Z_THRESHOLD = 3.0


@dataclass(frozen=True)
class ColumnOutlierStats:
    """单列离群值的**统一口径**：``(IQR 越界) | (|z| > z_threshold)``，按行去重。

    这是全仓库唯一的离群值定义。``quality.assess_quality`` 的 outlier 段、
    :func:`build_outlier_aggregates`、:func:`build_raw_outliers_map` 与
    ``services.auto_report`` 都经 :func:`compute_column_outliers` 取值，因此同一列
    同一数据只会有一个 ``count`` / ``rate``，也不再出现"同一行被 IQR 与 Z 各计一次"。

    ``rows`` 携带行索引，属**行级**信息，仅供进程内使用；出站请用
    :func:`build_outlier_aggregates`。
    """

    column: str
    sample_count: int
    rows: list[OutlierInfo]
    direction: str
    min_value: float | None
    max_value: float | None
    values: list[float]
    bounds: dict[str, float | None]

    @property
    def count(self) -> int:
        """去重后的离群行数。"""

        return len(self.rows)

    @property
    def rate(self) -> float:
        """离群行数 / 该列**非空样本数**（不是总行数）。"""

        return round(self.count / self.sample_count, 4) if self.sample_count else 0.0


def compute_column_outliers(
    df: pd.DataFrame,
    column: str,
    *,
    iqr_multiplier: float = OUTLIER_IQR_MULTIPLIER,
    z_threshold: float = OUTLIER_Z_THRESHOLD,
) -> ColumnOutlierStats | None:
    """计算单列的统一离群值口径。

    定义（固定，不再有第二套）：

    * 掩码 = IQR 越界 **并集** 极端 z 值，`count` 为掩码命中**行数**（按行去重）；
    * `rate = count / 该列非空样本数`；
    * 极值样本按 ``abs(value)`` 降序、**按值去重**后取前
      ``OUTLIER_VALUE_SAMPLE_LIMIT`` 个（``series`` 里不会再出现重复值）；
    * z 值使用总体标准差（``ddof=0``），与 ``analytics/engine.py`` 及原
      ``quality.assess_quality`` 保持一致。

    列不存在或不是数值列时返回 ``None``；非空样本为 0 时返回空统计（``count == 0``）。
    """

    if column not in df.columns or not pd.api.types.is_numeric_dtype(df[column]):
        return None

    # 批 33（golden 回归暴露）：bool 列是 is_numeric_dtype 但 quantile 会触发
    # numpy 布尔减法错误 —— 统一按 0/1 参与离群值统计，与 engine 的布尔列
    # 处理保持一致。
    source = df[column].astype("int64") if pd.api.types.is_bool_dtype(df[column]) else df[column]
    series = pd.to_numeric(source, errors="coerce")
    present = series.dropna()
    sample_count = int(len(present))
    if sample_count == 0:
        return ColumnOutlierStats(
            column=str(column),
            sample_count=0,
            rows=[],
            direction="none",
            min_value=None,
            max_value=None,
            values=[],
            bounds={},
        )

    q1 = float(present.quantile(0.25))
    q3 = float(present.quantile(0.75))
    spread = q3 - q1
    lower = q1 - iqr_multiplier * spread
    upper = q3 + iqr_multiplier * spread
    mean = float(present.mean())
    std = float(present.std(ddof=0))

    iqr_mask = (series < lower) | (series > upper)
    # z 值用总体标准差（ddof=0），与 analytics/engine.py 及原 quality.assess_quality 一致。
    z_mask = (series - mean).abs() > z_threshold * std if std > 0 else pd.Series(False, index=series.index)
    # 并集掩码 + 按行去重：同一行即便同时越 IQR 界又超 z 阈值，也只计一次。
    mask = (iqr_mask | z_mask).fillna(False)

    rows: list[OutlierInfo] = []
    sides: set[str] = set()
    for position, index in enumerate(series.index[mask]):
        value = float(series.loc[index])
        # 判定类型时保持"IQR 优先"：能由 IQR 解释的行按 IQR 归因，其余才记 z。
        if value > upper:
            outlier_type, threshold, is_upper = "iqr_upper", upper, True
        elif value < lower:
            outlier_type, threshold, is_upper = "iqr_lower", lower, False
        elif value > mean:
            outlier_type, threshold, is_upper = "zscore_high", mean + z_threshold * std, True
        else:
            outlier_type, threshold, is_upper = "zscore_low", mean - z_threshold * std, False
        sides.add("upper" if is_upper else "lower")
        # ``OutlierInfo.row_index`` 声明为 int；非整数索引（如字符串索引的帧）
        # 没有可靠的行身份，用序号占位——出站聚合本来就不带行号。
        row_index = int(index) if isinstance(index, (int, np.integer)) else position
        rows.append(
            OutlierInfo(
                column_name=str(column),
                row_index=row_index,
                value=value,
                outlier_type=outlier_type,
                threshold=float(threshold),
                is_upper=is_upper,
            )
        )

    if sides == {"upper"}:
        direction = "upper"
    elif sides == {"lower"}:
        direction = "lower"
    elif sides:
        direction = "both"
    else:
        direction = "none"

    raw_values = [item.value for item in rows]
    # 极值样本：按 |value| 降序 + **按值去重**（先舍入再入集合，保证出站的
    # ``series`` 里不会出现重复值），最后截断到有界长度。
    distinct: dict[float, None] = {}
    for value in sorted(raw_values, key=abs, reverse=True):
        distinct.setdefault(round(value, 4), None)

    return ColumnOutlierStats(
        column=str(column),
        sample_count=sample_count,
        rows=rows,
        direction=direction,
        min_value=round(min(raw_values), 4) if raw_values else None,
        max_value=round(max(raw_values), 4) if raw_values else None,
        values=list(distinct)[:OUTLIER_VALUE_SAMPLE_LIMIT],
        bounds={"iqr_lower": lower, "iqr_upper": upper, "z_threshold": z_threshold},
    )


def build_raw_outliers_map(df: pd.DataFrame, columns: list[str] | None = None) -> dict[str, list[dict]]:
    """统一口径的**行级**离群点映射（仅限进程内使用）。

    形状刻意与 ``corelation.compute_correlation_with_tests`` 的读取方式对齐
    （它按下标 ``out['row_index']`` 取行号），用于计算剔除离群点后的稳健相关系数。

    ⚠️ 返回值含行号，**禁止**直接交给 ``build_ai_context`` —— 出站请用
    :func:`build_outlier_aggregates`。
    """
    target = list(columns) if columns is not None else [
        str(col) for col in df.columns if pd.api.types.is_numeric_dtype(df[col])
    ]

    raw_map: dict[str, list[dict]] = {}
    for col in target:
        stats = compute_column_outliers(df, str(col))
        if stats is None or not stats.count:
            continue
        raw_map[str(col)] = [item.to_dict() for item in stats.rows]
    return raw_map


def build_outlier_aggregates(df: pd.DataFrame, columns: list[str] | None = None) -> list[dict]:
    """按列有界聚合的离群值摘要，**不含任何行级数据**。

    这是「对象级离群值」唯一允许出站的形状（Phase 1 决策 1 · 方案 A）：保留
    「AI 能引用具体数值」的价值（范围 + 有界极值），但不暴露行身份，因此防火墙
    的 ``_ROW_LIST_KEYS`` 无需放宽。

    极值样本放在 ``series`` 键下，而不是字面意义上的 ``top_values``：防火墙只放行
    ``_AGGREGATE_LIST_KEYS`` 里的键，其它键下的列表会被静默丢弃（``series`` 是其中
    语义最贴近的允许键）。

    计数口径与 ``quality.assess_quality`` 完全一致（同一个
    :func:`compute_column_outliers`），``series`` 按值去重后仍有界。

    Returns:
        ``[{name, method, count, rate, direction, min_value, max_value, series}, ...]``
    """
    target = list(columns) if columns is not None else [
        str(col) for col in df.columns if pd.api.types.is_numeric_dtype(df[col])
    ]

    aggregates: list[dict] = []
    for col in target:
        stats = compute_column_outliers(df, str(col))
        if stats is None or not stats.count:
            continue

        aggregates.append({
            "name": str(col),
            "method": "iqr_and_zscore",
            "count": stats.count,
            "rate": stats.rate,
            "sample_count": stats.sample_count,
            "direction": stats.direction,
            "min_value": stats.min_value,
            "max_value": stats.max_value,
            "series": stats.values,
        })

    return aggregates
