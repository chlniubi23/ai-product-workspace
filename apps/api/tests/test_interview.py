"""Stage-6 AI interview: rounds, answers, manual supplements, distillation.

No provider key in this suite, so generation and distillation must degrade to
a 200 with an honest ``not_configured`` status -- the interview continues with
manual rows, and nothing blocks the pipeline.  The CRUD paths (manual rows,
answers, skips) and the evidence bridge into ``insights`` run for real here.
"""

from __future__ import annotations

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.models import AIRun
from app.services.interview import _normalise_question_text


def add_manual(client, user, project_id: str, question_text: str = "为什么华北的事件量最高？", answer_text: str = "") -> dict:
    return data_of(
        client.post(
            "/api/v1/interview-questions",
            headers=auth(user),
            json={"project_id": project_id, "topic": "地域分布", "question_text": question_text, "answer_text": answer_text},
        )
    )


def start_round(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/interview/rounds", headers=auth(user))


# --------------------------------------------------------------------------
# Round generation -- degrades without a provider key
# --------------------------------------------------------------------------


def test_round_generation_degrades_without_a_provider_key(client, owner, project):
    response = start_round(client, owner, project["id"])
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["status"] == "not_configured"
    assert payload["questions"] == []
    assert payload["run_id"]


def test_round_generation_requires_editor(client, owner, viewer, project):
    assert start_round(client, viewer, project["id"]).status_code == 403


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
