"""Numeric fact-checking —— AI 文本的数字回填校验（批 38）.

本项目的核心主张是「AI 输出可信」：证据链已做到引用级（每条结论挂产物 id），
这里推进到**数字级**——AI 文本中出现的每一个数字，回查确定性聚合层建的数字
索引：命中即「可验证」，查无即「不可验证」。

只读聚合结果，**不动** ``analytics/engine.py`` 与任何统计路径；零依赖
（re + 既有 ``parsing.parse_numeric``）。校验结果为标量/小列表，写入既有
JSON 列（``deterministic_json["fact_check"]`` / distill 返回值），零迁移、
不进 AI 上下文。

抽取规则（全部确定性，宁紧勿松）：
* 复用 :func:`parsing.parse_numeric`（千分位/货币/%/k|M|万|亿 后缀，百分比
  保留其数值本身：``45%`` → ``45.0``）；
* 跳过：日期（YYYY-MM-DD / YYYY-MM / YYYY年M月[日] / YYYY/…）、「第 N」、
  ``finding-N``/「材料N」/「场景N」序号、``YYYY年``、UUID；
* 纯数字 8 位串按普通数字参与校验（与 id_hygiene 的 id 判定相反：这里要抓
  的是"AI 编的数字"，多查不漏）。
"""

from __future__ import annotations

import re
from typing import Any

from .parsing import parse_numeric

__all__ = ["build_number_index", "check_text_numbers"]

_MAX_DEPTH = 10
_UNVERIFIED_LIMIT = 5
#: 相对容差：覆盖 4 位有效数字的舍入误差并留一倍余量
#: （|x - v| <= rel_tol * max(|v|, 1e-9)）。
DEFAULT_REL_TOL = 0.001

# 跳过模式：先遮蔽再扫描，遮蔽占位符不含数字字符。
_SKIP_PATTERNS = [
    # ISO / 斜杠日期与 YYYY-MM：2026-08-01、2026/8、2026-08
    re.compile(r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?"),
    # 中文日期：2026年8月1日 / 2026年8月 / 2026年
    re.compile(r"\d{4}年(?:\d{1,2}月(?:\d{1,2}[日号])?)?"),
    re.compile(r"第\s*\d+"),
    re.compile(r"(?:finding|材料|场景)\s*[-#]?\d+", re.IGNORECASE),
    re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"),
]
_PLACEHOLDER = "\uFFFD" * 12

# 数字 token（含 ASCII 千分位/小数/可选的 % 或量级后缀）。注意不含全角逗号
# —— parse_numeric 只剥离 ASCII 千分位，吞进「，」会让整个 token 解析失败。
_TOKEN_RE = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?(?:%|％|[kKmMbB万亿])?")


def build_number_index(payload: Any, _depth: int = 0) -> dict[float, float]:
    """遍历 JSON（聚合/artifact payload），收集所有 float/int 数值。

    返回 ``{value: value}`` 查找表（去重）。不递归超过 10 层；跳过字符串内的
    数字（字符串是标签/文本，不是统计值）与布尔值。
    """

    index: dict[float, float] = {}
    if _depth >= _MAX_DEPTH:
        return index
    if payload is None or isinstance(payload, bool):
        return index
    if isinstance(payload, (int, float)):
        number = float(payload)
        if number == number and number != float("inf") and number != float("-inf"):
            index[number] = number
        return index
    if isinstance(payload, str):
        return index
    if isinstance(payload, dict):
        for value in payload.values():
            index.update(build_number_index(value, _depth + 1))
        return index
    if isinstance(payload, (list, tuple, set)):
        for value in payload:
            index.update(build_number_index(value, _depth + 1))
        return index
    return index


def _verified(value: float, index: dict[float, float], rel_tol: float) -> bool:
    return any(abs(value - known) <= rel_tol * max(abs(known), 1e-9) for known in index.values())


def check_text_numbers(
    text: str, index: dict[float, float], *, rel_tol: float = DEFAULT_REL_TOL
) -> dict[str, Any]:
    """抽取文本中的数字并回查索引。

    返回 ``{"total", "verified", "rate", "unverified"}``：``total == 0`` 时
    ``rate`` 记 ``None``（无数值可校验，不记分）；``unverified`` 最多保留
    5 条（``{"value", "context"}``，context 为原文 ±20 字符）。
    """

    empty: dict[str, Any] = {"total": 0, "verified": 0, "rate": None, "unverified": []}
    if not text or not isinstance(text, str):
        return empty

    masked = text
    for pattern in _SKIP_PATTERNS:
        masked = pattern.sub(_PLACEHOLDER, masked)

    total = 0
    verified = 0
    unverified: list[dict[str, Any]] = []
    for match in _TOKEN_RE.finditer(masked):
        value = parse_numeric(match.group(0))
        if value is None:
            continue
        total += 1
        if _verified(value, index, rel_tol):
            verified += 1
        elif len(unverified) < _UNVERIFIED_LIMIT:
            start = max(0, match.start() - 20)
            unverified.append({"value": value, "context": text[start: match.end() + 20].strip()})

    rate = round(verified / total, 4) if total else None
    return {"total": total, "verified": verified, "rate": rate, "unverified": unverified}
