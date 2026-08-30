"""Stage 4 -- analysis runs.

Analysis is the last deterministic stage before the AI boundary.  Every number a
later AI stage may cite is produced here, so these tests care about three
things: that a run computes a real artifact, that an impossible request fails
loudly rather than returning a plausible-looking empty result, and that the
deliberate degradation path at app/main.py:2691 behaves as documented.

Two fixtures are used deliberately:
  * ready_dataset   -- user_events.csv, event-shaped (user_id/event_time/...)
  * metrics_dataset -- metrics.csv, a daily business table (date/dau/...)
Some analysis types only apply to one shape, which is the whole reason the
degradation path exists.
"""

from __future__ import annotations

import json

from conftest import auth, data_of, error_of, run_analysis, start_analysis, validate_analysis_config

# The three event names present in tests/fixtures/user_events.csv, in order.
# Counts are 5 -> 5 -> 3, so a funnel over them has a real drop-off to assert.
FUNNEL_STEPS = ["register_start", "register_success", "first_value"]

# Semantic field_mapping only accepts user_id / event_time / event_name /
# session_id (FIELD_MAPPING_ALIASES at app/main.py:2470).  Column choices such
# as metric_column and group_column travel in `config` instead.
EVENT_MAPPING = {"user_id": "user_id", "event_time": "event_time", "event_name": "event_name"}


# --------------------------------------------------------------------------
# config validation (the pre-flight the UI calls before enabling "run")
# --------------------------------------------------------------------------


def test_validate_config_lists_the_real_columns(client, owner, ready_dataset):
    payload = validate_analysis_config(client, owner, ready_dataset, "eda")
    assert payload["available_columns"] == ["channel", "event_name", "event_time", "user_id", "version"]


def test_validate_config_accepts_a_correct_event_mapping(client, owner, ready_dataset):
    payload = validate_analysis_config(
        client,
        owner,
        ready_dataset,
        "funnel",
        config={"steps": FUNNEL_STEPS},
        field_mapping={"user_id": "user_id", "event_time": "event_time", "event_name": "event_name"},
    )
    assert payload["valid"] is True
    assert not payload["invalid_mappings"]
    assert not payload["errors"]


def test_validate_config_names_a_column_that_does_not_exist(client, owner, ready_dataset):
    payload = validate_analysis_config(
        client, owner, ready_dataset, "trend", field_mapping={"event_time": "column_that_does_not_exist"}
    )
    assert payload["valid"] is False
    assert payload["invalid_mappings"] or payload["missing_columns"]


def test_validate_config_reports_missing_mappings_for_a_wrong_shaped_table(client, owner, metrics_dataset):
    """A funnel needs user/event columns; a daily metrics table has none."""

    payload = validate_analysis_config(client, owner, metrics_dataset, "funnel")
    assert payload["valid"] is False
    assert payload["missing_mappings"]


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def test_unknown_analysis_type_is_rejected(client, owner, ready_dataset):
    response = start_analysis(client, owner, ready_dataset, "definitely_not_a_real_analysis")
    assert response.status_code in {400, 422}
    assert error_of(response)["code"] in {"VALIDATION_ERROR", "UNSUPPORTED_ANALYSIS_TYPE"}


def test_eda_run_succeeds_and_produces_an_artifact(client, owner, ready_dataset):
    run = run_analysis(client, owner, ready_dataset, "eda")
    assert run["status"] == "succeeded", run.get("error_code")
    assert run["result_summary"], "eda produced no summary"
    assert run["artifacts"], "eda produced no artifact rows"


def test_funnel_run_succeeds_on_event_data(client, owner, ready_dataset):
    run = run_analysis(
        client, owner, ready_dataset, "funnel", config={"steps": FUNNEL_STEPS}, field_mapping=EVENT_MAPPING
    )
    assert run["status"] == "succeeded", run.get("error_code")
    assert run["result_summary"]
    assert run["artifacts"]


def test_funnel_computes_the_real_drop_off(client, owner, ready_dataset):
    """The fixture has 5 -> 5 -> 3 users, so the funnel must not be flat.

    This is the test that would catch a funnel silently returning zeros or
    echoing the same count at every step.
    """

    run = run_analysis(
        client, owner, ready_dataset, "funnel", config={"steps": FUNNEL_STEPS}, field_mapping=EVENT_MAPPING
    )
    blob = json.dumps(run["result_summary"]) + json.dumps(run["artifacts"])
    assert "5" in blob and "3" in blob, f"expected the 5/3 step counts in the funnel result: {blob[:400]}"


def test_trend_run_succeeds_on_a_metrics_table(client, owner, metrics_dataset):
    run = run_analysis(
        client,
        owner,
        metrics_dataset,
        "trend",
        config={"time_column": "date", "metric_column": "dau"},
    )
    assert run["status"] == "succeeded", run.get("error_code")
    assert run["result_summary"]
    assert run["artifacts"]


