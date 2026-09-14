"""Dual-dimension quality, auditable pseudo-correlation exclusion and derived-column safety.

Covers the second analytics-correctness batch:

* the "analysis quality" dimension is computed from the *real* auto-analysis
  artifacts instead of being a constant 0, while ``overall_score`` / ``status``
  (the only gate inputs) stay exactly as the parse-time assessment produced them;
* excluded pseudo-correlations expose a human-auditable detail list that the AI
  firewall intentionally drops (the key is not allowlisted);
* extracted ``{source}__{metric}`` columns never overwrite an existing column;
* ordinal statistics keep legitimate zero values instead of turning them into
  ``None``.
"""

from __future__ import annotations

import json

import pandas as pd
from conftest import upload, version_of

from app.ai_context import assert_safe_ai_context, build_ai_context
from app.analytics.dag import build_lineage_map
from app.analytics.engine import AnalysisEngine
from app.analytics.quality import assess_quality
from app.analytics.text_metrics import extract_text_metrics
from app.analytics.types import compute_ordinal_statistics
from app.services.ai_stages import _reduce_artifact_payload_for_ai
from app.services.analysis_pipeline import _analysis_artifacts

FIXTURE = "tests/fixtures/samples/weekly_report.csv"
METRICS = "tests/fixtures/metrics.csv"


# ---------------------------------------------------------------------------
# 5. analysis_quality 落地为真实值
# ---------------------------------------------------------------------------


def test_parse_writes_real_analysis_quality_and_keeps_the_gate_untouched(client, owner, project):
    uploaded = upload(client, owner, project["id"], "metrics.csv")
    version = version_of(client, owner, uploaded["version"]["id"])

    quality = version["quality_report"]
    dual = quality["summary_json"]["dual_quality"]

    # 自动分析确实产出了相关性/离群结果，因此第二个维度不再是常量 0。
    assert dual["analysis_quality"]["score"] > 0
    assert dual["analysis_quality"]["correlation_tests"] > 0
    assert isinstance(dual["analysis_quality"]["outlier_detection"], bool)
    assert dual["data_quality"]["score"] >= 0
    assert {"summary", "recommendation", "data_quality", "analysis_quality"} == set(dual)

    # 门控无漂移：overall_score / status 必须仍是解析时点 assess_quality 的值。
    expected = assess_quality(pd.read_csv(METRICS))
    assert quality["overall_score"] == expected.overall_score
    assert quality["status"] == expected.status


# ---------------------------------------------------------------------------
# 6. 被排除相关性：可人工审计，但不出站
# ---------------------------------------------------------------------------


def _eda_payload():
    frame = pd.read_csv(FIXTURE)
    lineage = build_lineage_map(list(frame.columns), {"渗透率(% 占周活)"})
    return AnalysisEngine().run_eda(frame, lineage_map=lineage).to_dict()["payload_json"]


def test_excluded_correlation_detail_is_auditable_but_never_shipped():
    payload = _eda_payload()
    detail = payload["excluded_correlation_pairs_detail"]

    assert isinstance(detail, list) and detail
    assert len(detail) <= 20
    assert payload["excluded_correlation_pairs"] >= len(detail)
    assert all({"var1", "var2", "reason"} <= set(item) for item in detail)
    assert any("派生" in item["reason"] or "机械" in item["reason"] for item in detail)

    # 明细是给人工看的：键不在 _AGGREGATE_LIST_KEYS 里，出站必须被丢弃。
    reduced = _reduce_artifact_payload_for_ai(payload)
    safe = assert_safe_ai_context(build_ai_context(artifacts=[{"id": "eda", "title": "EDA", "payload": reduced}]))
    outbound = safe["artifacts"][0]["payload"]
    assert "excluded_correlation_pairs_detail" not in outbound
    assert "excluded_correlation_pairs_detail" not in json.dumps(safe, ensure_ascii=False)
    # 标量计数可以出站（它不是列表，防火墙不拦）。
    assert outbound["excluded_correlation_pairs"] == payload["excluded_correlation_pairs"]


