"""Correctness locks for four long-standing analytics defects.

Each test pins one defect that produced a *wrong number* rather than a crash:

1. ``dag._match_builtin_rules`` marked a rule's **source** column as derived, so
   ``should_exclude_from_correlation`` excluded the wrong pairs and stated the
   derivation backwards.
2. IQR and z-score outliers were counted with three different definitions, and
   ``build_outlier_aggregates`` added both lists so one row could be counted
   twice (``count`` disagreeing with the quality report, ``series`` repeating).
3. ``assess_quality`` never received ``expected_types`` on the production path,
   so ``type_error_count`` was permanently 0 and the 0.20 type penalty could
   never fire (score ceiling of 80 instead of 100).
4. ``infer_column_type_v2`` called any near-unique, whitespace-free column an
   ``identifier``, which mislabelled whitespace-free Chinese prose and stopped
   ``text_metrics`` from scanning it.
"""

from __future__ import annotations

import json

import pandas as pd

from app.ai_context import assert_safe_ai_context, build_ai_context
from app.analytics.dag import build_lineage_map, should_exclude_from_correlation
from app.analytics.engine import AnalysisEngine
from app.analytics.outliers import build_outlier_aggregates, build_raw_outliers_map, compute_column_outliers
from app.analytics.parsing import IDENTIFIER_MAX_LENGTH, infer_column_type_v2
from app.analytics.quality import assess_quality
from app.analytics.text_metrics import extract_text_metrics
from app.services.ai_stages import _reduce_artifact_payload_for_ai

# 45+ 个字符、无空格、无句读标点的中文长句：形状上"近唯一"，但显然是自由文本。
LONG_PROSE = [
    "本季度新用户在首次会话中完成核心动作的比例明显低于上个季度需要重点关注转化路径问题并尽快给出优化方案",
    "上一周期的活跃用户在同一功能上的平均使用次数出现了持续下降需要排查入口改动影响同时评估版本回滚代价",
    "付费转化在注册后第七天达到峰值但随后快速回落说明新手引导环节存在问题需要优化并补齐关键路径埋点",
    "客服工单里关于搜索结果不准确的反馈占比最高且集中在长文档问答场景需要优先修复并校验检索召回口径",
    "推荐位点击率在版本更新后明显走低同时详情页停留时长上升需要确认是否口径变化导致并修正统计逻辑",
]


# ---------------------------------------------------------------------------
# 1. 派生列血缘：源列不能被判成派生列，排除理由的方向必须正确
# ---------------------------------------------------------------------------


def test_source_columns_stay_primitive_and_exclusion_direction_is_correct():
    columns = ["周活跃用户", "总用户数", "渗透率(% 占周活)"]
    lineage = build_lineage_map(columns)

    # 两列都是**源列**，此前被规则误标成派生列（derived_from 还被填成两条源列）。
    assert lineage["周活跃用户"].is_derived is False
    assert lineage["总用户数"].is_derived is False

    # 真正的派生列仍然被识别出来，并指向它的源列。
    derived = lineage["渗透率(% 占周活)"]
    assert derived.is_derived is True
    assert "周活跃用户" in (derived.derived_from or [])

    # 方向：句子的主语必须是 is_derived 的那一列，且理由必须可读（不能是"（None）"）。
    should_exclude, reason = should_exclude_from_correlation("周活跃用户", "渗透率(% 占周活)", lineage)
    assert should_exclude is True
    assert reason is not None
    assert reason.startswith("渗透率(% 占周活)是周活跃用户")
    assert "派生列" in reason
    assert "None" not in reason

    # 源列之间不存在派生关系 -> 不得被误排除。
    assert should_exclude_from_correlation("周活跃用户", "总用户数", lineage) == (False, None)
    assert should_exclude_from_correlation("总用户数", "周活跃用户", lineage) == (False, None)


def test_builtin_rule_only_matches_when_the_rule_name_is_itself_a_column():
    # 规则名（渗透率）不在数据集里 -> 任何列都不得被判为派生列。
    lineage = build_lineage_map(["周活跃用户", "总用户数"])
    assert all(item.is_derived is False for item in lineage.values())

    # 规则名出现且源列齐备 -> 只有规则名那一列是派生列。
    lineage = build_lineage_map(["周活跃用户", "总用户数", "渗透率"])
    assert lineage["渗透率"].is_derived is True
    assert set(lineage["渗透率"].derived_from or []) == {"周活跃用户", "总用户数"}
    assert lineage["周活跃用户"].is_derived is False
    assert lineage["总用户数"].is_derived is False


# ---------------------------------------------------------------------------
# 2. 离群值只有一个口径：quality 与 outliers 的 count/rate 必须相等
# ---------------------------------------------------------------------------


def _outlier_frame() -> pd.DataFrame:
    """One column whose single extreme value trips BOTH the IQR and z masks."""

    return pd.DataFrame({"数值": list(range(10, 25)) + [500]})


def test_outlier_count_is_identical_in_quality_and_aggregates():
    frame = _outlier_frame()
    stats = compute_column_outliers(frame, "数值")
    assert stats is not None

    # 极端值同时越 IQR 界并超过 3σ —— 旧实现会把它按 IQR、Z 各计一次。
    assert stats.count == 1
    assert stats.sample_count == len(frame)

    aggregates = build_outlier_aggregates(frame, ["数值"])
    assert len(aggregates) == 1
    aggregate = aggregates[0]
    assert aggregate["count"] == stats.count

    quality = assess_quality(frame)
    assert quality.outliers["数值"]["count"] == stats.count
    assert quality.outliers["数值"]["rate"] == aggregate["rate"]

    # rate 的分母是**非空样本数**，且 count/rate 只有这一个定义。
    assert aggregate["rate"] == round(1 / len(frame), 4)
    assert aggregate["sample_count"] == len(frame)


