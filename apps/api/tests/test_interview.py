"""Stage-6 AI interview: adaptive next-question, completion digest, manual
supplements, and the auto-persisting distillation.

No provider key in this suite, so the AI paths must degrade to a 200 with an
honest ``not_configured`` status -- the interview continues with manual rows
and nothing blocks the pipeline.  The CRUD paths (manual rows, answers,
skips), the dedup/cap logic, the completion digest and the distill
auto-persistence run for real here (AI paths via scripted adapters).
"""

from __future__ import annotations

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.models import AIRun, Project, User, Workspace
from app.services.interview import _normalise_question_text


def _run(coro):
    import anyio

    return anyio.run(lambda: coro)


def _user(db, owner) -> User:
    return db.get(User, owner["user"]["id"])


def add_manual(client, user, project_id: str, question_text: str = "为什么华北的事件量最高？", answer_text: str = "") -> dict:
    return data_of(
        client.post(
            "/api/v1/interview-questions",
            headers=auth(user),
            json={"project_id": project_id, "topic": "地域分布", "question_text": question_text, "answer_text": answer_text},
        )
    )


def next_question(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/interview/next-question", headers=auth(user))


def complete_interview(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/interview/complete", headers=auth(user))


# --------------------------------------------------------------------------
# Round generation -- degrades without a provider key
# --------------------------------------------------------------------------


def test_next_question_degrades_without_a_provider_key(client, owner, project):
    response = next_question(client, owner, project["id"])
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["status"] == "not_configured"
    # nothing was persisted
    listed = data_of(client.get("/api/v1/interview-questions", headers=auth(owner)))
    assert listed == []


def test_next_question_requires_editor(client, owner, viewer, project):
    assert next_question(client, viewer, project["id"]).status_code == 403


# --------------------------------------------------------------------------
# Manual supplements and the answer/skip lifecycle
# --------------------------------------------------------------------------


def test_manual_question_with_info_is_answered_immediately(client, owner, project):
    question = add_manual(client, owner, project["id"], answer_text="华北渠道活动多，活动期事件翻倍。")
    assert question["source"] == "manual"
    assert question["round_number"] == 0
    assert question["status"] == "answered"
    assert question["answered_at"]


def test_manual_question_without_info_stays_pending_then_answer_and_skip(client, owner, project):
    question = add_manual(client, owner, project["id"])
    assert question["status"] == "pending"

    answered = data_of(
        client.patch(
            f"/api/v1/interview-questions/{question['id']}",
            headers=auth(owner),
            json={"answer_text": "活动期事件翻倍。"},
        )
    )
    assert answered["status"] == "answered"

    skipped = add_manual(client, owner, project["id"], question_text="渠道 ROI 如何分摊？")
    marked = data_of(
        client.patch(
            f"/api/v1/interview-questions/{skipped['id']}",
            headers=auth(owner),
            json={"status": "skipped"},
        )
    )
    assert marked["status"] == "skipped"


def test_answering_without_text_is_rejected(client, owner, project):
    question = add_manual(client, owner, project["id"])
    response = client.patch(
        f"/api/v1/interview-questions/{question['id']}",
        headers=auth(owner),
        json={"status": "answered"},
    )
    assert response.status_code == 422


def test_question_update_is_scoped_to_the_project_editor(client, owner, viewer, outsider, project, project_factory):
    question = add_manual(client, owner, project["id"])
    assert (
        client.patch(
            f"/api/v1/interview-questions/{question['id']}",
            headers=auth(viewer),
            json={"status": "skipped"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/interview-questions/{question['id']}",
            headers=auth(outsider),
            json={"status": "skipped"},
        ).status_code
        in {403, 404}
    )
    # the question belongs to the first project, so its editor rights do not
    # extend to answering from a different project context either
    response = client.patch(
        f"/api/v1/interview-questions/{question['id']}",
        headers=auth(owner),
        json={"answer_text": "跨项目上下文回答"},
    )
    assert response.status_code == 200  # same owner, still the question's editor


def test_list_is_scoped_to_projects_the_caller_can_see(client, owner, outsider, project, project_factory):
    add_manual(client, owner, project["id"])
    other = project_factory(owner)
    add_manual(client, owner, other["id"], question_text="另一个项目的问题")

    mine = data_of(client.get("/api/v1/interview-questions", headers=auth(owner)))
    assert len(mine) == 2

    scoped = data_of(client.get(f"/api/v1/interview-questions?project_id={project['id']}", headers=auth(owner)))
    assert len(scoped) == 1

    outsider_view = data_of(client.get("/api/v1/interview-questions", headers=auth(outsider)))
    assert outsider_view == []


# --------------------------------------------------------------------------
# Distillation (stage 7 entry)
# --------------------------------------------------------------------------


def test_distill_degrades_without_a_key_and_records_the_run(client, owner, project, ready_dataset):
    add_manual(client, owner, project["id"], answer_text="活动期事件翻倍。")
    response = client.post(
        "/api/v1/ai/distill-interview",
        headers=auth(owner),
        json={"project_id": project["id"]},
    )
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["status"] == "not_configured"
    assert payload["draft"] is True
    for section in ("facts", "hypotheses", "recommendations", "limitations"):
        assert section in payload["output"]
    assert payload["interview_answer_count"] == 1

    with database.SessionLocal() as db:
        run = db.scalar(select(AIRun).where(AIRun.feature_name == "interview_distill").order_by(AIRun.created_at.desc()).limit(1))
        assert run is not None
        assert run.status == "not_configured"


def test_distill_requires_editor(client, viewer, project):
    response = client.post(
        "/api/v1/ai/distill-interview",
        headers=auth(viewer),
        json={"project_id": project["id"]},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Evidence bridge: interview answers can back a saved insight
# --------------------------------------------------------------------------


def test_insight_may_cite_an_interview_question_as_evidence(client, owner, project, ready_dataset):
    question = add_manual(client, owner, project["id"], answer_text="活动期事件翻倍。")
    created = data_of(
        client.post(
            "/api/v1/insights",
            headers=auth(owner),
            json={
                "project_id": project["id"],
                "title": "活动期事件量翻倍",
                "insight_type": "fact",
                "content": "渠道活动期间华北事件量翻倍。",
                "evidence": [{"type": "interview_question", "id": question["id"]}],
            },
        )
    )
    assert created["evidence_json"] == [{"type": "interview_question", "id": question["id"]}]


def test_fabricated_interview_evidence_is_rejected(client, owner, project):
    response = client.post(
        "/api/v1/insights",
        headers=auth(owner),
        json={
            "project_id": project["id"],
            "title": "伪造引用",
            "insight_type": "fact",
            "content": "引用不存在的采访。",
            "evidence": [{"type": "interview_question", "id": "no-such-question"}],
        },
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Dedup helper
# --------------------------------------------------------------------------


def test_normalise_question_text_is_punctuation_and_case_insensitive():
    assert _normalise_question_text("华北的事件量为何最高？") == _normalise_question_text("华北的事件量为何最高")
    assert _normalise_question_text("Why is retention DIPPING?") == _normalise_question_text("why is retention dipping")
    assert _normalise_question_text("？？？？") == ""


# --------------------------------------------------------------------------
# Truncation-aware retry in _run_ai_stage (fake adapter, no network)
# --------------------------------------------------------------------------


def _llm_result(content: str = "", finish_reason: str | None = "stop", structured: dict | None = None, tokens: int = 100):
    from app.infrastructure.llm.deepseek import LlmResult

    return LlmResult(
        content=content,
        finish_reason=finish_reason,
        prompt_tokens=tokens,
        completion_tokens=tokens,
        structured=structured,
    )


class _FakeAdapter:
    """Scripted DeepSeekAdapter replacement recording every call."""

    configured = True

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def complete(self, *, messages, response_schema, request_metadata):
        self.calls.append(
            {
                "system": messages[0].content,
                "user": messages[1].content,
                "max_tokens": request_metadata.max_tokens,
            }
        )
        return self._results.pop(0)


def _workspace_of(client, owner):
    return owner["workspace"]


def _db_session():
    from app import db as database

    return database.SessionLocal()


def _patch_adapter(monkeypatch, fake: _FakeAdapter) -> None:
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: fake)


GOOD_JSON = {
    "facts": [{"text": "f1", "evidence": ["e1"]}],
    "hypotheses": [{"text": "h1", "evidence": ["e1"]}],
    "recommendations": [{"text": "r1", "evidence": ["e1"]}],
    "limitations": ["l1"],
}


def test_truncated_then_success_retries_with_doubled_tokens(client, owner, project, monkeypatch):
    """finish_reason=length on attempt 1 triggers one retry with 2x max_tokens."""

    from app.services.interview import distill_interview

    fake = _FakeAdapter(
        [
            _llm_result(content='{"facts": [', finish_reason="length"),
            _llm_result(structured=GOOD_JSON, finish_reason="stop"),
        ]
    )
    _patch_adapter(monkeypatch, fake)
    with _db_session() as db:
        ws = db.get(Workspace, owner["workspace"]["id"])
        result = _run(distill_interview(db=db, user=_user(db, owner), workspace=ws, project=db.get(Project, project["id"])))

    assert result["status"] == "succeeded"
    assert len(fake.calls) == 2
    assert fake.calls[0]["max_tokens"] == 4096
    assert fake.calls[1]["max_tokens"] == 8192
    assert "禁止任何截断" in fake.calls[1]["system"]
    with _db_session() as db:
        run = db.get(AIRun, result["run_id"])
        meta = (run.input_summary_json or {}).get("provider_meta", {})
        assert meta.get("retried") is True
        assert meta.get("finish_reason") == "stop"


def test_truncated_twice_fails_with_llm_truncated(client, owner, project, monkeypatch):
    from app.services.interview import distill_interview

    fake = _FakeAdapter(
        [
            _llm_result(content='{"facts": [', finish_reason="length"),
            _llm_result(content='{"facts": [{"tex', finish_reason="length"),
        ]
    )
    _patch_adapter(monkeypatch, fake)
    with _db_session() as db:
        ws = db.get(Workspace, owner["workspace"]["id"])
        result = _run(distill_interview(db=db, user=_user(db, owner), workspace=ws, project=db.get(Project, project["id"])))

    assert result["status"] == "failed"
    assert result["error_code"] == "LLM_TRUNCATED"
    assert result["output"]["limitations"] == ["AI 输出过长被截断"]
    with _db_session() as db:
        run = db.get(AIRun, result["run_id"])
        assert (run.input_summary_json or {}).get("provider_meta", {}).get("finish_reason") == "length"


def test_unparseable_json_fails_with_invalid_ai_output(client, owner, project, monkeypatch):
    """A non-JSON payload with finish_reason=stop fails honestly after retry."""

    from app.services.interview import distill_interview

    fake = _FakeAdapter(
        [
            _llm_result(content="not json at all", finish_reason="stop"),
            _llm_result(content="still not json", finish_reason="stop"),
        ]
    )
    _patch_adapter(monkeypatch, fake)
    with _db_session() as db:
        ws = db.get(Workspace, owner["workspace"]["id"])
        result = _run(distill_interview(db=db, user=_user(db, owner), workspace=ws, project=db.get(Project, project["id"])))

    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_AI_OUTPUT"
    assert len(fake.calls) == 2


def test_distill_system_prompt_carries_size_limits(client, owner, project, monkeypatch):
    from app.services.interview import distill_interview

    fake = _FakeAdapter([_llm_result(structured=GOOD_JSON, finish_reason="stop")])
    _patch_adapter(monkeypatch, fake)
    with _db_session() as db:
        ws = db.get(Workspace, owner["workspace"]["id"])
        _run(distill_interview(db=db, user=_user(db, owner), workspace=ws, project=db.get(Project, project["id"])))

    system = fake.calls[0]["system"]
    assert "洞察条数由证据决定" in system
    assert "不超过 80 字" in system
    assert "evidence 只引 1 个" in system


# --------------------------------------------------------------------------
# _normalize_distill_evidence: typed-reference normalization (pure function)
# --------------------------------------------------------------------------


class _FakeQuestion:
    def __init__(self, id: str):
        self.id = id
        self.topic = "t"
        self.question_text = "q"


QID = "68723dd7-093a-4dbe-b6b1-bd43d0ecdbd4"
ARTID = "8a627b11-659f-47c2-8478-9c9cbb9794ba"


def _fake_questions():
    return [_FakeQuestion(QID)]


def _fake_artifacts():
    return [{"id": ARTID, "artifact_type": "eda", "title": "EDA", "payload_json": {}}]


def test_normalize_adds_missing_type_from_id_membership():
    from app.services.interview import _normalize_distill_evidence

    output = {"facts": [{"text": "f", "evidence": [{"id": QID}]}], "hypotheses": [], "recommendations": []}
    normalized = _normalize_distill_evidence(output, _fake_questions(), _fake_artifacts())
    assert normalized["facts"][0]["evidence"] == [{"type": "interview_question", "id": QID}]


def test_normalize_corrects_wrong_type_by_id_membership():
    from app.services.interview import _normalize_distill_evidence

    output = {
        "facts": [{"text": "f", "evidence": [{"type": "analysis_artifact", "id": QID}, {"type": "interview_question", "id": ARTID}]}],
        "hypotheses": [],
        "recommendations": [],
    }
    normalized = _normalize_distill_evidence(output, _fake_questions(), _fake_artifacts())
    assert normalized["facts"][0]["evidence"] == [
        {"type": "interview_question", "id": QID},
        {"type": "analysis_artifact", "id": ARTID},
    ]


def test_normalize_extracts_uuid_from_strings_and_drops_unknown():
    from app.services.interview import _normalize_distill_evidence

    output = {
        "facts": [
            {"text": "f", "evidence": [f"引用 {QID}", ARTID.upper(), {"id": "not-in-project"}, 42, None]},
        ],
        "hypotheses": [],
        "recommendations": [],
    }
    normalized = _normalize_distill_evidence(output, _fake_questions(), _fake_artifacts())
    assert normalized["facts"][0]["evidence"] == [
        {"type": "interview_question", "id": QID},
        {"type": "analysis_artifact", "id": ARTID},
    ]


def test_normalize_dedupes_and_falls_back_when_all_dropped():
    from app.services.interview import _normalize_distill_evidence

    output = {
        "facts": [{"text": "dup", "evidence": [{"id": QID}, QID, {"type": "interview_question", "id": QID}]}],
        "hypotheses": [{"text": "all-dropped", "evidence": [{"id": "unknown"}, "garbage"]}],
        "recommendations": [],
    }
    normalized = _normalize_distill_evidence(output, _fake_questions(), _fake_artifacts())
    assert normalized["facts"][0]["evidence"] == [{"type": "interview_question", "id": QID}]
    assert normalized["hypotheses"][0]["evidence"] == [{"type": "interview_question", "id": QID}]


def test_normalize_without_fallback_leaves_evidence_empty():
    from app.services.interview import _normalize_distill_evidence

    output = {"facts": [{"text": "f", "evidence": [{"id": "unknown"}]}], "hypotheses": [], "recommendations": []}
    normalized = _normalize_distill_evidence(output, [], [])
    assert normalized["facts"][0]["evidence"] == []




# --------------------------------------------------------------------------
# Batch 18: adaptive next-question -- scripted provider paths
# --------------------------------------------------------------------------


def _question_result(text: str, topic: str = "转化缺口", complete: bool = False, note: str = ""):
    from app.infrastructure.llm.deepseek import LlmResult

    return LlmResult(
        structured={
            "question_text": text,
            "topic": topic,
            "rationale": "报告发现中最重要的未澄清点。",
            "interview_complete": complete,
            "completion_note": note,
        },
        finish_reason="stop",
        prompt_tokens=100,
        completion_tokens=80,
    )


def _summary_result():
    from app.infrastructure.llm.deepseek import LlmResult

    return LlmResult(
        structured={
            "collected": ["围绕注册转化缺口收集到验证码可达性是首要疑问"],
            "gaps": ["样本周期外的季节性影响未覆盖"],
            "ready_for": "可直接蒸馏注册转化的结论；留存结论建议先用数据验证。",
        },
        finish_reason="stop",
        prompt_tokens=100,
        completion_tokens=120,
    )


def test_next_question_persists_one_pending_question(client, owner, project, monkeypatch):
    fake = _FakeAdapter([_question_result("移动端验证码的到达率是多少？")])
    _patch_adapter(monkeypatch, fake)

    payload = data_of(next_question(client, owner, project["id"]))
    assert payload["status"] == "ok"
    question = payload["question"]
    assert question["status"] == "pending"
    assert question["source"] == "ai"
    assert question["round_number"] == 1
    assert len(fake.calls) == 1


def test_next_question_ai_judged_complete_persists_nothing(client, owner, project, monkeypatch):
    fake = _FakeAdapter([_question_result("", complete=True, note="关键信息已收集足够，建议直接进入洞察蒸馏。")])
    _patch_adapter(monkeypatch, fake)

    payload = data_of(next_question(client, owner, project["id"]))
    assert payload == {
        "status": "complete",
        "reason": "ai_judged",
        "note": "关键信息已收集足够，建议直接进入洞察蒸馏。",
    }
    listed = data_of(client.get("/api/v1/interview-questions", headers=auth(owner)))
    assert listed == []


def test_next_question_caps_at_ten_without_provider_calls(client, owner, project):
    with _db_session() as db:
        from app.models import InterviewQuestion

        for i in range(10):
            db.add(
                InterviewQuestion(
                    workspace_id=project["workspace_id"],
                    project_id=project["id"],
                    round_number=i + 1,
                    topic="t",
                    question_text=f"问题 {i}",
                    status="answered",
                    answer_text="a",
                    source="ai",
                    created_by=owner["user"]["id"],
                )
            )
        db.commit()

    payload = data_of(next_question(client, owner, project["id"]))
    assert payload["status"] == "complete"
    assert payload["reason"] == "cap_reached"
    assert "10" in payload["note"]


def test_next_question_duplicate_retries_once_then_completes(client, owner, project, monkeypatch):
    # 3 provider calls total: ask -> duplicate (retry) -> duplicate (give up)
    fake = _FakeAdapter([_question_result("验证码到达率如何？")] * 3)
    _patch_adapter(monkeypatch, fake)

    first = data_of(next_question(client, owner, project["id"]))
    assert first["status"] == "ok"

    second = data_of(next_question(client, owner, project["id"]))
    assert second["status"] == "complete"
    assert second["reason"] == "no_new_question"
    assert len(fake.calls) == 3
    # the retry attempt saw the first question as asked_question context
    assert "asked_question" in fake.calls[1]["user"]


def test_interview_complete_generates_and_persists_digest(client, owner, project, monkeypatch):
    add_manual(client, owner, project["id"], answer_text="验证码收不到是主要原因。")
    fake = _FakeAdapter([_summary_result()])
    _patch_adapter(monkeypatch, fake)

    payload = data_of(complete_interview(client, owner, project["id"]))
    assert payload["status"] == "ok"
    assert payload["summary"]["collected"]
    assert payload["summary"]["ready_for"]

    from app.models import InterviewSummary

    with _db_session() as db:
        row = db.scalar(select(InterviewSummary).where(InterviewSummary.project_id == project["id"]))
        assert row is not None
        assert "collected" in row.summary

    # idempotent: a second call overwrites, never stacks
    fake2 = _FakeAdapter([_summary_result()])
    _patch_adapter(monkeypatch, fake2)
    data_of(complete_interview(client, owner, project["id"]))
    with _db_session() as db:
        from sqlalchemy import func

        count = db.scalar(
            select(func.count()).select_from(InterviewSummary).where(InterviewSummary.project_id == project["id"])
        )
        assert count == 1


def test_interview_complete_requires_an_answered_question(client, owner, project):
    add_manual(client, owner, project["id"])  # pending, no answer
    assert complete_interview(client, owner, project["id"]).status_code == 400


# --------------------------------------------------------------------------
# Batch 18: distillation auto-persists draft insights
# --------------------------------------------------------------------------


def _distill_claims(qid: str, texts: tuple[str, ...]):
    def claim(text: str):
        return {"text": text, "evidence": [{"type": "interview_question", "id": qid}]}

    sections = {"facts": [], "hypotheses": [], "recommendations": []}
    order = ["facts", "hypotheses", "recommendations"]
    for index, text in enumerate(texts):
        sections[order[index % 3]].append(claim(text))
    sections["limitations"] = ["样本周期较短"]
    return _llm_result(structured=sections, finish_reason="stop")


def test_distill_persists_draft_insights_with_evidence(client, owner, project, monkeypatch):
    question = add_manual(client, owner, project["id"], answer_text="验证码收不到是主要原因。")
    fake = _FakeAdapter([_distill_claims(question["id"], ("验证码不可达是注册流失主因", "两步注册可降低流失", "增加短信重发"))])
    _patch_adapter(monkeypatch, fake)

    payload = data_of(
        client.post("/api/v1/ai/distill-interview", headers=auth(owner), json={"project_id": project["id"]})
    )
    assert payload["status"] == "succeeded"
    assert len(payload["created"]) == 3
    assert payload["discarded_claims"] == 0
    created = data_of(client.get(f"/api/v1/insights?project_id={project['id']}&page_size=100", headers=auth(owner)))
    drafts = [row for row in created if row["status"] == "draft"]
    assert len(drafts) == 3
    assert all(row["evidence_json"] for row in drafts)
    run_id = payload["run_id"]
    assert all(row["ai_run_id"] == run_id for row in drafts)


def test_distill_without_resolvable_evidence_discards_the_claim(client, owner, project, monkeypatch):
    """No interview questions exist, so the normalization fallback cannot
    rescue fabricated references -- the claim is discarded, not persisted."""

    fake = _FakeAdapter(
        [
            _llm_result(
                structured={
                    "facts": [{"text": "伪造引用的结论", "evidence": [{"type": "interview_question", "id": "no-such"}]}],
                    "hypotheses": [],
                    "recommendations": [],
                    "limitations": [],
                },
                finish_reason="stop",
            )
        ]
    )
    _patch_adapter(monkeypatch, fake)

    payload = data_of(
        client.post("/api/v1/ai/distill-interview", headers=auth(owner), json={"project_id": project["id"]})
    )
    assert payload["status"] == "succeeded"
    assert payload["created"] == []
    assert payload["discarded_claims"] == 1
    created = data_of(client.get(f"/api/v1/insights?project_id={project['id']}&page_size=100", headers=auth(owner)))
    assert created == []


def test_redisill_refreshes_drafts_and_keeps_adjudicated(client, owner, project, monkeypatch):
    question = add_manual(client, owner, project["id"], answer_text="验证码收不到。")
    fake = _FakeAdapter([_distill_claims(question["id"], ("结论一", "结论二", "结论三"))])
    _patch_adapter(monkeypatch, fake)
    data_of(client.post("/api/v1/ai/distill-interview", headers=auth(owner), json={"project_id": project["id"]}))

    drafts = data_of(client.get(f"/api/v1/insights?project_id={project['id']}&page_size=100", headers=auth(owner)))
    keep_id = drafts[0]["id"]
    drop_id = drafts[1]["id"]
    # user adjudicates: confirm one (with its evidence), reject another
    confirmed = data_of(
        client.patch(
            f"/api/v1/insights/{keep_id}",
            headers=auth(owner),
            json={"status": "confirmed", "evidence": drafts[0]["evidence_json"]},
        )
    )
    assert confirmed["status"] == "confirmed"
    rejected = data_of(
        client.patch(
            f"/api/v1/insights/{drop_id}",
            headers=auth(owner),
            json={"status": "rejected"},
        )
    )
    assert rejected["status"] == "rejected"

    # re-distill with new claims: drafts refresh, adjudicated survive
    fake2 = _FakeAdapter([_distill_claims(question["id"], ("新结论一", "新结论二"))])
    _patch_adapter(monkeypatch, fake2)
    payload = data_of(
        client.post("/api/v1/ai/distill-interview", headers=auth(owner), json={"project_id": project["id"]})
    )
    assert len(payload["created"]) == 2

    rows = data_of(client.get(f"/api/v1/insights?project_id={project['id']}&page_size=100", headers=auth(owner)))
    statuses = {row["id"]: row["status"] for row in rows}
    assert statuses[keep_id] == "confirmed"
    assert statuses[drop_id] == "rejected"
    survivors = [row for row in rows if row["status"] == "draft"]
    assert {row["title"] for row in survivors} == {"新结论一", "新结论二"}
