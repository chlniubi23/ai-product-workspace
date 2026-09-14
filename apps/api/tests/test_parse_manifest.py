"""批 32：文件名保真 + parse_manifest 完整性凭证与对账。

Covers the parse-integrity hardening batch: client file names survive into
``Dataset.name`` / ``DatasetVersion.file_name`` (storage keeps the safe name),
every parse records a machine-verifiable manifest in ``schema_json``, refused
materialisations leave bounded evidence, a mismatched second read fails the
job instead of passing a sick version, and the report overview opens with the
dataset-coverage statement.
"""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import auth, data_of, upload, version_of

from app.analytics.text_metrics import UNPARSED_SAMPLE_LIMIT, materialize_string_columns


def _upload_bytes(client, user, project_id: str, name: str, content: bytes) -> dict:
    """Upload raw bytes under an arbitrary (possibly Chinese) file name."""

    return data_of(
        client.post(
            "/api/v1/datasets/upload",
            headers=auth(user),
            data={"project_id": project_id},
            files={"file": (name, content, "text/csv")},
        )
    )


# ---------------------------------------------------------------------------
# 1. 文件名保真
# ---------------------------------------------------------------------------


def test_chinese_file_name_is_preserved(client, owner, project):
    content = "日期,数量\n2026-01-01,10\n2026-01-02,12\n".encode()
    payload = _upload_bytes(client, owner, project["id"], "03_功能模块使用周报.csv", content)

    assert payload["dataset"]["name"] == "03_功能模块使用周报"
    version = version_of(client, owner, payload["version"]["id"])
    assert version["file_name"] == "03_功能模块使用周报.csv"
    assert version["status"] == "ready"

    # 报告聚合沿用的数据集名 == 原名（纯确定性计算路径，不涉及 LLM）。
    computed = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )
    datasets = computed["report"]["deterministic_json"]["datasets"]
    assert datasets and datasets[0]["name"] == "03_功能模块使用周报"


# ---------------------------------------------------------------------------
# 2. parse_manifest
# ---------------------------------------------------------------------------


def test_parse_manifest_records_encoding_and_columns(client, owner, project):
    content = "\ufeff日期,数量\n2026-01-01,10\n2026-01-02,12\n".encode()
    payload = _upload_bytes(client, owner, project["id"], "weekly.csv", content)
    version = version_of(client, owner, payload["version"]["id"])
    manifest = version["schema_json"]["parse_manifest"]

    assert manifest["encoding"] == "utf-8-sig"
    assert manifest["bom"] is True
    assert manifest["rows"] == 2
    assert manifest["cols"] == 2
    assert manifest["file_name"] == "weekly.csv"
    assert [item["name"] for item in manifest["columns"]] == ["日期", "数量"]
    for item in manifest["columns"]:
        assert {"name", "semantic_type", "parse_rate", "missing", "constant", "identifier"} <= set(item)
    assert manifest["columns"][0]["semantic_type"] == "datetime"
    assert manifest["columns"][0]["parse_rate"] == pytest.approx(1.0)
    assert manifest["columns"][1]["semantic_type"] == "numeric"


# ---------------------------------------------------------------------------
# 3. 未解析样本
# ---------------------------------------------------------------------------


def test_materialize_string_columns_reports_refusals():
    frame = pd.DataFrame({"金额": ["¥100", "¥200", "¥300", "¥400", "非数字"]})
    working, samples = materialize_string_columns(frame)

    assert working["金额"].tolist()[:4] == [100.0, 200.0, 300.0, 400.0]
    assert samples == [{"column": "金额", "row": 4, "value": "非数字"}]


def test_materialize_samples_are_bounded():
    values = [str(value) for value in range(30)] + ["坏"] * 6
    frame = pd.DataFrame({"金额": values})
    working, samples = materialize_string_columns(frame)

    assert len(samples) == UNPARSED_SAMPLE_LIMIT
    assert all(item["column"] == "金额" for item in samples)
    assert working["金额"].iloc[0] == 0.0  # 物化本身不受影响


def test_unparsed_sample_surfaces_in_the_manifest(client, owner, project):
    rows = "\n".join(f"¥{value}" for value in range(100, 109))  # 9 个可解析
    content = f"备注\n{rows}\n非数字\n".encode()  # + 1 个脏值 = parse_rate 0.9
    payload = _upload_bytes(client, owner, project["id"], "mixed.csv", content)
    version = version_of(client, owner, payload["version"]["id"])
    assert version["status"] == "ready"  # 脏值不致命

    manifest = version["schema_json"]["parse_manifest"]
    assert manifest["columns"][0]["semantic_type"] == "numeric"
    assert manifest["columns"][0]["parse_rate"] == pytest.approx(0.9)
    assert manifest["unparsed_samples"] == [{"column": "备注", "row": 9, "value": "非数字"}]


# ---------------------------------------------------------------------------
# 4. 对账失败路径
# ---------------------------------------------------------------------------


def test_parse_integrity_reconciliation_fails_the_job(client, owner, project, monkeypatch):
    from app.services import job_handlers as job_handlers_module

    real_read = job_handlers_module._read_dataframe_with_meta
    calls = {"count": 0}

    def fake_read(path, file_name, worksheet_name=None):
        calls["count"] += 1
        frame, meta = real_read(path, file_name, worksheet_name)
        if calls["count"] == 2:
            return frame.iloc[1:], meta  # 二次读取行数不一致
        return frame, meta

    monkeypatch.setattr(job_handlers_module, "_read_dataframe_with_meta", fake_read)

    content = "日期,数量\n2026-01-01,10\n2026-01-02,12\n".encode()
    payload = _upload_bytes(client, owner, project["id"], "reconcile.csv", content)

    job = data_of(client.get(f"/api/v1/jobs/{payload['job']['id']}", headers=auth(owner)))
    assert job["status"] == "failed"
    assert job["error_code"] == "PARSE_INTEGRITY_FAILED"
    version = version_of(client, owner, payload["version"]["id"])
    assert version["status"] == "failed"


# ---------------------------------------------------------------------------
# 5. 报告覆盖说明
# ---------------------------------------------------------------------------


def test_report_coverage_with_two_datasets(client, owner, project):
    upload(client, owner, project["id"], "metrics.csv")
    upload(client, owner, project["id"], "user_events.csv")

    computed = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )
    report = computed["report"]
    assert report["deterministic_json"]["coverage"] == {"included": 2, "total": 2, "omitted": []}
    overview = next(
        section for section in report["sections_json"] if section["heading"] == "一、数据概况"
    )
    assert overview["content"].startswith("本报告包含全部 2 个数据集。")
