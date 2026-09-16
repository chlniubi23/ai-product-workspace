"""Auto report pipeline: batch upload -> deterministic aggregates -> AI report.

``DEEPSEEK_API_KEY`` is empty in the test environment, so ``POST .../auto-report``
must degrade to the deterministic fallback (status ``not_configured``) while
still persisting a readable, number-grounded report.  The AI-boundary assertion
(checking the stored provider context carries no raw rows) runs against the
persisted ``AIRun.input_summary_json``, exactly what a live provider would see.
"""

from __future__ import annotations

from io import BytesIO

from conftest import audit_actions, auth, data_of, error_of, version_of
from sqlalchemy import select

from app import db as database
from app.ai_context import assert_safe_ai_context
from app.models import AIRun

CSV_EVENTS = (
    "user_id,event_time,event_name,channel\n"
    + "".join(
        f"u_{i % 20:03d},2026-08-{(i % 28) + 1:02d}T10:00:00Z,page_view,{'organic' if i % 2 else 'paid'}\n"
        for i in range(40)
    )
)
CSV_METRICS = (
    "date,dau,new_users\n"
    + "".join(f"2026-08-{(i % 28) + 1:02d},{1000 + i},{50 + i}\n" for i in range(28))
)


def _batch(client, user, project_id: str, files: list[tuple[str, bytes]]):
    payload = {"project_id": project_id}
    return client.post(
        "/api/v1/datasets/upload-batch",
        headers=auth(user),
        data=payload,
        files=[("files", (name, BytesIO(content), "text/csv")) for name, content in files],
    )


