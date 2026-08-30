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
