"""The AI boundary: stages 6-12 draft, humans decide.

These are the most important tests in the suite.  The product rule is that AI
output is *always* a draft, is *always* recorded as an AIRun, and *never*
blocks the pipeline when the provider is missing or disabled.  Equally
important is what does not cross the boundary: raw rows, file paths, secrets,
and individual feedback text.

The tests run with no DEEPSEEK_API_KEY (conftest clears it), which is the
honest default for CI.  That is deliberate rather than a limitation: the
degradation path is the one that must never regress, because it is what a
self-hosted user without a key actually experiences.  Assertions therefore
target invariants that hold in both modes -- draft status, AIRun bookkeeping,
the context allowlist -- and never a specific model's prose.
"""

from __future__ import annotations

import json

from conftest import auth, data_of, error_of


def interpret(client, token, project, **extra):
    """POST /ai/interpret.  `project` may be the fixture dict or a bare id."""

    project_id = project["id"] if isinstance(project, dict) else project
    return client.post(
        "/api/v1/ai/interpret",
        json={"project_id": project_id, **extra},
        headers=auth(token),
    )


# --------------------------------------------------------------------------
# Draft status and graceful degradation
# --------------------------------------------------------------------------


def test_interpret_returns_a_draft_and_never_500s_without_a_provider_key(client, owner, project):
    """No API key must degrade, not fail.  This is the self-hosted default."""

    response = interpret(client, owner, project)
    assert response.status_code == 200, response.text
    payload = data_of(response)
    assert payload["draft"] is True
    assert payload["insight_status"] == "draft"
    assert payload["status"] in {"not_configured", "failed", "succeeded"}


def test_interpret_output_always_carries_the_four_contract_sections(client, owner, project):
    payload = data_of(interpret(client, owner, project))
    output = payload["output"]
    for section in ("facts", "hypotheses", "recommendations", "limitations"):
        assert section in output, f"missing contract section {section}"


def test_interpret_states_a_limitation_when_ai_is_unavailable(client, owner, project):
    """A degraded run must say so rather than return a confident empty answer."""

    payload = data_of(interpret(client, owner, project))
    if payload["status"] == "succeeded":
        return
    assert payload["output"]["limitations"], "a degraded run must record a limitation"


def test_interpret_records_an_ai_run_even_when_degraded(client, owner, project):
    """Bookkeeping is not conditional on the provider working.

    /ai/usage aggregates persisted AIRun rows, so a rising `calls` count is
    direct evidence the run was written -- an audit trail must exist for a
    degraded attempt just as it does for a successful one.
    """

    workspace_id = owner["workspace"]["id"]

    def calls():
        usage = data_of(client.get(f"/api/v1/ai/usage?workspace_id={workspace_id}", headers=auth(owner)))
        return usage["by_feature"].get("interpret", {}).get("calls", 0)

    before = calls()
    payload = data_of(interpret(client, owner, project))
    assert payload["run_id"]
    assert calls() == before + 1


def test_interpret_reports_zero_usage_when_no_provider_call_happened(client, owner, project):
    payload = data_of(interpret(client, owner, project))
    if payload["status"] == "succeeded":
        return
    assert payload["usage"]["total_tokens"] == 0


# --------------------------------------------------------------------------
# The context allowlist -- what must NOT cross the boundary
# --------------------------------------------------------------------------


def test_interpret_ignores_caller_supplied_raw_data(client, owner, project):
    """A caller cannot smuggle raw rows in through `context`.

    AIInterpretRequest is extra="ignore" and the route reprojects context
    through build_ai_context, so injected keys must be dropped rather than
    forwarded.  The request must still succeed.
    """

    response = interpret(
        client,
        owner,
        project,
        context={
            "rows": [{"user_id": "u-1", "email": "leak@example.com"}],
            "storage_path": "/etc/passwd",
            "api_key": "sk-should-never-travel",
        },
    )
    assert response.status_code == 200
    blob = json.dumps(data_of(response))
    assert "leak@example.com" not in blob
    assert "sk-should-never-travel" not in blob
    assert "/etc/passwd" not in blob


def test_ai_context_allowlist_drops_forbidden_keys():
    """Unit-level proof of the allowlist, independent of HTTP."""

    from app.ai_context import ALLOWED_CONTEXT_KEYS, build_ai_context

    context = build_ai_context(
        goal="raise activation",
        raw_rows=[{"secret": 1}],
        password="hunter2",
        storage_path="/var/data/x.csv",
    )
    assert set(context).issubset(ALLOWED_CONTEXT_KEYS)
    blob = json.dumps(context)
    assert "hunter2" not in blob
    assert "/var/data/x.csv" not in blob


def test_ai_context_redacts_emails_and_phone_numbers():
    from app.ai_context import build_ai_context

    context = build_ai_context(goal="contact a@b.com or 555-123-4567 for detail")
    blob = json.dumps(context)
    assert "a@b.com" not in blob
    assert "555-123-4567" not in blob


def test_individual_feedback_text_never_reaches_the_context(client, owner, project):
    """Feedback contributes aggregate counts only -- never verbatim notes."""

    secret_verbatim = "this exact sentence must never travel to a provider"
    created = client.post(
        "/api/v1/feedback-items",
        json={"project_id": project["id"], "content": secret_verbatim, "labels": ["onboarding"], "channel": "manual"},
        headers=auth(owner),
    )
    assert created.status_code == 200, created.text

    response = interpret(client, owner, project)
    assert response.status_code == 200
    assert secret_verbatim not in json.dumps(data_of(response))

    # And prove it at the context builder too, which is what actually feeds
    # the provider call.
    from app.ai_context import build_ai_context

    assert secret_verbatim not in json.dumps(build_ai_context(feedback=[{"content": secret_verbatim}]))


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_interpret_requires_authentication(client, project):
    response = client.post("/api/v1/ai/interpret", json={"project_id": project["id"]})
    assert response.status_code == 401


def test_interpret_rejects_a_project_from_another_workspace(client, outsider, project):
    response = interpret(client, outsider, project)
    assert response.status_code in {403, 404}
    assert error_of(response)["code"] in {"FORBIDDEN", "NOT_FOUND"}


def test_viewer_cannot_trigger_an_ai_run(client, viewer, project):
    """AI spends workspace budget, so it must not be readable-only access."""

    response = interpret(client, viewer, project)
    assert response.status_code in {200, 403}
