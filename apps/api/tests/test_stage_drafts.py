"""Stage 9/10 draft contracts and the Copilot insight injection.

The endpoints must speak the shape the stage pages render: a problem draft
carrying ``title/statement/impact_scope`` and solution drafts whose ``effort``
is one of S/M/L.  Everything here runs without a provider key (conftest clears
it), so the assertions pin the degraded path a self-hosted user actually sees:
the call must succeed, be recorded, and return the stage-specific empty draft
-- never a 500.
"""

from __future__ import annotations

from conftest import auth, data_of, upload

from app import db as database
from app.models import AIRun


def make_insight(client, user, project_id: str, version_id: str, title: str, status: str = "draft") -> dict:
    """Create an insight; ``confirmed`` requires non-empty in-scope evidence."""

    return data_of(
        client.post(
            "/api/v1/insights",
            headers=auth(user),
            json={
                "project_id": project_id,
                "title": title,
                "content": f"{title} 的正文。",
                "insight_type": "fact",
                "confidence": "high",
                "evidence": [{"type": "dataset_version", "id": version_id}],
                "status": status,
            },
        )
    )


def frame_problem(client, user, project_id: str, insight_ids: list[str]):
    return client.post(
        "/api/v1/ai/frame-problem",
        headers=auth(user),
        json={"project_id": project_id, "insight_ids": insight_ids},
    )


def propose_solutions(client, user, problem_id: str):
    return client.post(
        "/api/v1/ai/propose-solutions",
        headers=auth(user),
        json={"problem_id": problem_id, "option_count": 3},
    )


# --------------------------------------------------------------------------
# /ai/frame-problem (stage 9)
# --------------------------------------------------------------------------


def test_frame_problem_requires_at_least_one_insight(client, owner, project):
    response = frame_problem(client, owner, project["id"], [])
    assert response.status_code == 422


def test_frame_problem_rejects_insights_from_another_project(client, owner, ready_dataset, project_factory):
    confirmed = make_insight(
        client,
        owner,
        ready_dataset["project"]["id"],
        ready_dataset["version_id"],
        "Confirmed insight",
        status="confirmed",
    )
    other_project = project_factory(owner)
    response = frame_problem(client, owner, other_project["id"], [confirmed["id"]])
    assert response.status_code == 422


def test_frame_problem_forbids_viewers(client, owner, viewer, project, ready_dataset):
    insight = make_insight(client, owner, project["id"], ready_dataset["version_id"], "Viewer target")
    response = frame_problem(client, viewer, project["id"], [insight["id"]])
    assert response.status_code == 403


def test_frame_problem_degrades_to_a_structured_empty_draft_without_a_key(client, owner, project, ready_dataset):
    insight = make_insight(client, owner, project["id"], ready_dataset["version_id"], "Activation drop")
    response = frame_problem(client, owner, project["id"], [insight["id"]])
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["status"] == "not_configured"
    assert payload["draft"] is True
    assert payload["provider"] == "deepseek"
    for key in ("title", "statement", "impact_scope", "limitations"):
        assert key in payload["output"], f"missing problem draft key {key}"
    assert payload["source_insight_ids"] == [insight["id"]]


# --------------------------------------------------------------------------
# /ai/propose-solutions (stage 10)
# --------------------------------------------------------------------------


def test_propose_solutions_unknown_problem_returns_404(client, owner):
    response = propose_solutions(client, owner, "no-such-problem")
    assert response.status_code == 404


def test_propose_solutions_degrades_with_empty_options_and_editors_can_call(client, owner, editor, problem):
    response = propose_solutions(client, owner, problem["id"])
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["status"] == "not_configured"
    assert payload["output"]["options"] == []

    editor_response = propose_solutions(client, editor, problem["id"])
    assert editor_response.status_code == 200, editor_response.text
    assert data_of(editor_response)["status"] == "not_configured"


# --------------------------------------------------------------------------
# Copilot context: server-side confirmed insights (WP4)
# --------------------------------------------------------------------------


def test_copilot_message_injects_only_confirmed_insights_of_the_session_project(
    client, owner, ready_dataset, project_factory
):
    project_id = ready_dataset["project"]["id"]
    make_insight(client, owner, project_id, ready_dataset["version_id"], "Confirmed insight", status="confirmed")
    make_insight(client, owner, project_id, ready_dataset["version_id"], "Draft insight")

    # A confirmed insight in a different project must never leak in.
    other_project = project_factory(owner)
    other_upload = upload(client, owner, other_project["id"], "user_events.csv")
    make_insight(
        client,
        owner,
        other_project["id"],
        other_upload["version"]["id"],
        "Other project insight",
        status="confirmed",
    )

    session = data_of(
        client.post(
            "/api/v1/copilot/sessions",
            headers=auth(owner),
            json={"workspace_id": owner["workspace"]["id"], "project_id": project_id},
        )
    )
    sent = data_of(
        client.post(
            f"/api/v1/copilot/sessions/{session['id']}/messages",
            headers=auth(owner),
            json={
                "content": "这些洞察共同说明了什么？",
                # Client-supplied rows/ids must be ignored: the server picks
                # the insights itself.
                "context": {"insight_ids": ["ins-should-be-ignored"], "rows": [{"leak": True}]},
            },
        )
    )

    with database.SessionLocal() as db:
        run = db.get(AIRun, sent["run_id"])
        context = (run.input_summary_json or {}).get("context", {})
    titles = [item["title"] for item in context.get("insights", [])]
    assert titles == ["Confirmed insight"]


