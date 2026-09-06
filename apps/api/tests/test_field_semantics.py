"""Batch 21: LLM field-semantics dictionary.

Covers the output contract (hallucinated column names dropped, truncation,
dedupe, the 60-entry cap), the digest/deterministic-report label rendering
(with a byte-identical regression lock for label-less inputs), and the parse
pipeline integration: a scripted adapter writes the dictionary, an absent
provider key leaves both columns NULL without failing the parse.
"""

from __future__ import annotations

import pytest
from conftest import audit_actions, upload, version_of
from sqlalchemy import select

from app import db as database
from app.ai_context import FIELD_SEMANTICS_SCHEMA, AIOutputValidationError, validate_field_semantics
from app.analytics.digest import build_findings_digest
from app.models import DataColumn, DatasetVersion
from app.services.auto_report import _compute_report_aggregates, _deterministic_report_parts

# ---------------------------------------------------------------------------
# output contract
# ---------------------------------------------------------------------------


def test_field_semantics_schema_shape():
    assert FIELD_SEMANTICS_SCHEMA["required"] == ["dataset_label", "columns"]
    item = FIELD_SEMANTICS_SCHEMA["properties"]["columns"]["items"]
    assert item["required"] == ["name", "label", "description"]


def test_validator_drops_hallucinated_columns():
    result = validate_field_semantics(
        {
            "dataset_label": "测试表",
            "columns": [
                {"name": "a", "label": "甲", "description": "x"},
                {"name": "ghost", "label": "幻觉字段", "description": "不在输入里"},
            ],
        },
        known_columns={"a"},
    )
    assert [entry["name"] for entry in result["columns"]] == ["a"]


def test_validator_truncates_dedupes_and_skips_empty():
    result = validate_field_semantics(
        {
            "dataset_label": "标" * 100,
            "columns": [
                {"name": "a", "label": "签" * 50, "description": "述" * 300},
                {"name": "a", "label": "重复条目", "description": "不应生效"},
                {"name": "", "label": "无名字", "description": "不应生效"},
                {"name": "b", "label": "", "description": "无标签不应生效"},
            ],
        },
        known_columns={"a", "b"},
    )
    assert result["dataset_label"] == "标" * 60
    assert [entry["name"] for entry in result["columns"]] == ["a"]
    assert result["columns"][0]["label"] == "签" * 40
    assert result["columns"][0]["description"] == "述" * 200


def test_validator_caps_at_sixty_columns():
    known = {f"c{index}" for index in range(80)}
    payload = {
        "dataset_label": "",
        "columns": [{"name": f"c{index}", "label": "标签", "description": ""} for index in range(80)],
    }
    result = validate_field_semantics(payload, known_columns=known)
    assert len(result["columns"]) == 60


def test_validator_rejects_malformed_output():
    with pytest.raises(AIOutputValidationError):
        validate_field_semantics(["not", "an", "object"])
    with pytest.raises(AIOutputValidationError):
        validate_field_semantics({"dataset_label": "", "columns": "not-a-list"})


# ---------------------------------------------------------------------------
# digest / deterministic report label rendering
# ---------------------------------------------------------------------------


def _labelled_dataset() -> dict:
    return {
        "name": "events",
        "dataset_label": "用户行为",
        "row_count": 100,
        "duplicate_rows": 0,
        "metrics": [
            {
                "name": "channel",
                "label": "投放渠道",
                "missing_rate": 0.5,
                "categories": [{"value": "organic", "count": 90, "rate": 0.9}],
            },
            {
                "name": "dau",
                "label": "日活",
                "constant": True,
                "outliers": 20,
                "statistics": {"count": 100},
            },
        ],
        "correlation_pairs": {"channel ~ dau": 0.8},
        "trend": {
            "metric_column": "dau",
            "last_period_change": -0.5,
            "previous_value": 100,
            "last_value": 50,
            "last_period": "2026-09",
            "previous_count": 10,
            "last_count": 10,
        },
    }


