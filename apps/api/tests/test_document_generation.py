"""AI-driven document generation: grounded context, degradation, fallback.

The job handler runs inside the request cycle under TestClient (background
tasks execute synchronously), so the endpoint exercises the full chain:
template v1 immediately, AI (or fallback) v2 after the job completes.  With
no provider key the degraded path must still deliver a complete deterministic
document; with a scripted adapter the AI path must produce Chinese sections
on top of the unchanged evidence manifest.
"""

from __future__ import annotations

import contextlib

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.ai_context import assert_safe_ai_context
from app.models import AIRun, DocumentVersion


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
    # Batch 17: exactly ONE version -- the job's fallback template with the
    # visible provenance/状态 sections.  No pre-written template shell exists.
    assert len(document["versions"]) == 1
    assert document["versions"][0]["version_number"] == 1
    assert document["versions"][0]["ai_status"] == "fallback"
    content = document["versions"][-1]["content_markdown"]
    assert "## 证据溯源" in content
    assert "## 草稿状态" in content

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


def _outline_result(sections: int = 3):
    from app.infrastructure.llm.deepseek import LlmResult

    return LlmResult(
        structured={
            "findings": [
                {"id": "finding-1", "title": "注册转化缺口 14.3%", "evidence_hint": "漏斗分析", "severity": "高"},
                {"id": "finding-2", "title": "新版本覆盖率 46.6%", "evidence_hint": "维度分布", "severity": "中"},
            ],
            "sections": [
                {"heading": f"第{i}节", "purpose": f"写透第{i}节的设计要点"} for i in range(1, sections + 1)
            ],
            "root_cause": "注册到成功的转化缺口集中在移动端，根因是升级引导缺失。",
        },
        finish_reason="stop",
        prompt_tokens=900,
        completion_tokens=600,
    )


def _section_result(index: int, content: str | None = None, heading: str | None = None):
    from app.infrastructure.llm.deepseek import LlmResult

    body = content or (f"第{index}节正文。" + "设计要点与数据依据充分展开，覆盖边界情况与坏例兜底。" * 12)
    return LlmResult(
        structured={"heading": heading or f"第{index}节", "content": body},
        finish_reason="stop",
        prompt_tokens=1200,
        completion_tokens=1500,
    )


def _bad_result():
    from app.infrastructure.llm.deepseek import LlmResult

    return LlmResult(content="not json at all", finish_reason="stop", prompt_tokens=50, completion_tokens=20)


def _patch_adapter(monkeypatch, fake: _FakeAdapter) -> None:
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: fake)


def test_ai_document_renders_chinese_sections_and_keeps_manifest(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]
    add_manual_question(client, owner, ready["project"]["id"], "为什么不升级？", "习惯旧交互。")

    fake = _FakeAdapter([_outline_result(3), _section_result(1), _section_result(2), _section_result(3)])
    _patch_adapter(monkeypatch, fake)

    document = generate_doc(client, owner, ready, document_type="prd", title="升级引导 PRD")
    content = document["current_version"]["content_markdown"]
    # every outlined section lands in the markdown
    assert "## 第1节" in content and "## 第2节" in content and "## 第3节" in content
    # the root cause doubles as the executive summary
    assert "升级引导缺失" in content
    # the deterministic manifest survives after the AI sections
    assert "## 证据溯源" in content
    assert content.index("第3节") < content.index("## 证据溯源")

    with database.SessionLocal() as db:
        workspace_filter = AIRun.workspace_id == owner["workspace"]["id"]
        outline_run = db.scalar(
            select(AIRun).where(workspace_filter, AIRun.feature_name == "document_outline").order_by(AIRun.created_at.desc()).limit(1)
        )
        assert outline_run is not None and outline_run.status == "succeeded"
        section_runs = db.scalars(
            select(AIRun).where(workspace_filter, AIRun.feature_name == "document_section")
        ).all()
        assert len(section_runs) == 3
        assert all(run.status == "succeeded" for run in section_runs)


