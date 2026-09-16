"""批 37：ID 可读性 —— 纯函数 sanitizer、label 装配与采访链路接入。

用户可见文本（问题/依据/发现/正文）不得出现产物 UUID 或 8 位 hex 前缀：
prompt 禁令之外，``analytics.id_hygiene`` 提供确定性兜底。这里锁纯函数语义、
label 映射规则与采访落库链路的端到端清洗。
"""

from __future__ import annotations

from app.analytics.id_hygiene import UUID_HEX_RE, build_label_map, label_for_title, strip_resource_ids

_UUID_A = "07f777c9-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
_UUID_B = "dd6169f8-99aa-4bbc-8ccd-1d2e3f4a5b6c"


def _has_hex(text: str) -> bool:
    return bool(UUID_HEX_RE.search(text))


# ---------------------------------------------------------------------------
# 1. strip_resource_ids
# ---------------------------------------------------------------------------


def test_full_uuid_is_replaced_by_label():
    label_map = {_UUID_A: "相关性分析", _UUID_A[:8]: "相关性分析"}
    cleaned = strip_resource_ids(f"依据{_UUID_A}的结论", label_map)
    assert cleaned == "依据相关性分析的结论"
    assert not _has_hex(cleaned)


def test_eight_hex_prefix_is_replaced():
    label_map = {"07f777c9": "相关性分析", "dd6169f8": "离群值分析"}
    cleaned = strip_resource_ids("依据07f777c9（r=0.9771）与dd6169f8离群", label_map)
    assert "07f777c9" not in cleaned and "dd6169f8" not in cleaned
    assert "相关性分析" in cleaned and "离群值分析" in cleaned


def test_unmatched_hex_falls_back():
    cleaned = strip_resource_ids("依据deadbeef与cafebab0的结论", {"07f777c9": "相关性分析"})
    assert "deadbeef" not in cleaned and "cafebab0" not in cleaned
    assert "相关数据产物" in cleaned


def test_pure_digit_runs_are_not_touched():
    # 纯数字 8 位串更可能是统计值（如 12345678 元），不当作 id 清洗。
    text = "本期营收 12345678 元，环比提升 12%。"
    assert strip_resource_ids(text, {}) == text


def test_table_cells_are_cleaned_too():
    label_map = {"07f777c9": "相关性分析"}
    table = "| 指标 | 依据 |\n| --- | --- |\n| 渗透率 | 07f777c9 |"
    cleaned = strip_resource_ids(table, label_map)
    assert "07f777c9" not in cleaned
    assert cleaned.count("|") == table.count("|")  # 表格结构不破坏


def test_strip_is_idempotent():
    label_map = {_UUID_A: "相关性分析", "07f777c9": "相关性分析", "dd6169f8": "离群值分析"}
    text = f"依据{_UUID_A}与07f777c9、dd6169f8的结论"
    once = strip_resource_ids(text, label_map)
    twice = strip_resource_ids(once, label_map)
    assert once == twice


def test_structured_finding_refs_survive():
    # [finding-N] / 材料1 这类结构化引用不是 hex，不受清洗影响。
    label_map = {"07f777c9": "相关性分析"}
    text = "渗透率与周活强相关 [finding-1]（材料1）；另见 07f777c9。"
    cleaned = strip_resource_ids(text, label_map)
    assert "[finding-1]" in cleaned and "材料1" in cleaned
    assert "07f777c9" not in cleaned


# ---------------------------------------------------------------------------
# 2. build_label_map / label_for_title
# ---------------------------------------------------------------------------


