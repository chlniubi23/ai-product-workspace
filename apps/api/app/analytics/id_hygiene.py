"""ID hygiene for user-facing LLM text (批 37).

AI 采访/报告/文档的模型输出曾把产物 UUID 与 8 位 hex 前缀直接写给用户
（如「依据07f777c9（r=0.9771）与dd6169f8离群」）。本模块提供两层防护：

* :func:`build_label_map` —— 从 grounding 产物装配 ``{id/8位前缀 → 业务名}``
  映射（业务名来自标题的中文映射表；数据集聚合用数据集名，含 dataset_label）；
* :func:`strip_resource_ids` —— 把用户可见文本中的 UUID/8 位 hex 替换为业务名，
  匹配不到的替换为兜底文案「相关数据产物」。

纯函数、幂等：替换结果中不再含 hex 串（label 自身含 hex 时也回退兜底文案），
对已清洗文本再跑一遍输出不变。``[finding-N]`` 这类短结构化引用不是打击对象
（非 hex），Markdown 表格 cell 内同样逐 token 替换。存量文本不回填。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["UUID_HEX_RE", "build_label_map", "label_for_title", "strip_resource_ids"]

# 完整 UUID（含连字符）或独立的 8 位 hex 前缀。边界用显式环视而非 \b ——
# Python3 的 \w 包含汉字，「据07f777c9」里 \b 会失效；(?<![hex])/(?![hex])
# 只要求 token 前后不是更多 hex 字符。纯数字 8 位串（如统计值 12345678）
# 在 _replace 中跳过，避免把正常数字误判为 id。
UUID_HEX_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
    r"|(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])"
)

#: 产物标题 → 中文业务名（单一来源；未知 title 原样保留 —— 仍是业务名，不是 id）。
TITLE_LABELS: dict[str, str] = {
    "EDA summary": "EDA 汇总",
    "Trend analysis": "趋势分析",
    "Funnel analysis": "漏斗分析",
    "Retention analysis": "留存分析",
    "Anomaly detection": "异常检测",
    "Correlation analysis": "相关性分析",
    "Type-aware statistics": "类型画像",
    "Outlier objects": "离群值分析",
}
#: 前缀规则：``Group comparison by {col}`` → ``按{col}分组对比``。
_GROUP_COMPARISON_PREFIX = "Group comparison by "

#: label_map 匹配不到的 hex 串的兜底文案。
_FALLBACK_LABEL = "相关数据产物"

_HEX_8 = re.compile(r"[0-9a-fA-F]{8}")


def label_for_title(title: str, artifact_type: str = "", payload: Any = None) -> str:
    """单个产物的业务名（与 :func:`build_label_map` 同源的映射规则）。"""

    title = str(title or "").strip()
    if artifact_type == "dataset_summary":
        dataset_label = ""
        if isinstance(payload, dict):
            dataset_label = str(payload.get("dataset_label") or "").strip()
        return f"{title}（{dataset_label}）" if dataset_label else title
    if title.startswith(_GROUP_COMPARISON_PREFIX):
        column = title[len(_GROUP_COMPARISON_PREFIX):].strip()
        return f"按{column}分组对比"
    return TITLE_LABELS.get(title, title)


def _id_keys(artifact_id: str) -> set[str]:
    """一个产物 id 在文本中可能出现的形式：完整 id、按 ``:`` 拆分的段、
    以及各段的前 8 个字符（仅当确为 hex —— ``finding-1`` 这类结构化引用不映射）。"""

    keys: set[str] = set()
    for part in re.split(r"[:\s]+", artifact_id):
        if not part:
            continue
        keys.add(part)
        prefix = part[:8]
        if _HEX_8.fullmatch(prefix):
            keys.add(prefix)
    keys.discard("")
    return keys


def build_label_map(items: list[dict]) -> dict[str, str]:
    """``{产物id / 8位hex前缀 → 业务名}``，供 :func:`strip_resource_ids` 消费。"""

    label_map: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("id") or "")
        title = str(item.get("title") or "")
        if not artifact_id or not title:
            continue
        payload = item.get("payload_json")
        if payload is None:
            payload = item.get("payload")
        label = label_for_title(title, str(item.get("artifact_type") or ""), payload)
        for key in _id_keys(artifact_id):
            label_map.setdefault(key, label)
    return label_map


def strip_resource_ids(text: str, label_map: dict[str, str]) -> str:
    """把文本中的 UUID/8 位 hex 替换为业务名；匹配不到 → 兜底文案。

    幂等：替换结果不含 hex 串（label 自身含 hex 时也回退兜底），二次调用不变。
    """

    if not text:
        return text

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        if token.isdigit():
            return token  # 纯数字 8 位串按普通数字处理，不当作 id
        for key in (token, token.replace("-", "")[:8], token[:8]):
            label = label_map.get(key)
            if label and not UUID_HEX_RE.search(label):
                return label
        return _FALLBACK_LABEL

    cleaned = UUID_HEX_RE.sub(_replace, text)
    # 兜底替换后可能留下空括号/重复顿号，做一次轻量收尾。
    cleaned = cleaned.replace("（）", "").replace("「」", "").replace("、、", "、")
    return cleaned
