"""批 38：数字回填校验（fact-check）—— 索引构建、回查语义与两个接入点。

AI 文本中出现的每一个数字回查确定性聚合层的数字索引：命中即可验证，
查无即不可验证。这里锁纯函数语义（抽取/跳过/容差）与 narration、distill
两个接入点的行为。
"""

from __future__ import annotations

from io import BytesIO

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.analytics.fact_check import build_number_index, check_text_numbers
from app.models import Insight

# ---------------------------------------------------------------------------
# 1. build_number_index
# ---------------------------------------------------------------------------


def test_build_number_index_walks_nested_payloads_and_dedupes():
    payload = {
        "datasets": [
            {"name": "A", "row_count": 30, "metrics": [{"name": "DAU", "statistics": {"mean": 739451.6, "min": 1}}]},
            {"name": "B", "row_count": 30, "small_sample": False},
        ],
        "findings": [{"value": 0.9771}, {"value": 0.9771}],
    }
    index = build_number_index(payload)
    assert 739451.6 in index
    assert 30.0 in index
    assert 1.0 in index
    assert 0.9771 in index
    assert len([key for key in index if key == 30.0]) == 1  # 去重


def test_build_number_index_skips_strings_and_bools_and_caps_depth():
    payload = {
        "text": "字符串里的 12345 不收集",
        "flag": True,
        "nested": {"deep": {"deeper": {"deeeeeep": 42.0}}},
    }
    index = build_number_index(payload)
    assert index == {42.0: 42.0}

    # 深度上限：超过 10 层的值不收集。
    deep: dict = {"value": 7.0}
    for _ in range(15):
        deep = {"child": deep}
    assert build_number_index(deep) == {}


# ---------------------------------------------------------------------------
# 2-4. 回查：命中 / 舍入容忍 / 跳过
# ---------------------------------------------------------------------------


def _index() -> dict[float, float]:
    return build_number_index(
        {
            "row_count": 30,
            "statistics": {"mean": 739451.6},
            "rates": [23.11, 0.002774],
            "counts": [2950000, 31],
        }
    )


def test_numbers_hit_the_index():
    for text in ("均值 739451.6 元", "渗透率 23.11%", "共计 2,950,000 条"):
        result = check_text_numbers(text, _index())
        assert result["total"] == 1, text
        assert result["verified"] == 1, text
        assert result["rate"] == 1.0


def test_rounding_tolerance_covers_significant_digits():
    # 索引为 4 位有效数字的 0.002774，文本写更长的 0.0027745 → 命中。
    result = check_text_numbers("单次成本 0.0027745 元", _index())
    assert result["total"] == 1
    assert result["verified"] == 1


def test_dates_and_ordinals_are_skipped():
    text = "2026-08-01 起的第 3 问（finding-2、材料 4、场景 5）在 2026年 收集。"
    result = check_text_numbers(text, _index())
    assert result["total"] == 0
    assert result["rate"] is None


def test_plain_statistics_like_31_days_are_not_skipped():
    # 「31 天」是真实统计（01 的行数即 31），不得被日期/序号规则误跳。
    result = check_text_numbers("活动期共 31 天", _index())
    assert result["total"] == 1
    assert result["verified"] == 1


def test_unverified_numbers_are_reported_with_context():
    result = check_text_numbers("整体规模 99999999 条，环比 12%", {"total": 30.0})
    assert result["total"] == 2
    assert result["verified"] == 0
    assert result["rate"] == 0.0
    values = {item["value"] for item in result["unverified"]}
    assert 99999999.0 in values
    assert all(item["context"] for item in result["unverified"])


def test_empty_text_and_no_numbers_yield_rate_none():
    assert check_text_numbers("", {1.0: 1.0})["rate"] is None
    assert check_text_numbers("没有任何数字的一句话", {1.0: 1.0})["rate"] is None


# ---------------------------------------------------------------------------
# 5. narration 集成
# ---------------------------------------------------------------------------