# --------------------------------------------------------------------------
# Batch 19: used_insight_ids intersection (frame-problem)
# --------------------------------------------------------------------------


class _FakeAdapter:
    configured = True

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def complete(self, *, messages, response_schema, request_metadata):
        self.calls.append({"system": messages[0].content, "user": messages[1].content})
        return self._results.pop(0)


def _patch(monkeypatch, fake: _FakeAdapter) -> None:
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: fake)


def _ok(structured: dict):
    from app.infrastructure.llm.deepseek import LlmResult

    return LlmResult(structured=structured, finish_reason="stop", prompt_tokens=100, completion_tokens=100)


def test_frame_problem_filters_hallucinated_insight_ids(client, owner, ready_dataset, monkeypatch):
    confirmed = make_insight(client, owner, ready_dataset["project"]["id"], ready_dataset["version_id"], "缺口 14.3%", status="confirmed")
    real_id = confirmed["id"]
    fake = _FakeAdapter(
        [
            _ok(
                {
                    "title": "移动端注册流失",
                    "statement": "移动端用户在注册中途流失。",
                    "impact_scope": "移动端新用户。",
                    "priority": "P1",
                    "limitations": ["样本有限"],
                    "used_insight_ids": [real_id, "hallucinated-id"],
                }
            )
        ]
    )
    _patch(monkeypatch, fake)

    payload = data_of(frame_problem(client, owner, ready_dataset["project"]["id"], [real_id]))
    # the hallucinated id is gone; the real id survives
    assert payload["used_insight_ids"] == [real_id]


def test_frame_problem_all_hallucinated_falls_back_to_all_caller_ids(client, owner, ready_dataset, monkeypatch):
    confirmed = make_insight(client, owner, ready_dataset["project"]["id"], ready_dataset["version_id"], "缺口", status="confirmed")
    fake = _FakeAdapter(
        [
            _ok(
                {
                    "title": "流失",
                    "statement": "流失。",
                    "impact_scope": "移动端。",
                    "priority": "P2",
                    "limitations": [],
                    "used_insight_ids": ["made-up-1", "made-up-2"],
                }
            )
        ]
    )
    _patch(monkeypatch, fake)

    payload = data_of(frame_problem(client, owner, ready_dataset["project"]["id"], [confirmed["id"]]))
    # the evidence chain must not break: fall back to every caller id
    assert payload["used_insight_ids"] == [confirmed["id"]]


# --------------------------------------------------------------------------
# Batch 19: recommended option normalization (propose-solutions)
# --------------------------------------------------------------------------


def test_propose_solutions_recommends_exactly_one_option(client, owner, problem, monkeypatch):
    fake = _FakeAdapter(
        [
            _ok(
                {
                    "options": [
                        {"title": "方案A", "approach": "做法A", "pros": ["快"], "cons": ["贵"], "effort": "S"},
                        {
                            "title": "方案B",
                            "approach": "做法B",
                            "pros": ["稳"],
                            "cons": ["慢"],
                            "effort": "M",
                            "recommended": True,
                            "recommendation_reason": "风险最低且成本可控。",
                        },
                        {
                            "title": "方案C",
                            "approach": "做法C",
                            "pros": ["全"],
                            "cons": ["复杂"],
                            "effort": "L",
                            "recommended": True,
                            "recommendation_reason": "另一个理由",
                        },
                    ],
                    "limitations": [],
                }
            )
        ]
    )
    _patch(monkeypatch, fake)

    payload = data_of(propose_solutions(client, owner, problem["id"]))
    options = payload["output"]["options"]
    recommended = [option for option in options if option["recommended"]]
    assert len(recommended) == 1, "exactly one option must stay recommended"
    assert recommended[0]["title"] == "方案B"
    assert recommended[0]["recommendation_reason"] == "风险最低且成本可控。"


def test_propose_solutions_zero_recommended_promotes_the_first(client, owner, problem, monkeypatch):
    fake = _FakeAdapter(
        [
            _ok(
                {
                    "options": [
                        {"title": "甲", "approach": "做法甲", "pros": [], "cons": [], "effort": "S"},
                        {"title": "乙", "approach": "做法乙", "pros": [], "cons": [], "effort": "M"},
                    ],
                    "limitations": [],
                }
            )
        ]
    )
    _patch(monkeypatch, fake)

    payload = data_of(propose_solutions(client, owner, problem["id"]))
    options = payload["output"]["options"]
    assert [option["recommended"] for option in options] == [True, False]
    assert options[0]["recommendation_reason"]


