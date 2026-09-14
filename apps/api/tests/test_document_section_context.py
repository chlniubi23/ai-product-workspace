"""批 35：PRD 分节上下文裁剪（key_refs 机制）+ 大纲契约扩展 + harmonize 防膨胀。

分节裁剪是提示词工程中唯一触碰装配逻辑的改动，这里锁四件事：
key_refs 的解析与回退、切片的正确性（未引用的数据集聚合不得进入上下文）、
裁剪后上下文仍过 ``assert_safe_ai_context``、以及三个提示词的关键约束。
"""

from __future__ import annotations

from app.ai_context import build_ai_context, validate_document_outline
from app.services.documents import _harmonize_system_prompt, _section_context

# ---------------------------------------------------------------------------
# fixtures：手工构造 doc_context（2 个数据集聚合 + 3 条 findings）
# ---------------------------------------------------------------------------


def _artifacts() -> list[dict]:
    """模拟 _build_document_context 产出的 artifacts（经 _extract_artifacts 后的形状）。"""

    return [
        {"id": "finding-1", "artifact_type": "finding", "title": "发现一：激活转化率 41.7%", "payload": {"kind": "correlation", "dataset": "数据集A", "severity": 2}},
        {"id": "finding-2", "artifact_type": "finding", "title": "发现二：留存率下滑 12%", "payload": {"kind": "missing", "dataset": "数据集B", "severity": 3}},
        {"id": "finding-3", "artifact_type": "finding", "title": "发现三：成本 0.002774 元/次", "payload": {"kind": "outlier", "dataset": "数据集A", "severity": 1}},
        {"id": "ds-a", "artifact_type": "dataset_summary", "title": "数据集A", "payload": {"name": "数据集A", "metrics": [{"name": "激活转化率"}]}},
        {"id": "ds-b", "artifact_type": "dataset_summary", "title": "数据集B", "payload": {"name": "数据集B", "metrics": [{"name": "7日留存率"}]}},
    ]


def _doc_context() -> dict:
    return {
        "safe_context": build_ai_context(goal="提升转化", artifacts=_artifacts(), question="写 PRD"),
        "solution": {"title": "方案一", "approach": "重构激活链路", "effort": "M"},
        "decision": {"problem_statement": "转化低", "proposed_action": "优化引导", "validation_plan": "A/B"},
        "findings_summary": ["发现一：激活转化率 41.7%", "发现二：留存率下滑 12%", "发现三：成本 0.002774 元/次"],
    }


# ---------------------------------------------------------------------------
# 1. outline 校验（key_refs 契约）
# ---------------------------------------------------------------------------


def test_outline_accepts_key_refs():
    outline = validate_document_outline(
        {
            "findings": [{"id": "finding-1", "title": "发现一", "severity": "高"}],
            "sections": [
                {"heading": "需求背景", "purpose": "交代背景", "key_refs": ["finding-1", "finding-2"]},
            ],
            "root_cause": "根因",
        }
    )
    assert outline["sections"][0]["key_refs"] == ["finding-1", "finding-2"]


def test_outline_tolerates_missing_key_refs():
    outline = validate_document_outline(
        {
            "findings": [{"id": "finding-1", "title": "发现一", "severity": "高"}],
            "sections": [{"heading": "需求背景", "purpose": "交代背景"}],
            "root_cause": "根因",
        }
    )
    assert "key_refs" not in outline["sections"][0]


def test_outline_cleanses_invalid_key_refs():
    # 非字符串与空串被清洗；清洗后剩 2 个合法元素 → 保留。
    outline = validate_document_outline(
        {
            "findings": [{"id": "finding-1", "title": "发现一", "severity": "高"}],
            "sections": [
                {"heading": "需求背景", "purpose": "交代背景", "key_refs": [123, "", "finding-1", None, "finding-2"]},
            ],
            "root_cause": "根因",
        }
    )
    assert outline["sections"][0]["key_refs"] == ["finding-1", "finding-2"]


def test_outline_drops_key_refs_below_minimum():
    # 清洗后剩不足 2 个 → 视为缺失（分节侧回退全量），不拒绝整份大纲。
    outline = validate_document_outline(
        {
            "findings": [{"id": "finding-1", "title": "发现一", "severity": "高"}],
            "sections": [
                {"heading": "需求背景", "purpose": "交代背景", "key_refs": ["finding-1", ""]},
            ],
            "root_cause": "根因",
        }
    )
    assert "key_refs" not in outline["sections"][0]