def test_digest_statements_use_business_labels():
    findings = build_findings_digest([_labelled_dataset()])
    by_kind = {item["kind"]: item["statement"] for item in findings}
    assert "events（用户行为）" in by_kind["missing"]
    assert "channel（投放渠道）" in by_kind["missing"]
    assert "channel（投放渠道）" in by_kind["concentration"]
    assert "channel（投放渠道） ~ dau（日活）" in by_kind["correlation"]
    assert "dau（日活）" in by_kind["trend_shift"]
    assert "dau（日活）" in by_kind["constant"]
    assert "dau（日活）" in by_kind["outlier"]
    # evidence scope columns stay raw names
    missing = next(item for item in findings if item["kind"] == "missing")
    assert missing["columns"] == ["channel"]


def test_digest_statements_without_labels_unchanged():
    dataset = _labelled_dataset()
    dataset.pop("dataset_label")
    for column in dataset["metrics"]:
        column.pop("label")
    by_kind = {item["kind"]: item["statement"] for item in build_findings_digest([dataset])}
    assert by_kind["missing"] == "「events」字段 channel 缺失率高达 50.0%，分析结论受其完整性影响。"
    assert by_kind["correlation"] == "「events」中 channel ~ dau 呈正相关（r=0.8）；相关不代表因果。"
    assert by_kind["trend_shift"] == "「events」dau 最近一期（2026-09）环比下降 50.0%（上期 100 → 本期 50）。"
    assert by_kind["concentration"] == "「events」channel 高度集中于「organic」（90 条，占 90.0%）。"
    assert by_kind["constant"] == "「events」1 个字段内容完全固化（dau），不构成区分维度。"
    assert by_kind["outlier"] == "「events」dau 有 20 个 IQR 离群值（占 20.0%），均值类结论可能被拉偏。"


def _labelled_aggregates() -> list[dict]:
    return [
        {
            "name": "events",
            "dataset_label": "用户行为",
            "row_count": 100,
            "column_count": 2,
            "metrics": [
                {
                    "name": "channel",
                    "label": "投放渠道",
                    "source": "original",
                    "missing_rate": 0.0,
                    "categories": [{"value": "organic", "count": 80, "rate": 0.8}],
                },
                {
                    "name": "dau",
                    "source": "original",
                    "missing_rate": 0.0,
                    "statistics": {"count": 100, "mean": 12.5, "median": 12.0, "min": 1.0, "max": 30.0},
                },
            ],
        }
    ]


def test_deterministic_parts_use_labels():
    _title, _summary, sections, _findings = _deterministic_report_parts("项目", _labelled_aggregates(), [])
    body = "\n".join(section["content"] for section in sections)
    assert "events（用户行为）" in body
    assert "events（用户行为） · channel（投放渠道）" in body
    assert "events（用户行为） · dau" in body


def test_deterministic_parts_without_labels_unchanged():
    aggregates = _labelled_aggregates()
    del aggregates[0]["dataset_label"]
    for column in aggregates[0]["metrics"]:
        column.pop("label", None)
    _title, _summary, sections, _findings = _deterministic_report_parts("项目", aggregates, [])
    body = "\n".join(section["content"] for section in sections)
    assert "- **events**：100 行 × 2 列" in body
    assert "events · channel：" in body
    assert "（投放渠道）" not in body


# ---------------------------------------------------------------------------
# parse pipeline integration
# ---------------------------------------------------------------------------