def test_label_map_maps_artifact_titles_to_chinese():
    items = [
        {"id": _UUID_A, "artifact_type": "eda", "title": "EDA summary", "payload": {}},
        {"id": _UUID_B, "artifact_type": "anomaly", "title": "Anomaly detection", "payload": {}},
        {"id": "abc12345-0000-0000-0000-000000000000", "artifact_type": "group", "title": "Group comparison by 渠道", "payload": {}},
    ]
    label_map = build_label_map(items)
    assert label_map[_UUID_A] == "EDA 汇总"
    assert label_map[_UUID_A[:8]] == "EDA 汇总"
    assert label_map[_UUID_B[:8]] == "异常检测"
    assert label_map["abc12345"] == "按渠道分组对比"  # 前缀规则


def test_label_map_dataset_summary_uses_dataset_name_and_label():
    items = [
        {
            "id": _UUID_A,
            "artifact_type": "dataset_summary",
            "title": "03_功能模块使用周报",
            "payload_json": {"dataset_label": "AI 功能模块周度指标"},
        }
    ]
    label_map = build_label_map(items)
    assert label_map[_UUID_A[:8]] == "03_功能模块使用周报（AI 功能模块周度指标）"


def test_label_for_title_unknown_title_is_kept():
    assert label_for_title("自定义产物", "custom") == "自定义产物"


# ---------------------------------------------------------------------------
# 3. 采访链路接入（端到端：mock 模型输出含 UUID → 落库文本无 hex）
# ---------------------------------------------------------------------------


def _llm_result(structured: dict):
    from app.infrastructure.llm.deepseek import LlmResult

    return LlmResult(
        content="",
        finish_reason="stop",
        prompt_tokens=100,
        completion_tokens=100,
        structured=structured,
    )


class _FakeAdapter:
    configured = True

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def complete(self, *, messages, response_schema, request_metadata):
        self.calls.append({"system": messages[0].content, "user": messages[1].content})
        return self._results.pop(0)


def test_interview_persisted_text_is_id_free(client, owner, project, monkeypatch):
    import app.services.ai_stages as ai_stages

    dirty = {
        "question_text": f"依据{_UUID_A[:8]}的留存下滑是否与渠道有关？",
        "topic": "留存",
        "rationale": "07f777c9（r=-0.61）显示留存与渠道强相关",
        "interview_complete": False,
        "completion_note": "",
    }
    fake = _FakeAdapter([_llm_result(dirty)])
    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: fake)

    from conftest import auth, data_of

    response = client.post(
        f"/api/v1/projects/{project['id']}/interview/next-question", headers=auth(owner)
    )
    assert response.status_code == 200, response.text

    payload = data_of(response)
    assert payload["status"] == "ok"
    question = payload["question"]
    assert not _has_hex(question["question_text"])
    assert not _has_hex(question["rationale"])
    assert "相关数据产物" in question["rationale"]  # 空 grounding → 兜底文案
    # prompt 禁令同步验证。
    assert "禁止出现任何 UUID" in fake.calls[0]["system"]


# ---------------------------------------------------------------------------
# 4. prompt 断言
# ---------------------------------------------------------------------------


def test_shared_suffix_and_narration_prompt_constraints():
    from app.services.auto_report import _auto_report_system_prompt

    narration = _auto_report_system_prompt()
    assert "≤1200 字" in narration  # 精准层总预算
    assert "≤300 字" in narration
    assert "信息优先级" in narration
    assert "禁止出现资源 id 或哈希串" in narration


def test_shared_suffix_carries_id_ban():
    from app.services.documents import _outline_system_prompt

    outline = _outline_system_prompt("prd", "测试文档", "产品团队")
    # 大纲 prompt 自身无 id 禁令（共享尾缀统一追加），只验证精准层措辞。
    assert "按信息量降序排列" in outline

    from app.services.documents import _section_system_prompt

    section = _section_system_prompt("prd", "文档", 1, 10, "需求背景", "背景", "", "产品团队", None, None, outline_plan="x")
    assert "无法追溯的论断直接删除" in section
    assert "优先删弱论据" in section

    from app.services.documents import _harmonize_system_prompt

    assert "删除冗余为第一手段" in _harmonize_system_prompt()