# --------------------------------------------------------------------------
# Batch 19: /ai/draft-decision (draft-only decision proposal)
# --------------------------------------------------------------------------


def _draft_decision(client, user, problem_id: str):
    return client.post("/api/v1/ai/draft-decision", headers=auth(user), json={"problem_id": problem_id})


def _prepare_decision_chain(client, owner, ready_dataset, problem):
    """Create + select a solution for the problem."""

    created = data_of(
        client.post(
            f"/api/v1/problems/{problem['id']}/solutions",
            headers=auth(owner),
            json={"title": "两步极简注册", "approach": "压缩表单并增加短信重发。", "effort": "M"},
        )
    )
    # select it (no siblings -> no reject reasons needed)
    return data_of(
        client.post(
            f"/api/v1/solutions/{created['id']}/select",
            headers=auth(owner),
            json={"reject_reasons": {}},
        )
    )


def test_draft_decision_requires_a_selected_solution(client, owner, project, ready_dataset):
    insight = make_insight(client, owner, project["id"], ready_dataset["version_id"], "缺口", status="confirmed")
    problem = data_of(
        client.post(
            "/api/v1/problems",
            headers=auth(owner),
            json={"project_id": project["id"], "title": "移动端注册流失", "statement": "移动端注册中途流失。", "priority": "P1"},
        )
    )
    with database.SessionLocal() as db:
        from app.models import ProductProblem

        db.get(ProductProblem, problem["id"]).source_insight_ids = [insight["id"]]
        db.commit()
    response = _draft_decision(client, owner, problem["id"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SOLUTION_NOT_SELECTED"


def test_draft_decision_degrades_without_a_key(client, owner, project, ready_dataset):
    problem = data_of(
        client.post(
            "/api/v1/problems",
            headers=auth(owner),
            json={"project_id": project["id"], "title": "注册流失", "statement": "移动端注册中途流失。", "priority": "P1"},
        )
    )
    _prepare_decision_chain(client, owner, ready_dataset, problem)

    response = _draft_decision(client, owner, problem["id"])
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["status"] == "not_configured"
    for key in ("title", "problem_statement", "proposed_action", "expected_impact", "risk_summary", "validation_plan"):
        assert key in payload["output"]
    assert payload["output"]["title"] == ""


def test_draft_decision_fills_six_fields_from_the_selected_solution(client, owner, project, ready_dataset, monkeypatch):
    problem = data_of(
        client.post(
            "/api/v1/problems",
            headers=auth(owner),
            json={"project_id": project["id"], "title": "注册流失", "statement": "移动端注册中途流失。", "priority": "P1"},
        )
    )
    solution = _prepare_decision_chain(client, owner, ready_dataset, problem)

    fake = _FakeAdapter(
        [
            _ok(
                {
                    "title": "移动端两步注册与短信重发上线",
                    "problem_statement": "移动端注册中途流失（缺口 14.3%），主因是表单过长与验证码不可达。",
                    "proposed_action": "上线两步注册与短信重发：week_start 数据版本上按周观察 register_success 转化，验证码重发率同步监控。",
                    "expected_impact": "移动端注册成功率 85.7% -> 92%（两周内）。",
                    "risk_summary": "短信通道成本上升；邮箱降级路径需要文案。",
                    "validation_plan": "用 weekly_metrics 数据版本，观察 register_success/8 周转化率，两周内提升 5 个百分点算达标。",
                }
            )
        ]
    )
    _patch(monkeypatch, fake)

    payload = data_of(_draft_decision(client, owner, problem["id"]))
    assert payload["status"] == "succeeded"
    output = payload["output"]
    assert output["title"].startswith("移动端")
    # validation plan is data-grounded (these strings come from the fake)
    assert "register_success" in output["validation_plan"]
    assert "两周" in output["validation_plan"]
    # the selected solution id is echoed for traceability
    assert payload["solution_id"] == solution["id"]


def test_draft_decision_forbids_viewers(client, owner, viewer, project, ready_dataset):
    problem = data_of(
        client.post(
            "/api/v1/problems",
            headers=auth(owner),
            json={"project_id": project["id"], "title": "注册流失", "statement": "移动端注册中途流失。", "priority": "P1"},
        )
    )
    assert _draft_decision(client, viewer, problem["id"]).status_code == 403


def test_draft_decision_unknown_problem_returns_404(client, owner):
    response = client.post(
        "/api/v1/ai/draft-decision",
        headers=auth(owner),
        json={"problem_id": "no-such-problem"},
    )
    assert response.status_code == 404