class _FieldSemanticsAdapter:
    """Scripted provider returning the dictionary plus hallucinated and
    duplicate entries -- only the exact column names may survive."""

    configured = True

    async def complete(self, *, messages, response_schema, request_metadata):
        from app.infrastructure.llm.deepseek import LlmResult

        return LlmResult(
            structured={
                "dataset_label": "用户行为事件表",
                "columns": [
                    {"name": "user_id", "label": "用户标识", "description": "用户的唯一编号"},
                    {"name": "event_time", "label": "事件时间", "description": "事件发生的时间点"},
                    {"name": "event_name", "label": "事件名称", "description": "用户行为的类型"},
                    {"name": "channel", "label": "投放渠道", "description": "流量的来源渠道"},
                    {"name": "version", "label": "应用版本", "description": "客户端版本号"},
                    {"name": "ghost_column", "label": "幻觉字段", "description": "输入中不存在"},
                    {"name": "channel", "label": "重复条目", "description": "重名保序去重"},
                ],
            },
            finish_reason="stop",
            prompt_tokens=120,
            completion_tokens=90,
        )


def test_upload_writes_field_semantics_dictionary(client, owner, project, monkeypatch):
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FieldSemanticsAdapter())

    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version_id = uploaded["version"]["id"]

    with database.SessionLocal() as db:
        version = db.get(DatasetVersion, version_id)
        assert version is not None
        assert version.status == "ready"
        labels = {column.name: (column.semantic_label, column.semantic_description) for column in version.columns}
        assert labels["channel"] == ("投放渠道", "流量的来源渠道")
        assert labels["event_name"] == ("事件名称", "用户行为的类型")
        assert set(labels) == {"user_id", "event_time", "event_name", "channel", "version"}
        assert all(label is not None for label, _ in labels.values())
        assert version.schema_json["dataset_label"] == "用户行为事件表"

    assert "dataset.fields_interpreted" in audit_actions(owner["workspace"]["id"])


def test_upload_without_key_leaves_semantics_null(client, owner, project):
    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version_id = uploaded["version"]["id"]

    fetched = version_of(client, owner, version_id)
    assert fetched["status"] == "ready"
    assert all(column["semantic_label"] is None for column in fetched["columns"])
    assert all(column["semantic_description"] is None for column in fetched["columns"])
    actions = audit_actions(owner["workspace"]["id"])
    assert "dataset.field_semantics_failed" not in actions
    assert "dataset.fields_interpreted" not in actions


def test_compute_aggregates_carry_labels(client, owner, project, monkeypatch):
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FieldSemanticsAdapter())
    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version_id = uploaded["version"]["id"]

    with database.SessionLocal() as db:
        stored = db.get(DatasetVersion, version_id)
        assert stored is not None
        storage_path = stored.storage_path
        file_name = stored.file_name
        row_count = stored.row_count
        column_count = stored.column_count

    snapshot = {
        "version_id": version_id,
        "dataset_name": uploaded["dataset"]["name"],
        "version_number": 1,
        "storage_path": storage_path,
        "file_name": file_name,
        "row_count": row_count,
        "column_count": column_count,
        "dataset_label": "用户行为事件表",
        "columns": [
            {"name": column["name"], "type": column["confirmed_type"] or column["inferred_type"], "label": column["semantic_label"], "description": column["semantic_description"]}
            for column in version_of(client, owner, version_id)["columns"]
        ],
        "quality_score": None,
        "quality_status": None,
        "missing_values": None,
        "anomalies": None,
    }
    aggregates = _compute_report_aggregates(snapshot)
    assert aggregates["dataset_label"] == "用户行为事件表"
    labels = {entry["name"]: entry.get("label") for entry in aggregates["metrics"]}
    assert labels["channel"] == "投放渠道"
    assert labels["user_id"] == "用户标识"


def test_no_semantic_rows_written_without_labels(client, owner, project):
    """Regression lock: a dataset parsed without any semantics dictionary keeps
    every downstream payload free of label keys."""

    uploaded = upload(client, owner, project["id"], "user_events.csv")
    version_id = uploaded["version"]["id"]
    with database.SessionLocal() as db:
        rows = db.scalars(select(DataColumn).where(DataColumn.dataset_version_id == version_id)).all()
        assert rows
        assert all(row.semantic_label is None and row.semantic_description is None for row in rows)