CSV_METRICS = (
    "date,dau,new_users\n"
    + "".join(f"2026-08-{(i % 28) + 1:02d},{1000 + i},{50 + i}\n" for i in range(28))
)


def _compute(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/auto-report/compute", headers=auth(user))


def _narrate(client, user, report_id: str):
    return client.post(f"/api/v1/auto-reports/{report_id}/narrate", headers=auth(user))


def test_narration_records_fact_check_without_changing_the_state_machine(client, owner, project, monkeypatch):
    client.post(
        "/api/v1/datasets/upload",
        headers=auth(owner),
        data={"project_id": project["id"]},
        files={"file": ("metrics.csv", BytesIO(CSV_METRICS.encode()), "text/csv")},
    )
    computed = data_of(_compute(client, owner, project["id"]))["report"]

    class _FakeAdapter:
        configured = True

        async def complete(self, *, messages, response_schema, request_metadata):
            from app.infrastructure.llm.deepseek import LlmResult

            return LlmResult(
                structured={
                    "title": "解读",
                    # 99999999 不在聚合索引中 → unverified；1000 在（DAU 首日值）。
                    "summary": "AI 概述：DAU 约 1000，虚构峰值 99999999。",
                    "sections": [{"heading": "AI 解读", "content": "- 关键点一"}],
                    "key_findings": ["发现一"],
                    "recommendations": [],
                    "limitations": ["自动选列"],
                },
                finish_reason="stop",
                prompt_tokens=900,
                completion_tokens=700,
            )

    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FakeAdapter())

    data_of(_narrate(client, owner, computed["id"]))
    fetched = data_of(client.get(f"/api/v1/auto-reports/{computed['id']}", headers=auth(owner)))

    assert fetched["status"] == "succeeded"  # 状态机不变
    fact_check = fetched["deterministic_json"]["fact_check"]
    assert fact_check["total"] >= 2
    values = {item["value"] for item in fact_check["unverified"]}
    assert 99999999.0 in values  # 编造的数字被点名
    assert 1000.0 not in values  # 真实数字命中
    assert fetched["markdown"].index("数据概况") < fetched["markdown"].index("AI 解读")


# ---------------------------------------------------------------------------
# 6. distill 集成
# ---------------------------------------------------------------------------


def test_distill_returns_fact_check_and_keeps_persistence_shape(client, owner, project, monkeypatch):
    from app.infrastructure.llm.deepseek import LlmResult

    class _FakeAdapter:
        configured = True

        async def complete(self, *, messages, response_schema, request_metadata):
            return LlmResult(
                structured={
                    "facts": [{"text": "活跃用户 12345 人", "evidence": [{"type": "interview_question", "id": "x"}]}],
                    "hypotheses": [],
                    "recommendations": [],
                    "limitations": ["l"],
                },
                finish_reason="stop",
                prompt_tokens=200,
                completion_tokens=150,
            )

    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FakeAdapter())

    # 一条已回答的采访问题（evidence id 会被 _normalize_distill_evidence 纠正为真实 id）。
    data_of(
        client.post(
            "/api/v1/interview-questions",
            headers=auth(owner),
            json={"project_id": project["id"], "topic": "留存", "question_text": "留存为何下滑？", "answer_text": "活动期事件翻倍。"},
        )
    )
    payload = data_of(
        client.post("/api/v1/ai/distill-interview", headers=auth(owner), json={"project_id": project["id"]})
    )

    fact_check = payload["fact_check"]
    assert fact_check["total"] == 1  # 「12345」被抽取并回查
    assert fact_check["verified"] == 0  # 空 grounding → 索引为空 → 不可验证
    assert fact_check["rate"] == 0.0
    assert fact_check["unverified_claims"] and fact_check["unverified_claims"][0]["text"].startswith("活跃用户")

    # 落库结构不变：洞察仍带 content/evidence_json/status。
    with database.SessionLocal() as db:
        insight = db.scalar(select(Insight).where(Insight.project_id == project["id"]))
        assert insight is not None
        assert insight.content == "活跃用户 12345 人"
        assert insight.evidence_json
        assert insight.status == "draft"
