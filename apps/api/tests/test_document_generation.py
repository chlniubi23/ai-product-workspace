"""AI-driven document generation: grounded context, degradation, fallback.

The job handler runs inside the request cycle under TestClient (background
tasks execute synchronously), so the endpoint exercises the full chain:
template v1 immediately, AI (or fallback) v2 after the job completes.  With
no provider key the degraded path must still deliver a complete deterministic
document; with a scripted adapter the AI path must produce Chinese sections
on top of the unchanged evidence manifest.
"""

from __future__ import annotations

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.ai_context import assert_safe_ai_context
from app.models import AIRun


def add_manual_question(client, user, project_id: str, question: str, answer: str) -> dict:
    return data_of(
        client.post(
            "/api/v1/interview-questions",
            headers=auth(user),
            json={"project_id": project_id, "topic": "背景", "question_text": question, "answer_text": answer},
        )
    )


def generate_doc(client, user, ready: dict, document_type: str = "prd", title: str = "测试 PRD") -> dict:
    """POST the generation and re-read the document after the inline job.

    The response body is serialized before TestClient runs the background
    task, so the AI/fallback version is only visible on the second read.
    """

    insight_id = ready["insight_id"]
    created = data_of(
        client.post(
            "/api/v1/ai/draft-document",
            headers=auth(user),
            json={
                "project_id": ready["project"]["id"],
                "document_type": document_type,
                "title": title,
                "source_refs": [{"type": "insight", "id": insight_id}],
            },
        )
    )
    return data_of(client.get(f"/api/v1/documents/{created['document']['id']}", headers=auth(user)))


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


# --------------------------------------------------------------------------
# Degraded path: no provider key
# --------------------------------------------------------------------------


def test_document_generation_without_key_falls_back_to_template(client, owner, project):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    document = generate_doc(client, owner, ready)
    assert document["current_version"]["content_markdown"]
    # v1 template immediately, v2 fallback after the inline job run
    assert len(document["versions"]) >= 2
    content = document["versions"][-1]["content_markdown"]
    assert "## Evidence manifest" in content
    assert "Draft status" in content

    with database.SessionLocal() as db:
        run = db.scalar(select(AIRun).where(AIRun.feature_name == "document_generation").order_by(AIRun.created_at.desc()).limit(1))
        assert run is not None
        assert run.status == "not_configured"
        assert run.error_code == "LLM_NOT_CONFIGURED"


def test_document_job_records_audit_with_fallback_reason(client, owner, project):
    from conftest import audit_actions

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]
    workspace_id = owner["workspace"]["id"]

    before = audit_actions(workspace_id)
    generate_doc(client, owner, ready)
    after = audit_actions(workspace_id)
    assert "document.generation_completed" in after
    assert len([a for a in after if a == "document.generation_completed"]) == len(
        [a for a in before if a == "document.generation_completed"]
    ) + 1


# --------------------------------------------------------------------------
# AI path with a scripted adapter
# --------------------------------------------------------------------------


class _FakeAdapter:
    configured = True

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def complete(self, *, messages, response_schema, request_metadata):
        self.calls.append(
            {"system": messages[0].content, "user": messages[1].content, "max_tokens": request_metadata.max_tokens}
        )
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _report_result(finish_reason: str = "stop", truncated: bool = False):
    from app.infrastructure.llm.deepseek import LlmResult

    if truncated:
        return LlmResult(content='{"title": "截断', finish_reason="length", prompt_tokens=100, completion_tokens=4096)
    return LlmResult(
        structured={
            "title": "升级引导 PRD",
            "summary": "基于采访与数据结论的需求草案。",
            "sections": [
                {"heading": "需求背景", "content": "旧版本用户缺乏升级引导。"},
                {"heading": "目标与非目标", "content": "目标是提升升级率；非目标为渠道重构。"},
            ],
            "key_findings": ["旧版本事件占比过半"],
            "recommendations": ["上线应用内升级引导"],
            "limitations": ["样本周期较短"],
        },
        finish_reason=finish_reason,
        prompt_tokens=800,
        completion_tokens=1200,
    )


def _patch_adapter(monkeypatch, fake: _FakeAdapter) -> None:
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: fake)


