"""Batch-8 budget model: spend-then-account with hard caps and a daily valve.

The three rules under test:
1. spend directly -- no per-request token gate before or after the call; a
   completed result is never discarded for budget reasons;
2. hard output caps -- desired output ceilings are clamped by the module
   constant HARD_OUTPUT_CAP (16384) on every path;
3. one daily valve -- refused before any provider spend, with an actionable
   hint, when the conservative worst case does not fit today's budget.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.infrastructure.llm.deepseek import LlmResult
from app.models import AIRun, User, Workspace
from app.services.workspace_settings import _workspace_token_usage

VALVE_DAILY = 16000


class _FakeAdapter:
    configured = True

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def complete(self, *, messages, response_schema, request_metadata):
        self.calls.append({"system": messages[0].content, "max_tokens": request_metadata.max_tokens})
        return self._results.pop(0)


def _report(prompt_tokens: int, completion_tokens: int, finish_reason: str = "stop", truncated: bool = False):
    if truncated:
        return LlmResult(
            content='{"title": "截',
            finish_reason="length",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    return LlmResult(
        structured={
            "title": "升级引导 PRD",
            "summary": "基于证据的草案。",
            "sections": [{"heading": "需求背景", "content": "旧版本用户缺乏引导。"}],
            "key_findings": ["旧版本事件占比过半"],
            "recommendations": ["上线应用内引导"],
            "limitations": ["样本周期较短"],
        },
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _outline_stub():
    return LlmResult(
        structured={
            "findings": [{"id": "finding-1", "title": "缺口 14.3%", "severity": "高"}],
            "sections": [{"heading": "第1节", "purpose": "写透"}],
            "root_cause": "根因。",
        },
        finish_reason="stop",
        prompt_tokens=900,
        completion_tokens=600,
    )


def _section_result(prompt_tokens: int, completion_tokens: int, finish_reason: str = "stop", truncated: bool = False):
    if truncated:
        return LlmResult(
            content='{"heading": "第1节", "content": "截',
            finish_reason="length",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    return LlmResult(
        structured={"heading": "第1节", "content": "正文" * 200},
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _patch_adapter(monkeypatch, fake: _FakeAdapter) -> None:
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: fake)


def make_ready(client, owner, project) -> dict:
    from conftest import review_schema, upload, version_of

    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version_id = uploaded["version"]["id"]
    review_schema(client, owner, version_id)
    return {
        "project": project,
        "version_id": version_id,
        "version": version_of(client, owner, version_id),
    }


def confirmed_insight(client, owner, ready: dict) -> dict:
    return data_of(
        client.post(
            "/api/v1/insights",
            headers=auth(owner),
            json={
                "project_id": ready["project"]["id"],
                "title": "已确认事实",
                "insight_type": "fact",
                "content": "渠道活动期间事件量翻倍。",
                "evidence": [{"type": "dataset_version", "id": ready["version_id"]}],
                "status": "confirmed",
            },
        )
    )


def generate_doc(client, user, ready: dict, title: str = "预算测试 PRD") -> dict:
    """POST generation, wait for the inline job, return the re-read document."""

    created = data_of(
        client.post(
            "/api/v1/ai/draft-document",
            headers=auth(user),
            json={
                "project_id": ready["project"]["id"],
                "document_type": "prd",
                "title": title,
                "source_refs": [{"type": "insight", "id": ready["insight_id"]}],
            },
        )
    )
    return data_of(client.get(f"/api/v1/documents/{created['document']['id']}", headers=auth(user)))


def set_daily_budget(client, owner, workspace_id: str, daily: int) -> None:
    data_of(
        client.patch(
            f"/api/v1/workspaces/{workspace_id}/settings",
            headers=auth(owner),
            json={"ai_daily_token_budget": daily},
        )
    )


def latest_generation_run() -> AIRun:
    with database.SessionLocal() as db:
        return db.scalar(
            select(AIRun).where(AIRun.feature_name == "document_generation").order_by(AIRun.created_at.desc()).limit(1)
        )



# --------------------------------------------------------------------------
# Rule 1: spend directly -- big observed usage must land
# --------------------------------------------------------------------------


def test_large_observed_usage_is_kept_not_rejected(client, owner, project, monkeypatch):
    """Old model killed a paid 16746-token result against per_request=16000;
    the new model must keep it (only account for it)."""

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    # Batch 17: the large paid result is now a SECTION call (the outline is
    # the cheap first pass); spend-then-account keeps it either way.
    fake = _FakeAdapter([_outline_stub(), _section_result(prompt_tokens=3746, completion_tokens=13000)])
    _patch_adapter(monkeypatch, fake)
    document = generate_doc(client, owner, ready)

    content = document["current_version"]["content_markdown"]
    assert "第1节" in content
    assert len(fake.calls) == 2
    with database.SessionLocal() as db:
        run = db.scalar(
            select(AIRun).where(AIRun.feature_name == "document_section").order_by(AIRun.created_at.desc()).limit(1)
        )
    assert run.status == "succeeded"
    assert run.error_code is None
    assert run.prompt_tokens == 3746
    assert run.completion_tokens == 13000


# --------------------------------------------------------------------------
# Rule 2: hard output caps on every path
# --------------------------------------------------------------------------


def test_document_uses_8192_and_retry_caps_at_hard_cap(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    # Batch 17: outline first (default ceiling), then a section call whose
    # truncation retry doubles 8192 -> 16384 and never beyond.
    fake = _FakeAdapter(
        [
            _outline_stub(),
            _section_result(3746, 8000, finish_reason="length", truncated=True),
            _section_result(3746, 9000),
        ]
    )
    _patch_adapter(monkeypatch, fake)
    generate_doc(client, owner, ready)

    assert [call["max_tokens"] for call in fake.calls] == [4096, 8192, 16384]
    assert all(call["max_tokens"] <= 16384 for call in fake.calls)
    with database.SessionLocal() as db:
        run = db.scalar(
            select(AIRun).where(AIRun.feature_name == "document_section").order_by(AIRun.created_at.desc()).limit(1)
        )
    assert run.status == "succeeded"


def test_normal_call_keeps_the_workspace_default_cap(client, owner, project, monkeypatch):
    """Regression lock: a default-shaped _run_ai_stage call (distill, same as
    the interpret family) still rides the 4096 default output ceiling."""

    fake = _FakeAdapter([_report(500, 300)])
    _patch_adapter(monkeypatch, fake)
    response = client.post(
        "/api/v1/ai/distill-interview",
        headers=auth(owner),
        json={"project_id": project["id"]},
    )
    assert response.status_code == 200, response.text
    assert fake.calls, response.text
    assert fake.calls[0]["max_tokens"] == 4096


# --------------------------------------------------------------------------
# Rule 3: the daily valve refuses before any spend
# --------------------------------------------------------------------------


def test_daily_valve_refuses_before_any_provider_spend(client, owner, project, monkeypatch):
    """daily == per_request == 9000 forces worst_case (>=1000+8192) past the
    valve: refuse before the call, keep the provider untouched."""

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]
    data_of(
        client.patch(
            f"/api/v1/workspaces/{owner['workspace']['id']}/settings",
            headers=auth(owner),
            json={"ai_daily_token_budget": 9000, "ai_per_request_token_budget": 9000},
        )
    )

    fake = _FakeAdapter([])
    _patch_adapter(monkeypatch, fake)
    document = generate_doc(client, owner, ready)

    assert fake.calls == [], "the valve must refuse before any provider call"
    # the deliverable still exists as a template fallback
    assert "证据溯源" in document["current_version"]["content_markdown"]

    run = latest_generation_run()
    assert run.status == "failed"
    assert run.error_code == "AI_BUDGET_EXCEEDED"
    assert run.prompt_tokens is None and run.completion_tokens is None
    budget_meta = (run.input_summary_json or {}).get("budget", {})
    assert budget_meta.get("daily_remaining", 0) >= 0
    assert "每日 token 预算" in budget_meta.get("hint", "")
    assert budget_meta.get("needed_tokens", 0) >= 9192


def test_valve_hint_reports_remaining_and_needed_tokens(client, owner, project, monkeypatch):
    """The valve details must be actionable: remaining, needed, and where to
    fix it (daily 9100 < worst-case floor 9192 guarantees the trip)."""

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]
    data_of(
        client.patch(
            f"/api/v1/workspaces/{owner['workspace']['id']}/settings",
            headers=auth(owner),
            json={"ai_daily_token_budget": 9100, "ai_per_request_token_budget": 9000},
        )
    )

    fake = _FakeAdapter([])
    _patch_adapter(monkeypatch, fake)
    generate_doc(client, owner, ready)

    assert fake.calls == [], "the valve must refuse before any provider call"
    run = latest_generation_run()
    meta = (run.input_summary_json or {}).get("budget", {})
    assert run.error_code == "AI_BUDGET_EXCEEDED"
    # remaining == budget - accounted usage (whatever prior rows reserved)
    assert meta.get("daily_remaining") == 9100 - meta.get("daily_used_tokens", 0)
    assert meta.get("needed_tokens") >= 9192
    assert "调大" in meta.get("hint", "")


# --------------------------------------------------------------------------
# Concurrent reservation: running rows reserve their recorded worst case
# --------------------------------------------------------------------------


def test_workspace_token_usage_reserves_recorded_worst_case(client, owner):
    """New-style running rows reserve their recorded worst case; legacy rows
    fall back to the per-request budget."""

    with database.SessionLocal() as db:
        user = db.get(User, owner["user"]["id"])
        workspace = db.get(Workspace, owner["workspace"]["id"])
        stamp = datetime.now(UTC).replace(tzinfo=None)

        new_style = AIRun(
            workspace_id=workspace.id,
            user_id=user.id,
            feature_name="document_generation",
            provider="deepseek",
            model="deepseek-chat",
            status="running",
            input_summary_json={"budget": {"worst_case": 12000, "max_tokens": 8192}},
            created_at=stamp,
        )
        legacy = AIRun(
            workspace_id=workspace.id,
            user_id=user.id,
            feature_name="interpret",
            provider="deepseek",
            model="deepseek-chat",
            status="running",
            input_summary_json={},
            created_at=stamp,
        )
        db.add(new_style)
        db.add(legacy)
        db.commit()

        from app.services.workspace_settings import _workspace_ai_budget

        budget = _workspace_ai_budget(workspace)
        reserved = _workspace_token_usage(db, workspace, budget["per_request"])
        assert reserved == 12000 + budget["per_request"]

        db.delete(new_style)
        db.delete(legacy)
        db.commit()