def test_manual_correlation_artifact_carries_the_same_detail():
    frame = pd.read_csv(FIXTURE)
    version = type("Version", (), {"id": "v-detail", "schema_json": {"columns": []}, "columns": []})()
    payload = _analysis_artifacts(frame, version, "correlation_analysis", {})[0]["payload_json"]

    assert payload["excluded_correlation_pairs"] >= 1
    assert 0 < len(payload["excluded_correlation_pairs_detail"]) <= 20
    assert all({"var1", "var2", "reason"} <= set(item) for item in payload["excluded_correlation_pairs_detail"])


# ---------------------------------------------------------------------------
# 7. 抽取列不得覆盖既有列
# ---------------------------------------------------------------------------


def test_extracted_column_never_overwrites_an_existing_column():
    # 每行尾字符不同 -> 列不是常量列，才会被文本指标抽取扫描。
    prose = "本周DAU110k留存率45%需要继续观察后续走势避免结论被单周波动带偏并且核对周维度口径"
    rows = [f"{prose}{suffix}" for suffix in "甲乙丙丁戊"]

    # 先跑一遍拿到确定性的派生列名，再构造一个**同名**的既有列。
    _clean, clean_report = extract_text_metrics(pd.DataFrame({"备注": rows}))
    assert clean_report
    derived_name = clean_report[0]["derived_column"]

    frame = pd.DataFrame({"备注": rows, derived_name: [1.0, 2.0, 3.0, 4.0, 5.0]})
    working, report = extract_text_metrics(frame)

    # 既有列一字未动。
    assert working[derived_name].tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]
    # 新列拿到确定性的后缀名，并写进 report 供人工发现。
    renamed = [item for item in report if item.get("renamed_from")]
    assert renamed, report
    assert renamed[0]["renamed_from"] == derived_name
    assert renamed[0]["derived_column"] == f"{derived_name}__dup1"
    # 该列的指标是留存率（45%）——数值来自 "45%" 这一段。
    assert working[f"{derived_name}__dup1"].tolist() == [45.0] * 5


def test_extraction_report_records_display_variants():
    # 两个源列写的是同一个指标，只有大小写不同（DAU / Dau）——normalized 相同、
    # display 不同，于是派生出两个"其实是同一个指标"的列。
    prose = "本周DAU110k留存率45%需要继续观察后续走势避免结论被单周波动带偏并且核对周维度口径"
    frame = pd.DataFrame(
        {
            "备注": [f"本周DAU110k留存率45%{prose}{suffix}" for suffix in "甲乙丙丁戊"],
            "周报": [f"本周dau110k留存率45%{prose}{suffix}" for suffix in "甲乙丙丁戊"],
        }
    )
    _working, report = extract_text_metrics(frame)

    variants = {item["normalized"]: item.get("display_variants") for item in report if item.get("display_variants")}
    assert variants, "同一指标的不同显示写法应被记录（指标碎片化可见）"
    for _normalized, names in variants.items():
        assert len(names) > 1
        assert {name.lower() for name in names} == {next(iter(names)).lower()}


# ---------------------------------------------------------------------------
# 8. 序数统计不再把 0 当成缺失
# ---------------------------------------------------------------------------


def test_ordinal_statistics_keep_legitimate_zero_values():
    frame = pd.DataFrame({"评分": [0.0, 0.0, 0.0]})
    stats = compute_ordinal_statistics(frame, frame["评分"]).to_dict()

    assert stats["mean"] == 0.0
    assert stats["median"] == 0.0
    assert stats["mode"] == "0.0"
    assert stats["mode_percentage"] == 100.0

    # 非零场景不受影响（[0,0,1,2] 的均值 0.75、中位数 0.5 都必须是数值而非 None）。
    mixed = pd.DataFrame({"评分": [0.0, 0.0, 1.0, 2.0]})
    mixed_stats = compute_ordinal_statistics(mixed, mixed["评分"]).to_dict()
    assert mixed_stats["mean"] == 0.75
    assert mixed_stats["median"] == 0.5