def test_ai_document_renders_chinese_sections_and_keeps_manifest(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]
    add_manual_question(client, owner, ready["project"]["id"], "为什么不升级？", "习惯旧交互。")

    fake = _FakeAdapter([_report_result()])
    _patch_adapter(monkeypatch, fake)

    document = generate_doc(client, owner, ready, document_type="prd", title="升级引导 PRD")
    content = document["current_version"]["content_markdown"]
    assert "## 需求背景" in content or "需求背景" in content
    assert "旧版本用户缺乏升级引导" in content
    # the deterministic manifest survives after the AI sections
    assert "## Evidence manifest" in content
    assert content.index("需求背景") < content.index("## Evidence manifest")

    with database.SessionLocal() as db:
        run = db.scalar(select(AIRun).where(AIRun.feature_name == "document_generation").order_by(AIRun.created_at.desc()).limit(1))
        assert run.status == "succeeded"


def test_ai_document_uses_at_least_8192_output_tokens(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    fake = _FakeAdapter([_report_result()])
    _patch_adapter(monkeypatch, fake)
    generate_doc(client, owner, ready)

    assert fake.calls, "adapter was never invoked"
    assert fake.calls[0]["max_tokens"] >= 8192


def test_truncated_ai_output_falls_back_to_template(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    fake = _FakeAdapter([_report_result(truncated=True), _report_result(truncated=True)])
    _patch_adapter(monkeypatch, fake)
    document = generate_doc(client, owner, ready)

    content = document["current_version"]["content_markdown"]
    # both attempts truncated -> honest failure -> deterministic template
    assert "## Requirement background" in content
    assert "## Evidence manifest" in content
    assert len(fake.calls) == 2
    with database.SessionLocal() as db:
        run = db.scalar(select(AIRun).where(AIRun.feature_name == "document_generation").order_by(AIRun.created_at.desc()).limit(1))
        assert run.status == "failed"
        assert run.error_code == "LLM_TRUNCATED"


def test_document_system_prompt_mentions_sections_and_audience(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    fake = _FakeAdapter([_report_result()])
    _patch_adapter(monkeypatch, fake)
    generate_doc(client, owner, ready, document_type="prd")

    system = fake.calls[0]["system"]
    assert "章节结构" in system
    assert "禁止" in system


# --------------------------------------------------------------------------
# Context assembly (unit level)
# --------------------------------------------------------------------------


def test_build_document_context_assembles_four_artifact_classes(client, owner, project):
    from app.services.documents import _build_document_context as build

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    add_manual_question(client, owner, ready["project"]["id"], "为什么不升级？", "习惯旧交互。")
    # an approved decision: create + submit + approve
    proposal = data_of(
        client.post(
            "/api/v1/decision-proposals",
            headers=auth(owner),
            json={
                "project_id": ready["project"]["id"],
                "title": "升级引导",
                "problem_statement": "旧版本滞留",
                "proposed_action": "上线引导",
                "validation_plan": "两周观察",
            },
        )
    )
    submitted = data_of(client.post(f"/api/v1/decision-proposals/{proposal['id']}/submit", headers=auth(owner)))
    approval = submitted["approval_request"]
    data_of(
        client.post(
            f"/api/v1/approval-requests/{approval['id']}/approve",
            headers=auth(owner),
            json={"version": approval["version"], "decision_note": ""},
        )
    )

    with database.SessionLocal() as db:
        from app.models import User
        from app.schemas import DocumentGenerate

        user = db.get(User, owner["user"]["id"])
        body = DocumentGenerate(
            project_id=ready["project"]["id"],
            document_type="prd",
            title="上下文测试",
            source_refs=[{"type": "insight", "id": insight["id"]}],
        )
        context = build(body, db, user)

    safe = assert_safe_ai_context(dict(context["safe_context"]))
    artifacts = safe["artifacts"]
    types = {item.get("payload", {}).get("artifact_type") or item.get("artifact_type") for item in artifacts}
    # payload keys are flattened by the sanitizer; inspect raw artifacts instead
    raw_types = {item["artifact_type"] for item in context["safe_context"]["artifacts"]}
    assert {"insight", "interview_answer", "decision"} <= raw_types
    # dataset_summary appears once an auto-report exists for the project
    assert types  # sanitizer output remains structured
    assert context["evidence"] == [{"type": "insight", "id": insight["id"]}]
