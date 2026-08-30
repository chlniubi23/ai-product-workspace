"""Stage 5 AI narration: the draft contract and the AI boundary.

The suite runs with an empty DEEPSEEK_API_KEY, so these exercise the
degraded-but-honest path: a 200 with an empty draft and a stated limitation,
never a 500.  ``_reduce_artifact_payload_for_ai`` is tested directly because a
silent regression there would let the model narrate confident prose from the
schema alone -- the payload would be ``{}`` and nothing would fail loudly.
"""
from __future__ import annotations

import pandas as pd
from conftest import auth, data_of, error_of

from app.ai_context import build_ai_context
from app.main import _analysis_artifacts, _reduce_artifact_payload_for_ai


class _Version:
    id = "v1"
    dataset_id = "d1"
    row_count = 13
    column_count = 5
    schema_json = [{"name": "user_id", "data_type": "string"}, {"name": "event_time", "data_type": "datetime"}]


def test_raw_artifact_payload_is_emptied_by_the_sanitizer():
    """Pins the reason the reducer exists at all."""

    raw = {"columns": [{"name": "price", "mean": 12.5}], "rows": [{"a": 1}]}
    context = build_ai_context(artifacts=[{"id": "a1", "artifact_type": "table", "title": "EDA", "payload": raw}])
    assert context["artifacts"][0]["payload"] == {}, "if this ever passes raw data through, revisit the reducer"


def test_reducer_keeps_real_numbers_for_every_artifact_type():
    frame = pd.read_csv("tests/fixtures/user_events.csv")
    seen = 0
    for kind in ("eda", "retention"):
        for artifact in _analysis_artifacts(frame, _Version(), kind, {}):
            reduced = _reduce_artifact_payload_for_ai(artifact["payload_json"] or {})
            context = build_ai_context(
                artifacts=[{"id": "x", "artifact_type": artifact["artifact_type"], "title": artifact["title"], "payload": reduced}]
            )
            assert context["artifacts"][0]["payload"], f"{kind}/{artifact['artifact_type']} reduced to nothing"
            seen += 1
    assert seen >= 3


def test_reducer_drops_row_level_data():
    reduced = _reduce_artifact_payload_for_ai({"rows": [{"user": "u1"}], "records": [1], "chart": {"x": 1}, "columns": [{"mean": 2}]})
    assert "rows" not in reduced and "records" not in reduced and "chart" not in reduced
    assert reduced["metrics"] == [{"mean": 2}]


def test_narration_returns_a_draft_without_a_provider_key(client, owner, ready_dataset):
    payload = data_of(
        client.post(
            f"/api/v1/dataset-versions/{ready_dataset['version_id']}/report-narration",
            headers=auth(owner),
        )
    )
    assert payload["draft"] is True
    assert payload["requires_human_confirmation"] is True
    assert payload["status"] in {"not_configured", "failed", "succeeded"}
    assert payload["output"]["limitations"], "a draft with no key must state its limitation"
    assert payload["analysis_run_ids"], "narration must cite the runs it read"


def test_narration_requires_completed_analysis(client, owner, project):
    from conftest import upload

    uploaded = upload(client, owner, project["id"], "empty.csv")
    response = client.post(
        f"/api/v1/dataset-versions/{uploaded['version']['id']}/report-narration",
        headers=auth(owner),
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_viewer_cannot_trigger_narration(client, viewer, ready_dataset):
    response = client.post(
        f"/api/v1/dataset-versions/{ready_dataset['version_id']}/report-narration",
        headers=auth(viewer),
    )
    assert response.status_code == 403


def test_outsider_cannot_narrate_foreign_version(client, outsider, ready_dataset):
    response = client.post(
        f"/api/v1/dataset-versions/{ready_dataset['version_id']}/report-narration",
        headers=auth(outsider),
    )
    assert response.status_code in {403, 404}