# ---------------------------------------------------------------------------
# 2. 裁剪正确性
# ---------------------------------------------------------------------------


def test_section_context_slices_datasets_by_key_refs():
    context = _section_context(_doc_context(), ["finding-1"], [], "", "")
    ids = [item["id"] for item in context["artifacts"]]
    assert "ds-a" in ids  # finding-1 指向 数据集A
    assert "ds-b" not in ids  # 未被引用的数据集聚合不得进入
    # findings 与叙事证据全部保留。
    assert {"finding-1", "finding-2", "finding-3"} <= set(ids)


def test_section_context_missing_key_refs_falls_back_to_full():
    context = _section_context(_doc_context(), None, [], "", "")
    ids = [item["id"] for item in context["artifacts"]]
    assert {"ds-a", "ds-b"} <= set(ids)


def test_section_context_unresolvable_refs_fall_back_to_full():
    context = _section_context(_doc_context(), ["finding-9", "unknown-id"], [], "", "")
    ids = [item["id"] for item in context["artifacts"]]
    assert {"ds-a", "ds-b"} <= set(ids)


def test_section_context_direct_material_id_hit():
    # ref 直接命中数据集材料 id（不经过 finding-N 映射）。
    context = _section_context(_doc_context(), ["ds-b", "finding-1"], [], "", "")
    ids = [item["id"] for item in context["artifacts"]]
    assert "ds-b" in ids
    # finding-1 指向 数据集A → ds-a 也应命中。
    assert "ds-a" in ids


def test_section_context_carries_narrative_keys():
    context = _section_context(_doc_context(), ["finding-1"], [{"id": "finding-1"}], "前文摘要", "大纲全文")
    assert context["goal"] == "提升转化"
    assert context["outline_findings"] == [{"id": "finding-1"}]
    assert context["solution"]["title"] == "方案一"
    assert context["decision"]["problem_statement"] == "转化低"
    assert context["written_summary"] == "前文摘要"
    assert context["outline_plan"] == "大纲全文"


def test_section_context_survives_firewall():
    """裁剪后的白名单键必须仍过 assert_safe_ai_context（_section_context 内部已断言）。"""

    # 若裁剪引入任何非白名单/禁止字段，_section_context 会抛 AIContextError；
    # 这里再验证白名单七键结构与受信装配键并存（与既有分节上下文行为一致）。
    context = _section_context(_doc_context(), ["finding-1"], [], "摘要", "大纲")
    for key in ("goal", "metrics", "artifacts", "quality", "schema", "question", "insights"):
        assert key in context
    assert isinstance(context["goal"], str)


# ---------------------------------------------------------------------------
# 3. 提示词关键约束
# ---------------------------------------------------------------------------


def test_harmonize_prompt_bounds_section_length():
    prompt = _harmonize_system_prompt()
    assert "105%" in prompt
    assert "不得新增任何数字或论断" in prompt


def test_section_prompt_requires_three_elements():
    from app.services.documents import _section_system_prompt

    prompt = _section_system_prompt(
        "prd",
        "测试 PRD",
        3,
        10,
        "功能范围与优先级",
        "圈定边界",
        "",
        "产品团队",
        None,
        None,
        outline_plan="1. 需求背景；2. 根因判断；3. 功能范围与优先级",
        materials_summary="发现一：激活转化率 41.7%；数据集A",
    )
    assert "[finding-N]" in prompt  # 数字锚点标注口径
    assert "badcase" in prompt  # 边界情况
    assert "设计决策" in prompt
    assert "700-1200 字" in prompt
    assert "本章专属材料（只能从这里取数）：发现一：激活转化率 41.7%；数据集A" in prompt
    assert "大纲全文（你的节是第 3 项）" in prompt
    assert "写深写透" not in prompt


def test_section_prompt_non_prd_keeps_brief():
    from app.services.documents import _section_system_prompt

    prompt = _section_system_prompt(
        "weekly_report", "周报", 1, 5, "本期概览", "概述", "", "产品团队", None, None
    )
    assert "400–800 字" in prompt
    assert "700-1200 字" not in prompt
    assert "读者 产品团队" in prompt  # audience 并入首行