def _generate_report(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/auto-report", headers=auth(user))


def test_batch_upload_creates_versions_and_audits(client, owner, project):
    response = _batch(
        client,
        owner,
        project["id"],
        [("events.csv", CSV_EVENTS.encode()), ("metrics.csv", CSV_METRICS.encode()), ("virus.exe", b"MZ...")],
    )
    payload = data_of(response)
    assert len(payload["uploads"]) == 2
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["file_name"] == "virus.exe"
    assert payload["failures"][0]["code"] == "VALIDATION_ERROR"

    names = sorted(row["dataset"]["name"] for row in payload["uploads"])
    assert names == ["events", "metrics"]
    # The inline version payload is serialized before the parse job drains, so
    # re-fetch each version the way the web client does.
    for row in payload["uploads"]:
        version = version_of(client, owner, row["version"]["id"])
        assert version["status"] == "ready", version["status"]
        assert version["row_count"] > 0
    assert "dataset.batch_upload_queued" in audit_actions(owner["workspace"]["id"])


def test_batch_upload_rejects_more_than_ten_files(client, owner, project):
    files = [(f"f{index}.csv", CSV_METRICS.encode()) for index in range(11)]
    payload = error_of(_batch(client, owner, project["id"], files))
    assert payload["code"] == "VALIDATION_ERROR"
    assert "10" in payload["message"]


def test_auto_report_without_provider_is_deterministic(client, owner, project):
    _batch(client, owner, project["id"], [("events.csv", CSV_EVENTS.encode()), ("metrics.csv", CSV_METRICS.encode())])
    payload = data_of(_generate_report(client, owner, project["id"]))

    report = payload["report"]
    assert payload["status"] == "not_configured"
    assert report["status"] == "not_configured"
    assert report["error_code"] == "LLM_NOT_CONFIGURED"
    assert len(report["dataset_version_ids"]) == 2
    headings = [section["heading"] for section in report["sections_json"]]
    assert any("数据概况" in heading for heading in headings)
    assert report["key_findings"], "deterministic findings must not be empty"
    assert "40" in report["content_markdown"]  # events.csv row count appears in the overview
    assert report["deterministic_json"]["datasets"]
    assert not report["deterministic_json"]["read_failures"]


def test_auto_report_persists_and_lists(client, owner, project):
    _batch(client, owner, project["id"], [("metrics.csv", CSV_METRICS.encode())])
    created = data_of(_generate_report(client, owner, project["id"]))["report"]

    listed = data_of(client.get(f"/api/v1/projects/{project['id']}/auto-reports", headers=auth(owner)))
    assert [row["id"] for row in listed] == [created["id"]]

    fetched = data_of(client.get(f"/api/v1/auto-reports/{created['id']}", headers=auth(owner)))
    assert fetched["id"] == created["id"]
    assert fetched["markdown"] == created["content_markdown"]


def test_auto_report_payload_carries_trust_card(client, owner, project):
    """批 39：可信度评分卡 —— 全部来自既有字段，缺失项为 null，不编造默认值。"""

    _batch(client, owner, project["id"], [("events.csv", CSV_EVENTS.encode()), ("metrics.csv", CSV_METRICS.encode())])
    report = data_of(_generate_report(client, owner, project["id"]))["report"]
    trust = report["trust_card"]
    deterministic = report["deterministic_json"]
    datasets = deterministic["datasets"]

    # 批 32 起新解析的版本都带 manifest 摘要 → 对账通过。
    assert trust["integrity"] == "对账通过"
    # 覆盖与 deterministic_json["coverage"] 一致。
    assert trust["coverage"] == f"{deterministic['coverage']['included']}/{deterministic['coverage']['total']}"
    # 未做 AI 解读 → 数字可验证率为 null（缺失不编造）。
    assert trust["fact_check_rate"] is None
    # 其余维度与聚合一致（镜像聚合逻辑，不预设具体值）。
    warnings = [item["parse_warnings_count"] for item in datasets if isinstance(item.get("parse_warnings_count"), int)]
    assert trust["parse_warnings"] == (sum(warnings) if warnings else None)
    excluded = [item["excluded_correlation_pairs"] for item in datasets if isinstance(item.get("excluded_correlation_pairs"), int)]
    assert trust["excluded_correlations"] == (sum(excluded) if excluded else None)
    small = sum(1 for item in datasets if item.get("small_sample") is True)
    assert trust["small_sample"] == small
    # 两个数据集都远大于小样本阈值 → 0 是真实计数而非默认值。
    assert trust["small_sample"] == 0


def test_auto_report_confirms_idempotently(client, owner, project):
    _batch(client, owner, project["id"], [("metrics.csv", CSV_METRICS.encode())])
    report_id = data_of(_generate_report(client, owner, project["id"]))["report"]["id"]

    confirmed = data_of(client.post(f"/api/v1/auto-reports/{report_id}/confirm", headers=auth(owner)))
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_by"] == owner["user"]["id"]
    assert confirmed["confirmed_at"]

    again = data_of(client.post(f"/api/v1/auto-reports/{report_id}/confirm", headers=auth(owner)))
    assert again["confirmed_at"] == confirmed["confirmed_at"]
    assert "report.confirmed" in audit_actions(owner["workspace"]["id"])


def test_auto_report_enforces_roles_and_membership(client, owner, viewer, outsider, project):
    assert error_of(_generate_report(client, viewer, project["id"]))["code"] == "FORBIDDEN"
    assert error_of(_generate_report(client, outsider, project["id"]))["code"] in {"FORBIDDEN", "NOT_FOUND"}
    assert error_of(_generate_report(client, owner, project["id"]))["code"] == "VALIDATION_ERROR"

    _batch(client, owner, project["id"], [("metrics.csv", CSV_METRICS.encode())])
    report_id = data_of(_generate_report(client, owner, project["id"]))["report"]["id"]
    assert error_of(client.get(f"/api/v1/auto-reports/{report_id}", headers=auth(outsider)))["code"] == "FORBIDDEN"
    assert error_of(client.post(f"/api/v1/auto-reports/{report_id}/confirm", headers=auth(viewer)))["code"] == "FORBIDDEN"


def test_auto_report_provider_context_stays_aggregate_only(client, owner, project):
    _batch(client, owner, project["id"], [("events.csv", CSV_EVENTS.encode())])
    data_of(_generate_report(client, owner, project["id"]))

    with database.SessionLocal() as db:
        # Batch 10 moved the provider call into the auto_report_narration
        # stage; the combined endpoint still produces exactly one such run.
        run = db.scalar(select(AIRun).where(AIRun.feature_name == "auto_report_narration"))
        assert run is not None
        context = run.input_summary_json["context"]
    # The exact payload a live provider would receive must satisfy the
    # allowlist: no raw rows, no storage paths, no feedback content.
    assert_safe_ai_context(context)
    for artifact in context["artifacts"]:
        assert "rows" not in artifact["payload"]
        assert "preview" not in artifact["payload"]
        assert "file_name" not in artifact["payload"]
        assert "storage_path" not in artifact["payload"]


def test_auto_report_skips_versions_that_failed_parse(client, owner, project):
    # broken.xlsx holds garbage, so its parse job fails and the version never
    # becomes ready -- the report must simply cover the healthy dataset.
    _batch(
        client,
        owner,
        project["id"],
        [("metrics.csv", CSV_METRICS.encode()), ("broken.xlsx", b"not a real xlsx")],
    )
    payload = data_of(_generate_report(client, owner, project["id"]))
    report = payload["report"]
    assert len(report["dataset_version_ids"]) == 1
    assert report["deterministic_json"]["datasets"][0]["name"] == "metrics"
    assert report["sections_json"], "healthy dataset still produces sections"
