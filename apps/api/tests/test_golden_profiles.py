"""批 33：golden 回归 —— 9 份真实 CSV 的计算层锁值。

9 份文件来自用户的真实工作数据（时序日表 / 小样本汇总表 / 事件长表 / 文本明细 /
实验宽表混合，UTF-8 BOM、中文文件名），**原样复制**进 ``fixtures/golden/``。
期望值全部来自实跑探针（``TEMP/golden_probe.json``）并经人工复核后固化：
先跑实现、再锁值 —— 不是先写想象值再凑实现。

锁三层行为：
1. 结构：rows/cols/原始列清单/小样本开关/相关与分组的存在性；
2. 数值：抽验列的均值/中位数 = ``round_stat``（裸 pandas 独立复算，不经过引擎）；
3. 语义：序数列的 scale/分布/众数、role 命中、机械派生排除对。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest
from conftest import FIXTURES

from app.analytics.dag import build_lineage_map
from app.analytics.digest import build_findings_digest
from app.analytics.engine import AnalysisEngine
from app.analytics.rounding import round_stat
from app.analytics.text_metrics import extract_text_metrics
from app.analytics.types import infer_column_type
from app.services.auto_report import _compute_report_aggregates
from app.services.datasets import _column_schema

GOLDEN = FIXTURES / "golden"
FILES = sorted(GOLDEN.glob("*.csv"))


def _read_reference(path) -> pd.DataFrame:
    """独立读取（不复用生产 reader）：BOM 探测 + 同序编码链。"""

    try:
        has_bom = path.open("rb").read(3) == b"\xef\xbb\xbf"
    except OSError:
        has_bom = False
    order = ("utf-8-sig", "utf-8", "gb18030", "gbk") if has_bom else ("utf-8", "utf-8-sig", "gb18030", "gbk")
    last_error: Exception | None = None
    for encoding in order:
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError as exc:  # pragma: no cover - fixtures are clean
            last_error = exc
    raise AssertionError(f"unreadable fixture {path.name}: {last_error}")


def _snapshot(name: str, frame: pd.DataFrame) -> dict:
    columns = []
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            dtype = "datetime"
        elif pd.api.types.is_integer_dtype(series):
            dtype = "integer"
        elif pd.api.types.is_float_dtype(series):
            dtype = "float"
        else:
            dtype = "string"
        columns.append({"name": str(column), "type": dtype})
    return {
        "version_id": f"golden-{name}",
        "dataset_name": name,
        "version_number": 1,
        # pathlib 语义：绝对路径直接胜出，聚合函数因此读到 fixture 原件。
        "storage_path": str(GOLDEN / name),
        "file_name": name,
        "row_count": len(frame),
        "column_count": len(frame.columns),
        "dataset_label": "",
        "columns": columns,
        "quality_score": None,
        "quality_status": None,
        "missing_values": None,
        "anomalies": None,
        "parse_manifest_summary": None,
    }


def _aggregates(name: str, frame: pd.DataFrame) -> dict:
    return _compute_report_aggregates(_snapshot(name, frame))


def _metrics_by_name(aggregates: dict) -> dict[str, dict]:
    return {str(item.get("name")): item for item in aggregates.get("metrics") or []}


def _original_columns(aggregates: dict) -> list[str]:
    return [str(item.get("name")) for item in aggregates.get("metrics") or [] if item.get("source") == "original"]


def _eda_exclusions(name: str, frame: pd.DataFrame) -> list[dict]:
    """独立跑一次 EDA 取排除明细（聚合层只落计数）。"""

    frame_ext, _report = extract_text_metrics(frame)
    extracted = {str(column) for column in frame_ext.columns if str(column) not in {str(c) for c in frame.columns}}
    lineage = build_lineage_map([str(column) for column in frame_ext.columns], extracted)
    payload = AnalysisEngine(f"golden-{name}").run_eda(frame_ext, top_n=5, lineage_map=lineage).to_dict().get("payload_json") or {}
    return [item for item in payload.get("excluded_correlation_pairs_detail") or [] if isinstance(item, dict)]


#: 实跑探针锁定的期望值（人工逐条复核）。
GOLDEN_EXPECTATIONS = {
    "01_核心产品指标日报.csv": {"rows": 30, "cols": 13, "small": False, "excluded": 4},
    "02_用户增长漏斗.csv": {"rows": 6, "cols": 12, "small": True, "excluded": 1},
    "03_功能模块使用周报.csv": {"rows": 10, "cols": 8, "small": False, "excluded": 4},
    "04_用户行为事件明细.csv": {"rows": 30, "cols": 7, "small": False, "excluded": 0},
    "05_用户反馈明细.csv": {"rows": 20, "cols": 6, "small": False, "excluded": 0},
    "06_AI模型效果与成本日报.csv": {"rows": 30, "cols": 12, "small": False, "excluded": 4},
    "07_AB测试结果.csv": {"rows": 8, "cols": 13, "small": True, "excluded": 0},
    "08_商业化日报.csv": {"rows": 30, "cols": 8, "small": False, "excluded": 0},
    "09_竞品对标_公开数据.csv": {"rows": 6, "cols": 5, "small": True, "excluded": 0},
}


def test_golden_fixture_inventory():
    assert len(FILES) == 9
    assert set(GOLDEN_EXPECTATIONS) == {path.name for path in FILES}


# ---------------------------------------------------------------------------
# 1. 结构锁值
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GOLDEN_EXPECTATIONS))
def test_golden_shape_and_small_sample(name):
    expectations = GOLDEN_EXPECTATIONS[name]
    frame = _read_reference(GOLDEN / name)
    aggregates = _aggregates(name, frame)

    assert aggregates["row_count"] == expectations["rows"] == len(frame)
    assert aggregates["column_count"] == expectations["cols"]
    assert _original_columns(aggregates) == [str(column) for column in frame.columns]
    assert aggregates["small_sample"] is expectations["small"]

    if expectations["small"]:
        # 小样本防护：跨行相关与分组比较整层停用。
        assert "correlation_pairs" not in aggregates
        assert "correlation_pairs_detail" not in aggregates
        assert "breakdown" not in aggregates
        assert "样本量仅" in (aggregates.get("dataset_note") or "")


# ---------------------------------------------------------------------------
# 2. 数值独立复算（裸 pandas + round_stat，不经过引擎）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GOLDEN_EXPECTATIONS))
def test_golden_stats_match_bare_pandas(name):
    frame = _read_reference(GOLDEN / name)
    aggregates = _aggregates(name, frame)
    metrics = _metrics_by_name(aggregates)

    numeric_columns = [
        str(column)
        for column in frame.columns
        if pd.api.types.is_numeric_dtype(frame[column]) and not pd.api.types.is_bool_dtype(frame[column])
    ][:3]
    assert numeric_columns, f"{name}: no numeric columns"

    for column in numeric_columns:
        entry = metrics[column]
        statistics = entry.get("statistics") or {}
        bare = frame[column].dropna()
        assert statistics["mean"] == round_stat(float(bare.mean()))
        assert statistics["median"] == round_stat(float(bare.median()))


# ---------------------------------------------------------------------------
# 3. 机械派生排除锁值
# ---------------------------------------------------------------------------


def test_golden_exclusions_locked():
    # 01：4 对被排除，其中 WAU ~ MAU（|r|=1.0 机械相关）必须在内。
    frame = _read_reference(GOLDEN / "01_核心产品指标日报.csv")
    aggregates = _aggregates("01_核心产品指标日报.csv", frame)
    assert aggregates["excluded_correlation_pairs"] == 4
    detail = _eda_exclusions("01_核心产品指标日报.csv", frame)
    pairs = {(str(item.get("var1")), str(item.get("var2"))) for item in detail}
    assert ("WAU", "MAU") in pairs or ("MAU", "WAU") in pairs

    # 03：4 对（含 满意度 ~ 周NPS 这对高相关序数对）。
    frame = _read_reference(GOLDEN / "03_功能模块使用周报.csv")
    assert _aggregates("03_功能模块使用周报.csv", frame)["excluded_correlation_pairs"] == 4

    # 06：4 对（tokens 之间 |r|=1.0 等）。
    frame = _read_reference(GOLDEN / "06_AI模型效果与成本日报.csv")
    assert _aggregates("06_AI模型效果与成本日报.csv", frame)["excluded_correlation_pairs"] == 4

    # 08：实跑为 0 —— 内置 ARPPU 规则名是 "ARPPU"，而真实列名是 "ARPPU(元/日)"，
    # 规则匹配按批 1 约定取精确名，因此不触发。这是**已知限制**而非回归：
    # golden 把现状固化，后续若放宽规则匹配需连同此断言一起更新。
    frame = _read_reference(GOLDEN / "08_商业化日报.csv")
    assert "excluded_correlation_pairs" not in _aggregates("08_商业化日报.csv", frame)


# ---------------------------------------------------------------------------
# 4. 类型口径：序数 / 比率
# ---------------------------------------------------------------------------


def test_golden_ordinal_and_ratio_scales():
    frame = _read_reference(GOLDEN / "03_功能模块使用周报.csv")
    aggregates = _aggregates("03_功能模块使用周报.csv", frame)
    metrics = _metrics_by_name(aggregates)

    satisfaction = metrics["满意度(1-5)"]
    assert satisfaction["scale"] == "ordinal"
    assert satisfaction["stat_note"] == "序数量表：重点关注分布与众数，均值仅供参考"
    assert satisfaction["distribution"] == {"4.4": 2, "4.2": 2, "4.3": 2, "4.5": 1, "4.0": 1, "4.1": 1, "4.6": 1}
    assert satisfaction["mode"] == "4.2"
    assert satisfaction["mode_percentage"] == pytest.approx(20.0)

    nps = metrics["周NPS"]
    assert nps["scale"] == "ordinal"
    assert nps["mode"] == "33.0"
    assert nps["mode_percentage"] == pytest.approx(10.0)

    assert metrics["渗透率(% 占周活)"]["scale"] == "ratio"
    assert metrics["渗透率(% 占周活)"]["stat_note"] == "比率/百分比指标：优先关注中位数与分位数，跨行平均需谨慎"
    assert "distribution" not in metrics["渗透率(% 占周活)"]

    # 05 的评分列同为序数。
    frame = _read_reference(GOLDEN / "05_用户反馈明细.csv")
    metrics = _metrics_by_name(_aggregates("05_用户反馈明细.csv", frame))
    assert metrics["评分(1-5)"]["scale"] == "ordinal"
    assert metrics["评分(1-5)"]["mode"] == "4.0"
    assert metrics["评分(1-5)"]["mode_percentage"] == pytest.approx(35.0)

    # 01 的留存率类为 ratio，DAU 为 numeric（不加 note）。
    frame = _read_reference(GOLDEN / "01_核心产品指标日报.csv")
    metrics = _metrics_by_name(_aggregates("01_核心产品指标日报.csv", frame))
    assert metrics["次日留存率(%)"]["scale"] == "ratio"
    assert metrics["DAU"]["scale"] == "numeric"
    assert "stat_note" not in metrics["DAU"]


# ---------------------------------------------------------------------------
# 5. role 语义匹配
# ---------------------------------------------------------------------------


def test_golden_roles_hit_for_chinese_event_table():
    frame = _read_reference(GOLDEN / "04_用户行为事件明细.csv")
    frame_ext, _report = extract_text_metrics(frame)
    schema = {item["name"]: item.get("mapping_role") for item in _column_schema(frame_ext)}

    assert schema["用户ID(脱敏)"] == "user_id"
    assert schema["日期"] == "event_time"
    assert schema["事件名称"] == "event_name"


def test_role_match_negative_cases():
    # 指标名不是 ID：「新增用户」是 numeric 指标，不得命中 user_id。
    frame = _read_reference(GOLDEN / "01_核心产品指标日报.csv")
    schema = {item["name"]: item.get("mapping_role") for item in _column_schema(extract_text_metrics(frame)[0])}
    assert schema["新增用户"] is None
    assert schema["DAU"] is None
    assert schema["日期"] == "event_time"

    # 02 的漏斗指标名同样全部不命中。
    frame = _read_reference(GOLDEN / "02_用户增长漏斗.csv")
    schema = {item["name"]: item.get("mapping_role") for item in _column_schema(extract_text_metrics(frame)[0])}
    assert set(schema.values()) == {None}


# ---------------------------------------------------------------------------
# 6. 有效数字
# ---------------------------------------------------------------------------


def test_golden_significant_digits_for_tiny_cost():
    frame = _read_reference(GOLDEN / "06_AI模型效果与成本日报.csv")
    metrics = _metrics_by_name(_aggregates("06_AI模型效果与成本日报.csv", frame))

    cost = metrics["单次调用成本(元)"]
    # round(x, 4) 会给出 0.0028；有效数字 4 位保留 0.002774。
    assert cost["statistics"]["mean"] == 0.002774
    assert cost["statistics"]["mean"] != 0.0028
    assert cost["statistics"]["median"] == 0.00278

    # 大数量级同规则：模型调用次数均值 3.837e6。
    assert metrics["模型调用次数"]["statistics"]["mean"] == 3837000.0


# ---------------------------------------------------------------------------
# 7. digest 的 small_sample 发现
# ---------------------------------------------------------------------------


def test_digest_reports_small_sample_dataset():
    frame = _read_reference(GOLDEN / "02_用户增长漏斗.csv")
    aggregates = _aggregates("02_用户增长漏斗.csv", frame)
    findings = build_findings_digest([aggregates])

    small = [item for item in findings if item.get("kind") == "small_sample"]
    assert small, findings
    assert small[0]["severity"] == 1
    assert "仅 6 行" in small[0]["statement"]

    # 非小样本数据集不产生该发现。
    frame = _read_reference(GOLDEN / "01_核心产品指标日报.csv")
    findings = build_findings_digest([_aggregates("01_核心产品指标日报.csv", frame)])
    assert not [item for item in findings if item.get("kind") == "small_sample"]


# ---------------------------------------------------------------------------
# 8. 类型口径单元（序数判定扩展）
# ---------------------------------------------------------------------------


def test_ordinal_indicators_extended_to_nps():
    frame = pd.DataFrame({"周NPS": [45, 52, 38, 47, 55, 41]})
    info = infer_column_type(frame["周NPS"])
    assert str(info.inferred_type.value) == "ordinal"

    frame = pd.DataFrame({"满意度(1-5)": [1, 2, 3, 4, 5, 4]})
    info = infer_column_type(frame["满意度(1-5)"])
    assert str(info.inferred_type.value) == "ordinal"

    # 普通数值指标不受影响。
    frame = pd.DataFrame({"DAU": [1200, 1350, 1180, 1420, 1500, 1610]})
    info = infer_column_type(frame["DAU"])
    assert str(info.inferred_type.value) == "numeric"


def test_round_stat_semantics():
    assert round_stat(0.00278) == 0.00278
    assert round_stat(45.678) == 45.68
    assert round_stat(1234.5678) == 1235.0  # 4 位有效数字 = 1.235e3（标准舍入）
    assert round_stat(739451.61) == 739500.0  # 4 位有效数字 = 7.395e5
    assert round_stat(None) is None
    assert round_stat(float("nan")) is None
    assert round_stat(float("inf")) is None
    assert round_stat("abc") is None
    assert round_stat(0) == 0.0
    assert isinstance(round_stat(12.5), float) and math.isclose(round_stat(12.5), 12.5)