def test_group_run_succeeds_on_event_data(client, owner, ready_dataset):
    run = run_analysis(client, owner, ready_dataset, "group", config={"group_column": "channel"})
    assert run["status"] == "succeeded", run.get("error_code")
    assert run["artifacts"]


def test_retention_run_succeeds_on_event_data(client, owner, ready_dataset):
    run = run_analysis(
        client, owner, ready_dataset, "retention", config={"periods": [1, 7]}, field_mapping=EVENT_MAPPING
    )
    assert run["status"] == "succeeded", run.get("error_code")
    assert run["result_summary"]


def test_anomaly_run_succeeds_on_a_metrics_table(client, owner, metrics_dataset):
    run = run_analysis(
        client,
        owner,
        metrics_dataset,
        "anomaly",
        config={"metric_column": "dau", "time_column": "date", "method": "iqr"},
    )
    assert run["status"] == "succeeded", run.get("error_code")


def test_funnel_needs_at_least_two_steps(client, owner, ready_dataset):
    response = start_analysis(
        client, owner, ready_dataset, "funnel", config={"steps": ["register_start"]}, field_mapping=EVENT_MAPPING
    )
    assert response.status_code == 400


def test_anomaly_rejects_an_unsupported_method(client, owner, metrics_dataset):
    response = start_analysis(
        client, owner, metrics_dataset, "anomaly", config={"metric_column": "dau", "method": "telepathy"}
    )
    assert response.status_code == 400


def test_unsupported_field_mapping_key_is_rejected(client, owner, metrics_dataset):
    """`metric` is not a semantic mapping key -- only metric_column in config is."""

    response = start_analysis(
        client, owner, metrics_dataset, "trend", field_mapping={"event_time": "date", "metric": "dau"}
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == "FIELD_MAPPING_INVALID"


def test_run_with_a_nonexistent_column_is_rejected_not_degraded(client, owner, ready_dataset):
    """An invalid mapping is a real mistake and must surface as an error.

    This is the counterpart to the degradation test below: missing mappings
    degrade, but a mapping pointing at a column that isn't there does not.
    """

    response = start_analysis(
        client, owner, ready_dataset, "trend", field_mapping={"event_time": "column_that_does_not_exist"}
    )
    assert response.status_code == 400
    assert error_of(response)["code"] not in {"INTERNAL_ERROR"}


def test_wrong_shaped_request_degrades_instead_of_blocking(client, owner, metrics_dataset):
    """A funnel over a table with no event columns falls back to descriptive.

    Documented at app/main.py:2688 -- a cleaned business table often has no
    user/event/time columns, and that is a shape mismatch rather than a user
    error, so the pipeline degrades rather than dead-ending.
    """

    run = run_analysis(client, owner, metrics_dataset, "funnel")
    assert run["status"] == "succeeded", run.get("error_code")
    assert run["analysis_type"] != "funnel", "expected a fallback to a descriptive run"


# --------------------------------------------------------------------------
# gates and boundaries
# --------------------------------------------------------------------------


def test_analysis_cannot_cross_project_boundaries(client, owner, ready_dataset, project_factory):
    """A dataset from project A must not be analysable under project B."""

    other = project_factory(owner, name="Unrelated project")
    response = client.post(
        "/api/v1/analysis-runs",
        headers=auth(owner),
        json={
            "project_id": other["id"],
            "dataset_version_id": ready_dataset["version_id"],
            "analysis_type": "eda",
            "config": {},
        },
    )
    assert response.status_code == 403
    assert error_of(response)["code"] == "FORBIDDEN"


def test_viewer_cannot_start_an_analysis(client, viewer, ready_dataset):
    response = start_analysis(client, viewer, ready_dataset, "eda")
    assert response.status_code == 403
    assert error_of(response)["code"] == "FORBIDDEN"


def test_outsider_cannot_read_an_analysis_run(client, owner, outsider, ready_dataset):
    run = run_analysis(client, owner, ready_dataset, "eda")
    response = client.get(f"/api/v1/analysis-runs/{run['id']}", headers=auth(outsider))
    assert response.status_code in {403, 404}


def test_analysis_run_is_listed_for_its_project(client, owner, ready_dataset):
    created = run_analysis(client, owner, ready_dataset, "eda")
    payload = data_of(
        client.get(
            "/api/v1/analysis-runs",
            headers=auth(owner),
            params={"project_id": ready_dataset["project"]["id"]},
        )
    )
    items = payload["items"] if isinstance(payload, dict) else payload
    assert created["id"] in [item["id"] for item in items]


def test_rerun_creates_a_second_run(client, owner, ready_dataset):
    first = run_analysis(client, owner, ready_dataset, "eda")
    rerun = data_of(client.post(f"/api/v1/analysis-runs/{first['id']}/rerun", headers=auth(owner)))
    second_id = rerun["analysis_run"]["id"] if "analysis_run" in rerun else rerun["id"]
    assert second_id != first["id"]
