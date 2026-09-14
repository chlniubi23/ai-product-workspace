"""统计量的有效数字舍入（batch 33）.

报告行文与聚合展示层的 magnitude 依赖型统计量按 ``digits`` 位**有效数字**呈现，
避免 ``round(x, 4)`` 把 0.00278 显示成 0.0028 这类小量级精度丢失。引擎内部计算
一律不经过这里 —— 本模块只影响"呈现"。

``digits`` 指有效数字个数，不是小数位：

* ``0.00278`` —— 3 位有效数字，4 位下保持 ``0.00278``（``round(0.00278, 4)``
  会给出 ``0.0028``，正是要避免的）；
* ``45.678`` —— 4 位有效数字 → ``45.68``；
* ``739451.61`` —— 4 位有效数字即 ``7.395e5`` → ``739500.0``（不是 739451.61，
  也不是 739400）；
* 量纲无关的量（相关系数 r/p、0-1 占比）与 trend 原始数据点**不**经过本函数，
  继续用 ``round(x, 4)``。

舍入采用标准有效数字（``decimal.ROUND_HALF_EVEN``，与 Python 内建 ``round``
的银行家舍入一致）。
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

__all__ = ["round_stat"]


def round_stat(value: Any, digits: int = 4) -> Any:
    """按有效数字舍入；``None``/非有限值/非数值输入一律返回 ``None``。

    >>> round_stat(0.00278)
    0.00278
    >>> round_stat(45.678)
    45.68
    >>> round_stat(1234.5678)
    1235.0
    >>> round_stat(739451.61)
    739500.0
    >>> round_stat(None) is None
    True
    >>> round_stat(float("nan")) is None
    True
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if number == 0:
        return 0.0
    exponent = math.floor(math.log10(abs(number)))
    decimal_places = digits - 1 - exponent
    quantum = Decimal(1).scaleb(-decimal_places)
    return float(Decimal(str(number)).quantize(quantum, rounding=ROUND_HALF_EVEN))