def test_ai_document_uses_at_least_8192_output_tokens(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    fake = _FakeAdapter([_outline_result(2), _section_result(1), _section_result(2)])
    _patch_adapter(monkeypatch, fake)
    generate_doc(client, owner, ready)

    assert len(fake.calls) == 3, "1 outline + 2 section calls expected"
    # the outline rides the default ceiling; every section call raises its
    # first-attempt ceiling to 8192 and never beyond
    assert fake.calls[0]["max_tokens"] <= 8192
    assert all(call["max_tokens"] == 8192 for call in fake.calls[1:])


def test_truncated_ai_output_falls_back_to_template(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    fake = _FakeAdapter([_report_result(truncated=True)] * 4)
    _patch_adapter(monkeypatch, fake)
    document = generate_doc(client, owner, ready)

    content = document["current_version"]["content_markdown"]
    # outline truncated twice -> single pass truncated twice -> template
    assert "## 需求背景" in content
    assert "## 证据溯源" in content
    assert len(fake.calls) == 4
    with database.SessionLocal() as db:
        run = db.scalar(select(AIRun).where(AIRun.feature_name == "document_generation").order_by(AIRun.created_at.desc()).limit(1))
        assert run.status == "failed"
        assert run.error_code == "LLM_TRUNCATED"


def test_document_system_prompt_mentions_sections_and_audience(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    fake = _FakeAdapter([_outline_result(2), _section_result(1), _section_result(2)])
    _patch_adapter(monkeypatch, fake)
    generate_doc(client, owner, ready, document_type="prd")

    outline_system = fake.calls[0]["system"]
    assert "大纲" in outline_system
    assert "产品团队" in outline_system
    section_system = fake.calls[1]["system"]
    assert "第 1/2 节" in section_system
    assert "|目标|衡量指标|目标值|" in section_system
    assert "|编号|验收点|预期结果|" in section_system
    assert "badcase" in section_system
    assert "禁止编造数据" in section_system


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
    # batch 17: the decision chain rides as top-level keys for the two-pass prompts
    assert context["decision"]["problem_statement"] == "旧版本滞留"
    assert context["solution"] is None  # no selected solution in this fixture
    # dataset_summary appears once an auto-report exists for the project
    assert types  # sanitizer output remains structured
    assert context["evidence"] == [{"type": "insight", "id": insight["id"]}]


# --------------------------------------------------------------------------
# Find-or-create (batch 8): regenerating reuses the same document
# --------------------------------------------------------------------------


def test_regenerating_same_type_reuses_one_document(client, owner, project):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    first = generate_doc(client, owner, ready, title="第一次生成")
    second = generate_doc(client, owner, ready, title="第二次生成")

    assert first["id"] == second["id"], "same project+type must reuse the document"
    assert second["title"] == "第二次生成"
    # Batch 17: each generation writes exactly one final version.
    assert len(second["versions"]) == 2
    assert [row["version_number"] for row in second["versions"]] == [1, 2]


def test_generation_job_id_exposed_in_payload_across_lifecycle(client, owner, project):
    """Batch 16: the document payload carries the in-flight generation job id
    (null once terminal) so the delivery page can resume its poll."""

    from app.models import Job

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]
    generated = generate_doc(client, owner, ready)
    document_id = generated["id"]

    # Terminal (the inline job already finished): null.
    fetched = data_of(client.get(f"/api/v1/documents/{document_id}", headers=auth(owner)))
    assert fetched["generation_job_id"] is None

    # An in-flight job shows up on both detail and list payloads.
    with database.SessionLocal() as db:
        job = Job(
            workspace_id=owner["workspace"]["id"],
            job_type="document_generation",
            status="running",
            progress=30,
            current_step="AI 撰写文档",
            input_json={"document_id": document_id, "_actor_id": owner["user"]["id"]},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    fetched = data_of(client.get(f"/api/v1/documents/{document_id}", headers=auth(owner)))
    assert fetched["generation_job_id"] == job_id
    listed = data_of(client.get(f"/api/v1/documents?project_id={project['id']}", headers=auth(owner)))
    row = next(item for item in listed if item["id"] == document_id)
    assert row["generation_job_id"] == job_id

    with database.SessionLocal() as db:
        stored = db.get(Job, job_id)
        stored.status = "succeeded"
        db.commit()
    fetched = data_of(client.get(f"/api/v1/documents/{document_id}", headers=auth(owner)))
    assert fetched["generation_job_id"] is None


def test_route_creates_shell_only_and_job_writes_the_single_final_version(client, owner, project):
    """Batch 17 core: the route's response shows a version-less shell (the
    body serializes before the inline job), and the job writes the first and
    ONLY final version (v1)."""

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    response = client.post(
        "/api/v1/documents/generate",
        headers=auth(owner),
        json={
            "project_id": ready["project"]["id"],
            "document_type": "prd",
            "title": "唯一终版",
            "source_refs": [{"type": "insight", "id": ready["insight_id"]}],
        },
    )
    payload = data_of(response)
    document = payload["document"]
    # Pre-job serialization: shell only, no template version was pre-written.
    assert document["current_version"] is None
    assert document["versions"] == []
    assert payload["job"]["id"]

    # The inline job then lands exactly one final version.
    fetched = data_of(client.get(f"/api/v1/documents/{document['id']}", headers=auth(owner)))
    assert len(fetched["versions"]) == 1
    assert fetched["versions"][0]["version_number"] == 1
    assert fetched["current_version"]["id"] == fetched["versions"][0]["id"]
    assert fetched["status"] == "draft"


def test_failed_job_leaves_no_version_and_retry_recovers(client, owner, project):
    """A failed generation job leaves the shell version-less with
    status=generation_failed; regenerating recovers to a normal v1."""

    from app.models import Document as DocumentModel
    from app.models import Job
    from app.services.job_handlers import JobContext, _mark_document_failed, job_executor

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    # Shell + queued job whose actor no longer exists (handler raises before
    # any AI call or version write).  Run the handler directly to keep the
    # failure deterministic.
    with database.SessionLocal() as db:
        shell = DocumentModel(
            workspace_id=owner["workspace"]["id"],
            project_id=project["id"],
            document_type="retrospective",
            title="会失败的生成",
            status="draft",
            created_by=owner["user"]["id"],
        )
        db.add(shell)
        db.flush()
        job = Job(
            workspace_id=owner["workspace"]["id"],
            job_type="document_generation",
            status="queued",
            progress=0,
            current_step="queued",
            input_json={
                "document_id": shell.id,
                "project_id": project["id"],
                "document_type": "retrospective",
                "_actor_id": "missing-actor",
            },
        )
        db.add(job)
        db.commit()
        document_id, job_id = shell.id, job.id

    with database.SessionLocal() as db:
        context = JobContext(db, job_id)
        handler = job_executor._handlers["document_generation"].run
        # The real executor wrapper catches the raise and marks the job
        # failed; here we suppress and call the marker directly.
        with contextlib.suppress(Exception):
            handler(context)
        stored_job = db.get(Job, job_id)
        _mark_document_failed(db, stored_job, "NOT_FOUND", "Narrating user no longer exists")
        db.commit()

    with database.SessionLocal() as db:
        stored = db.get(DocumentModel, document_id)
        assert stored.status == "generation_failed"
        assert stored.current_version_id is None
        assert db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id)).all() == []

    # Retry through the route reuses the same shell and recovers to v1/draft.
    retry = generate_doc(client, owner, ready, document_type="retrospective", title="重试")
    assert retry["id"] == document_id
    assert len(retry["versions"]) == 1
    assert retry["versions"][0]["version_number"] == 1
    assert retry["status"] == "draft"


def test_outline_failure_falls_back_to_single_pass(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    # Outline call fails twice (retry), the single-pass fallback then succeeds.
    fake = _FakeAdapter([_bad_result(), _bad_result(), _report_result()])
    _patch_adapter(monkeypatch, fake)
    document = generate_doc(client, owner, ready, document_type="prd", title="单次回退")

    assert document["current_version"]["ai_status"] == "succeeded"
    content = document["current_version"]["content_markdown"]
    assert "需求背景" in content  # came from the single-pass _report_result
    assert len(fake.calls) == 3


def test_section_failure_degrades_that_section_only(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    # Outline ok; section 2 fails (bad output + retry), sections 1/3 succeed.
    fake = _FakeAdapter(
        [_outline_result(3), _section_result(1), _bad_result(), _bad_result(), _section_result(3)]
    )
    _patch_adapter(monkeypatch, fake)
    document = generate_doc(client, owner, ready, document_type="prd", title="局部降级")

    assert document["current_version"]["ai_status"] == "succeeded"
    content = document["current_version"]["content_markdown"]
    assert "## 第1节" in content and "## 第3节" in content
    assert "## 第2节" in content
    # the degraded section carries the outline purpose bullets
    assert "本节要点：写透第2节的设计要点" in content
    assert "相关发现：注册转化缺口 14.3%" in content
    assert len(fake.calls) == 5


def test_all_sections_failing_falls_back_to_template(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]

    # Outline ok; every section burns its retry (2 calls each); the final
    # single-pass attempt gets an exhausted adapter (IndexError -> LLM_ERROR).
    fake = _FakeAdapter([_outline_result(2)] + [_bad_result()] * 4)
    _patch_adapter(monkeypatch, fake)
    document = generate_doc(client, owner, ready, document_type="prd", title="全败回退")

    assert document["current_version"]["ai_status"] == "fallback"
    content = document["current_version"]["content_markdown"]
    assert "## 证据溯源" in content
    assert "## 第1节" not in content


def test_section_calls_carry_decision_axis_and_written_summary(client, owner, project, monkeypatch):
    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    ready["insight_id"] = insight["id"]
    add_manual_question(client, owner, ready["project"]["id"], "为什么不升级？", "习惯旧交互。")
    # approved decision (same chain as the context test)
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

    fake = _FakeAdapter([_outline_result(3), _section_result(1), _section_result(2), _section_result(3)])
    _patch_adapter(monkeypatch, fake)
    generate_doc(client, owner, ready, document_type="prd", title="决策主轴")

    # section 2+ must see the written summary of the previous section
    section_users = [call["user"] for call in fake.calls[1:]]
    assert "written_summary" in section_users[0]
    assert "第1节" in section_users[1], "second section sees the first section's summary"
    # the section prompt quotes the approved decision as the narrative axis
    assert "旧版本滞留" in fake.calls[1]["system"]
    with database.SessionLocal() as db:
        run = db.scalar(
            select(AIRun).where(AIRun.feature_name == "document_section").order_by(AIRun.created_at.desc()).limit(1)
        )
        context = run.input_summary_json["context"]
    assert "decision" in context and context["decision"]["proposed_action"] == "上线引导"
    assert "solution" in context  # None here -- no selected solution in this fixture
    assert "outline_findings" in context and context["outline_findings"]
