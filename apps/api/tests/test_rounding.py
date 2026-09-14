"""批 33：``round_stat`` 有效数字舍入的独立单元测试。

golden 测试锁聚合行为，这里只锁舍入函数本身的语义边界。
"""

from __future__ import annotations

import math

from app.analytics.rounding import round_stat


def test_small_magnitudes_keep_precision():
    # round(0.00278, 4) == 0.0028 —— 有效数字舍入正是为了避免这种丢失。
    assert round_stat(0.00278) == 0.00278
    assert round_stat(0.002774333333333334) == 0.002774


def test_standard_significant_rounding():
    assert round_stat(45.678) == 45.68
    assert round_stat(1234.5678) == 1235.0  # 1.235e3
    assert round_stat(739451.61) == 739500.0  # 7.395e5
    assert round_stat(-45.678) == -45.68


def test_integers_and_zero():
    assert round_stat(0) == 0.0
    assert round_stat(30) == 30.0


def test_non_finite_and_non_numeric_return_none():
    assert round_stat(None) is None
    assert round_stat(float("nan")) is None
    assert round_stat(float("inf")) is None
    assert round_stat(float("-inf")) is None
    assert round_stat("abc") is None
    assert round_stat([1, 2]) is None


def test_digit_count_parameter():
    assert round_stat(1234.5678, digits=2) == 1200.0
    assert round_stat(1234.5678, digits=6) == 1234.57
    assert math.isclose(round_stat(0.00278, digits=2), 0.0028)
