"""Batch 14 unit tests: robust value parsing and text-metric extraction."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.analytics.parsing import (
    infer_column_type_v2,
    parse_boolean,
    parse_datetime_value,
    parse_numeric,
)
from app.analytics.text_metrics import extract_text_metrics

# --------------------------------------------------------------------------
# parse_numeric
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234", 1234.0),
        ("¥12,000", 12000.0),
        ("￥3,500.5", 3500.5),
        ("$2,000", 2000.0),
        ("45%", 45.0),
        ("110k", 110000.0),
        ("110K", 110000.0),
        ("1.5M", 1500000.0),
        ("2.3亿", 230000000.0),
        ("1.5万", 15000.0),
        ("-42.5", -42.5),
        ("(1,234)", -1234.0),
        ("（880）", -880.0),
        (" 7 ", 7.0),
        (17, 17.0),
        (2.5, 2.5),
    ],
)
def test_parse_numeric_accepts_real_world_formats(raw, expected):
    assert parse_numeric(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "abc", "12ab", "2026年3月", "是", True, float("nan")],
)
def test_parse_numeric_rejects_non_numbers(raw):
    assert parse_numeric(raw) is None


# --------------------------------------------------------------------------
# parse_datetime_value
# --------------------------------------------------------------------------


def test_parse_datetime_value_handles_iso_slash_and_chinese():
    assert parse_datetime_value("2026-03-30") == datetime(2026, 3, 30)
    assert parse_datetime_value("2026/3/5") == datetime(2026, 3, 5)
    assert parse_datetime_value("2026-03-30 10:00") == datetime(2026, 3, 30, 10, 0)
    assert parse_datetime_value("2026-03-30T10:00:30") == datetime(2026, 3, 30, 10, 0, 30)
    assert parse_datetime_value("2026年3月30日") == datetime(2026, 3, 30)
    assert parse_datetime_value("2026年3月") == datetime(2026, 3, 1)
    assert parse_datetime_value("2026年3月30日 08:05") == datetime(2026, 3, 30, 8, 5)
    assert parse_datetime_value(datetime(2026, 1, 1)) == datetime(2026, 1, 1)
    assert parse_datetime_value("not a date") is None
    assert parse_datetime_value("") is None
    assert parse_datetime_value(None) is None


# --------------------------------------------------------------------------
# parse_boolean
# --------------------------------------------------------------------------


def test_parse_boolean_common_spellings():
    for raw in ("是", "true", "TRUE", "Y", "yes", 1, "1"):
        assert parse_boolean(raw) is True
    for raw in ("否", "false", "False", "N", "no", 0, "0"):
        assert parse_boolean(raw) is False
    assert parse_boolean("maybe") is None
    assert parse_boolean("") is None
    assert parse_boolean(None) is None
    assert parse_boolean(2) is None


# --------------------------------------------------------------------------
# infer_column_type_v2
# --------------------------------------------------------------------------


def test_infer_v2_numeric_percent_and_magnitude_columns():
    series = pd.Series(["1,234", "¥12,000", "45%", "110k", "2.3亿", "-8"])
    result = infer_column_type_v2(series)
    assert result["semantic_type"] == "numeric"
    assert result["parse_rate"] == pytest.approx(1.0)


def test_infer_v2_datetime_chinese_and_iso_mixed():
    series = pd.Series(["2026-03-30", "2026/3/5", "2026年3月30日", "2026年4月1日"])
    result = infer_column_type_v2(series)
    assert result["semantic_type"] == "datetime"
    assert result["parse_rate"] == pytest.approx(1.0)


def test_infer_v2_boolean_and_identifier_and_constant():
    assert infer_column_type_v2(pd.Series(["是", "否", "是", "true"]))["semantic_type"] == "boolean"
    identifier = infer_column_type_v2(pd.Series([f"id-{i}" for i in range(10)]))
    assert identifier["semantic_type"] == "identifier"
    assert identifier["unique_ratio"] == pytest.approx(1.0)
    constant = infer_column_type_v2(pd.Series(["CC-01"] * 5))
    assert constant["constant"] is True
    assert constant["semantic_type"] == "category"  # single value: constant is an orthogonal flag
    assert infer_column_type_v2(pd.Series([1.0, 2.0, 3.0]))["semantic_type"] == "numeric"


def test_infer_v2_low_parse_rate_falls_back_to_text():
    series = pd.Series(["-week 1-", "weekly review", "month note", "quarter", "annual plan"])
    result = infer_column_type_v2(series)
    # every row differs AND the cells carry spaces -> prose, not an identifier
    assert result["semantic_type"] == "text"


# --------------------------------------------------------------------------
# extract_text_metrics
# --------------------------------------------------------------------------


def _metrics_frame():
    return pd.DataFrame(
        {
            "week_start": [f"2026-01-{day:02d}" for day in range(1, 11)],
            "summary": [
                "周 DAU 100k，7日留存 40%",
                "周 DAU 103k，7日留存 42%",
                "周新增注册 3,200",
                "周 DAU 109k，7日留存 41%",
                "周 DAU 112k，7日留存 44%",
                "",
                "周 DAU 118k，7日留存 46%",
                "周 DAU 121k，7日留存 47%",
                "周 DAU 124k，7日留存 43%",
                "周 DAU 127k，7日留存 45%",
            ],
        }
    )


def test_extract_promotes_frequent_labels_and_reports_coverage():
    frame, report = extract_text_metrics(_metrics_frame())
    by_column = {row["derived_column"]: row for row in report}
    assert "summary__DAU" in by_column
    assert "summary__7日留存" in by_column
    dau = by_column["summary__DAU"]
    assert dau["source_column"] == "summary"
    assert dau["parsed_rows"] == 8  # 10 rows, minus the prose row and the empty one
    assert dau["coverage"] == pytest.approx(0.8)
    assert dau["unit_note"] == "k"
    # row-aligned: rows without DAU stay NaN, values are magnitudes
    assert pd.isna(frame.loc[2, "summary__DAU"])
    assert pd.isna(frame.loc[5, "summary__DAU"])
    assert frame.loc[0, "summary__DAU"] == pytest.approx(100000.0)
    # 新增注册 covers 1/10 rows -- below the 0.3 threshold, never a column
    assert not any("新增注册" in row["metric"] for row in report)
    # the original column is untouched
    assert frame["summary"].iloc[0] == "周 DAU 100k，7日留存 40%"


def test_extract_is_deterministic_and_never_mutates_input():
    frame = _metrics_frame()
    snapshot = frame.copy()
    first_frame, first_report = extract_text_metrics(frame)
    second_frame, second_report = extract_text_metrics(frame)
    pd.testing.assert_frame_equal(first_frame, second_frame)
    assert first_report == second_report
    pd.testing.assert_frame_equal(frame, snapshot)


def test_extracted_derived_column_feeds_the_trend_engine():
    from app.analytics.engine import AnalysisEngine

    frame, _report = extract_text_metrics(_metrics_frame())
    artifact = AnalysisEngine("v-1").run_trend_analysis(
        frame, time_column="week_start", metric_column="summary__DAU", frequency="D"
    )
    rows = artifact.payload["rows"]
    assert len(rows) == 10
    assert rows[-1]["value"] == pytest.approx(127000.0)
    assert rows[-1]["period_over_period"] is not None


def test_extract_reads_numbers_glued_to_the_label():
    # 无分隔符写法：数字紧跟标签（旧实现会把 "DAU110k" 拆成 标签"DAU11"+数字"0k"）。
    rows = [f"本周DAU{n}k，7日留存{m}%" for n, m in ((110, 45), (120, 46), (130, 47), (140, 48), (150, 49))]
    working, report = extract_text_metrics(pd.DataFrame({"周报": rows}))

    by_column = {row["derived_column"]: row for row in report}
    assert set(by_column) == {"周报__本周DAU", "周报__7日留存"}
    assert by_column["周报__本周DAU"]["coverage"] == pytest.approx(1.0)
    assert by_column["周报__7日留存"]["coverage"] == pytest.approx(1.0)
    assert working["周报__本周DAU"].tolist() == [110000.0, 120000.0, 130000.0, 140000.0, 150000.0]
    assert working["周报__7日留存"].tolist() == [45.0, 46.0, 47.0, 48.0, 49.0]


def test_extract_reads_a_unitless_number_glued_to_the_label():
    working, report = extract_text_metrics(pd.DataFrame({"周报": [f"本周DAU110，{s}" for s in "甲乙丙丁戊"]}))
    assert [item["derived_column"] for item in report] == ["周报__本周DAU"]
    assert working["周报__本周DAU"].tolist() == [110.0] * 5


def test_extract_keeps_a_label_with_an_inner_digit():
    # "Top3销量" 以汉字结尾、内含数字：标签不得在数字处截断（旧实现会回退成 "Top"+3）。
    working, report = extract_text_metrics(pd.DataFrame({"周报": [f"Top3销量 800，{s}" for s in "甲乙丙丁戊"]}))
    assert [item["derived_column"] for item in report] == ["周报__Top3销量"]
    assert working["周报__Top3销量"].tolist() == [800.0] * 5


def test_extract_yields_two_correct_metrics_for_a_glued_prose_pair():
    # 长句里 "110k" 与 "45%" 都紧贴文字：应拆成 本周DAU=110000 与 留存率=45 两个指标，
    # 而不是把 110k 吞进指标名。
    prose = "本周DAU110k留存率45%需要继续观察后续走势避免结论被单周波动带偏并且核对周维度口径"
    working, report = extract_text_metrics(pd.DataFrame({"备注": [f"{prose}{s}" for s in "甲乙丙丁戊"]}))

    by_column = {row["derived_column"]: row for row in report}
    assert set(by_column) == {"备注__本周DAU", "备注__留存率"}
    assert working["备注__本周DAU"].tolist() == [110000.0] * 5
    assert working["备注__留存率"].tolist() == [45.0] * 5