def test_outlier_series_has_no_duplicate_values_and_row_level_map_is_deduped():
    frame = pd.DataFrame({"数值": list(range(10, 25)) + [500, 500]})
    stats = compute_column_outliers(frame, "数值")
    assert stats is not None
    assert stats.count == 2

    aggregate = build_outlier_aggregates(frame, ["数值"])[0]
    assert len(aggregate["series"]) == len(set(aggregate["series"])), "同一行不得被 IQR 与 Z 各计一次"
    assert set(aggregate["series"]) == {500.0}

    # 行级映射（进程内）同样按行去重，且形状仍满足 corelation 的 row_index 读取。
    raw = build_raw_outliers_map(frame, ["数值"])
    assert [item["row_index"] for item in raw["数值"]] == [15, 16]
    assert all("row_index" in item and "value" in item for item in raw["数值"])


def test_eda_artifact_never_ships_row_level_outlier_information():
    frame = pd.DataFrame({"数值": list(range(10, 25)) + [500], "类别": [f"c{index}" for index in range(16)]})
    payload = AnalysisEngine().run_eda(frame, lineage_map=build_lineage_map(list(frame.columns))).to_dict()["payload_json"]
    reduced = _reduce_artifact_payload_for_ai(payload)
    safe = assert_safe_ai_context(build_ai_context(artifacts=[{"id": "eda", "title": "EDA", "payload": reduced}]))
    serialized = json.dumps(safe, ensure_ascii=False)
    assert "row_index" not in serialized
    # EDA 的 preview 键（行级记录）本来就不在聚合白名单里，出站必须没有它。
    assert "preview" not in safe["artifacts"][0]["payload"]


# ---------------------------------------------------------------------------
# 3. 类型合规维度真正生效
# ---------------------------------------------------------------------------


def test_type_errors_fire_without_explicit_expected_types():
    clean = pd.DataFrame({"金额": [str(value) for value in range(1, 11)]})
    dirty = pd.DataFrame({"金额": [str(value) for value in range(1, 10)] + ["未知"]})

    clean_report = assess_quality(clean)
    dirty_report = assess_quality(dirty)

    assert clean_report.summary["type_error_count"] == 0
    assert clean_report.overall_score == 100.0

    # 90% 的单元是数字 -> 期望类型 numeric，混进去的那一个就是类型错误。
    assert dirty_report.summary["type_error_count"] == 1
    assert [item["column"] for item in dirty_report.type_errors] == ["金额"]
    assert dirty_report.type_errors[0]["expected_type"] == "numeric"
    # 0.20 的类型权重真正参与惩罚，分数不再虚高。
    assert dirty_report.overall_score == 98.0


def test_explicit_expected_types_still_win_and_text_columns_are_skipped():
    dirty = pd.DataFrame({"金额": [str(value) for value in range(1, 10)] + ["未知"]})
    forced = assess_quality(dirty, expected_types={"金额": "boolean"})
    assert forced.type_errors and forced.type_errors[0]["expected_type"] == "boolean"

    # category / text / identifier 没有"非法值"概念 -> 不产生类型错误。
    prose = pd.DataFrame({"备注": LONG_PROSE})
    channels = pd.DataFrame({"渠道": ["自然流量", "付费投放", "自然流量", "付费投放", "自然流量"]})
    assert assess_quality(prose).summary["type_error_count"] == 0
    assert assess_quality(channels).summary["type_error_count"] == 0


# ---------------------------------------------------------------------------
# 4. 无空格的中文长句是 text，并且真的会被文本指标抽取扫描到
# ---------------------------------------------------------------------------


def test_whitespace_free_chinese_prose_is_text_not_identifier():
    assert all(len(item) > IDENTIFIER_MAX_LENGTH for item in LONG_PROSE)
    assert infer_column_type_v2(pd.Series(LONG_PROSE))["semantic_type"] == "text"

    # 短标识的判据保持不变。
    assert infer_column_type_v2(pd.Series([f"id-{index}" for index in range(10)]))["semantic_type"] == "identifier"
    assert infer_column_type_v2(pd.Series(["KA001", "KA002", "KA003"]))["semantic_type"] == "identifier"


def test_long_prose_column_is_scanned_for_text_metrics():
    # 每行共享同一段"指标文本"（数值相同，标签才会稳定），尾部用不同的中文长句
    # 把行长推到 40 字符以上——正是旧实现会误判成 identifier 的那种列。
    suffixes = [
        "需要继续观察后续走势避免结论被单周波动带偏并且核对周维度口径",
        "需要重点排查入口改动带来的影响并同步评估版本回滚的代价是否可接受",
        "需要复盘新手引导流程中的关键节点并补齐转化路径上的行为埋点数据",
        "需要优先修复检索召回不准的问题并且校验长文档问答场景的切分策略",
        "需要确认推荐位实验分流是否均衡并复核详情页停留时长的统计口径",
    ]
    frame = pd.DataFrame({"备注": [f"本周DAU110k留存率45%{suffix}" for suffix in suffixes]})
    assert all(len(row) > IDENTIFIER_MAX_LENGTH for row in frame["备注"])
    assert infer_column_type_v2(frame["备注"])["semantic_type"] == "text"

    derived, report = extract_text_metrics(frame)
    assert report, "无空格的中文长句列必须能被文本指标抽取扫描到"
    assert {item["source_column"] for item in report} == {"备注"}
    assert any("DAU" in item["derived_column"] for item in report)
    assert [name for name in derived.columns if name.startswith("备注__")]
