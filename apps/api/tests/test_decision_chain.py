"""Stages 6-11: the chain from a confirmed insight to a selected solution.

This is where the product's central claim lives.  Every downstream artifact
must trace back to data, so the suite asserts the two rules that enforce it:
a confirmed insight needs evidence, and choosing one solution requires a
written reason for every option that lost.
"""

from __future__ import annotations

from conftest import auth, data_of, error_of


def _insight(client, token, project_id, **overrides):
    body = {
        "project_id": project_id,
        "title": "Checkout drop-off concentrates on mobile",
        "insight_type": "fact",
        "content": "Mobile completion trails desktop by 18 points across the last four weeks.",
        "confidence": "high",
    }
    body.update(overrides)
    return client.post("/api/v1/insights", json=body, headers=auth(token))


def test_insight_starts_as_a_draft(client, owner, project):
    """Stage 6 output is a proposal, not a conclusion."""

    insight = data_of(_insight(client, owner, project["id"]))
    assert insight["status"] == "draft"


def test_confirming_an_insight_without_evidence_is_refused(client, owner, project):
    """The core anti-hallucination rule: no evidence, no confirmed status."""

    insight = data_of(_insight(client, owner, project["id"]))
    response = client.patch(
        f"/api/v1/insights/{insight['id']}",
        json={"status": "confirmed"},
        headers=auth(owner),
    )
    assert response.status_code in {400, 422}
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_confirming_an_insight_with_evidence_succeeds(client, owner, project, evidence):
    """The same transition is allowed once the insight cites its source."""

    insight = data_of(_insight(client, owner, project["id"]))
    confirmed = data_of(
        client.patch(
            f"/api/v1/insights/{insight['id']}",
            json={"status": "confirmed", "evidence": evidence},
            headers=auth(owner),
        )
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["evidence_json"] == evidence


def test_problem_rejects_an_insight_from_another_project(client, owner, project, project_factory):
    """Stage 9 traceability: a problem cannot cite a foreign project's insight."""

    foreign = data_of(_insight(client, owner, project_factory(owner)["id"]))
    response = client.post(
        "/api/v1/problems",
        json={
            "project_id": project["id"],
            "title": "Mobile checkout is losing users",
            "statement": "Users abandon the mobile checkout at the payment step.",
            "source_insight_ids": [foreign["id"]],
        },
        headers=auth(owner),
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_selecting_a_solution_requires_rejecting_the_others_with_reasons(client, owner, problem):
    """Stage 10-11: a decision record that keeps only the winner cannot
    explain itself later, so the losing rationale is mandatory."""

    winner = data_of(_solution(client, owner, problem["id"], title="Rebuild the payment step"))
    loser = data_of(_solution(client, owner, problem["id"], title="Add a progress indicator"))

    refused = client.post(
        f"/api/v1/solutions/{winner['id']}/select",
        json={"reject_reasons": {}},
        headers=auth(owner),
    )
    assert refused.status_code == 422
    detail = error_of(refused)
    assert detail["code"] == "VALIDATION_ERROR"
    assert loser["id"] in detail.get("details", {}).get("missing_reject_reasons", [])

    selected = data_of(
        client.post(
            f"/api/v1/solutions/{winner['id']}/select",
            json={"reject_reasons": {loser["id"]: "Does not address the payment failure itself."}},
            headers=auth(owner),
        )
    )
    assert selected["status"] == "selected"
    rejected = {row["id"]: row for row in selected["rejected"]}
    assert rejected[loser["id"]]["status"] == "rejected"
    assert rejected[loser["id"]]["reject_reason"]


def _solution(client, token, problem_id, **overrides):
    body = {
        "title": "Rebuild the payment step",
        "approach": "Replace the third-party widget with an inline form.",
        "effort": "M",
    }
    body.update(overrides)
    return client.post(f"/api/v1/problems/{problem_id}/solutions", json=body, headers=auth(token))


def _proposal(client, token, project_id, **overrides):
    body = {
        "project_id": project_id,
        "title": "Rebuild the mobile payment step",
        "problem_statement": "Mobile users abandon checkout at the payment step.",
        "proposed_action": "Replace the embedded widget with an inline form.",
        "validation_plan": "Compare mobile completion rate four weeks after release.",
        "priority": "P1",
    }
    body.update(overrides)
    return client.post("/api/v1/decision-proposals", json=body, headers=auth(token))


# --------------------------------------------------------------------------
# stage 11: a decision reaches "approved" only through a review
# --------------------------------------------------------------------------


def test_decision_proposal_starts_as_draft(client, owner, project):
    assert data_of(_proposal(client, owner, project["id"]))["status"] == "draft"


def test_submitting_a_decision_opens_an_approval_request(client, owner, project):
    """An author cannot move their own proposal straight to approved."""

    proposal = data_of(_proposal(client, owner, project["id"]))
    submitted = data_of(
        client.post(f"/api/v1/decision-proposals/{proposal['id']}/submit", headers=auth(owner))
    )
    assert submitted["proposal"]["status"] == "pending_approval"
    assert submitted["approval_request"]["status"] == "pending"


def test_a_decision_cannot_be_submitted_twice(client, owner, project):
    proposal = data_of(_proposal(client, owner, project["id"]))
    client.post(f"/api/v1/decision-proposals/{proposal['id']}/submit", headers=auth(owner))
    again = client.post(f"/api/v1/decision-proposals/{proposal['id']}/submit", headers=auth(owner))
    assert again.status_code == 409
    assert error_of(again)["code"] == "INVALID_STATE"


def test_approval_moves_the_proposal_to_approved(client, owner, project):
    proposal = data_of(_proposal(client, owner, project["id"]))
    submitted = data_of(
        client.post(f"/api/v1/decision-proposals/{proposal['id']}/submit", headers=auth(owner))
    )
    request = submitted["approval_request"]
    decided = data_of(
        client.post(
            f"/api/v1/approval-requests/{request['id']}/approve",
            json={"version": request["version"], "decision_note": "Impact is worth the effort."},
            headers=auth(owner),
        )
    )
    assert decided["approval_request"]["status"] == "approved"
    assert decided["target"]["status"] == "approved"


def test_rejection_requires_a_written_reason(client, owner, project):
    """A rejected decision must record why, or the audit trail is useless."""

    proposal = data_of(_proposal(client, owner, project["id"]))
    request = data_of(
        client.post(f"/api/v1/decision-proposals/{proposal['id']}/submit", headers=auth(owner))
    )["approval_request"]

    blank = client.post(
        f"/api/v1/approval-requests/{request['id']}/reject",
        json={"version": request["version"], "decision_note": "   "},
        headers=auth(owner),
    )
    assert blank.status_code == 400
    assert error_of(blank)["code"] == "VALIDATION_ERROR"

    rejected = data_of(
        client.post(
            f"/api/v1/approval-requests/{request['id']}/reject",
            json={"version": request["version"], "decision_note": "Wait for the pricing test."},
            headers=auth(owner),
        )
    )
    assert rejected["target"]["status"] == "rejected"


def test_editing_a_proposal_invalidates_a_pending_approval(client, owner, project):
    """Optimistic locking: approving a proposal that changed after review
    would approve text nobody read."""

    proposal = data_of(_proposal(client, owner, project["id"]))
    request = data_of(
        client.post(f"/api/v1/decision-proposals/{proposal['id']}/submit", headers=auth(owner))
    )["approval_request"]

    client.patch(
        f"/api/v1/decision-proposals/{proposal['id']}",
        json={"proposed_action": "Rewritten after the request was opened."},
        headers=auth(owner),
    )
    stale = client.post(
        f"/api/v1/approval-requests/{request['id']}/approve",
        json={"version": request["version"], "decision_note": "Looks fine."},
        headers=auth(owner),
    )
    assert stale.status_code == 409
    assert error_of(stale)["code"] == "VERSION_CONFLICT"


def test_viewer_cannot_approve_a_decision(client, owner, viewer, project):
    request = data_of(
        client.post(
            f"/api/v1/decision-proposals/{data_of(_proposal(client, owner, project['id']))['id']}/submit",
            headers=auth(owner),
        )
    )["approval_request"]
    response = client.post(
        f"/api/v1/approval-requests/{request['id']}/approve",
        json={"version": request["version"], "decision_note": "ok"},
        headers=auth(viewer),
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# stage 12: the PRD is a versioned, exportable document
# --------------------------------------------------------------------------


def _document(client, token, project_id, **overrides):
    body = {
        "project_id": project_id,
        "document_type": "prd",
        "title": "Mobile checkout rebuild",
        "content_markdown": "# Mobile checkout rebuild\n\nInline payment form.\n",
    }
    body.update(overrides)
    return client.post("/api/v1/documents", json=body, headers=auth(token))


def test_document_creation_records_a_first_version(client, owner, project):
    document = data_of(_document(client, owner, project["id"]))
    assert document["current_version"] is not None
    assert document["current_version"]["version_number"] == 1


def test_new_document_version_supersedes_the_previous_one(client, owner, project):
    document = data_of(_document(client, owner, project["id"]))
    version = data_of(
        client.post(
            f"/api/v1/documents/{document['id']}/versions",
            json={"content_markdown": "# Rewritten\n\nSecond draft.\n"},
            headers=auth(owner),
        )
    )
    assert version["version_number"] == 2

    reloaded = data_of(client.get(f"/api/v1/documents/{document['id']}", headers=auth(owner)))
    assert reloaded["current_version"]["version_number"] == 2
    assert len(reloaded["versions"]) == 2


def test_document_version_rejects_a_non_sequential_number(client, owner, project):
    """An explicit version must follow the latest, or a concurrent edit
    would silently overwrite someone else's draft."""

    document = data_of(_document(client, owner, project["id"]))
    response = client.post(
        f"/api/v1/documents/{document['id']}/versions",
        json={"content_markdown": "# Skipped ahead\n", "version": 7},
        headers=auth(owner),
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "VERSION_CONFLICT"


def test_document_export_returns_the_current_markdown(client, owner, project):
    document = data_of(_document(client, owner, project["id"]))
    response = client.get(f"/api/v1/documents/{document['id']}/export", headers=auth(owner))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "Inline payment form" in response.text


def test_export_filename_is_sanitised(client, owner, project):
    """Titles are user input and reach a Content-Disposition header."""

    document = data_of(_document(client, owner, project["id"], title='../../etc/pa ss"wd'))
    response = client.get(f"/api/v1/documents/{document['id']}/export", headers=auth(owner))

    disposition = response.headers["content-disposition"]
    assert response.status_code == 200
    assert "/" not in disposition and '"wd' not in disposition


def test_outsider_cannot_export_a_foreign_document(client, owner, outsider, project):
    document = data_of(_document(client, owner, project["id"]))
    response = client.get(f"/api/v1/documents/{document['id']}/export", headers=auth(outsider))
    assert response.status_code in {403, 404}
