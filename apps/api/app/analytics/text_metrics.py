"""Text-metric extraction: derive numeric columns from free-text cells.

Business tables often bury metrics in prose ("周 DAU 110k，7日留存 45%").
``extract_text_metrics`` scans text columns row by row for the fixed pattern
"label + number + optional unit", promotes labels that appear in enough rows
into first-class derived numeric columns (``{source}__{label}``), and reports
per-metric coverage.  Everything is deterministic: the token grammar, label
grammar and normalization are module constants, and numbers that do not match
the pattern are never claimed.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .parsing import infer_column_type_v2, parse_boolean, parse_datetime_value, parse_numeric

# Number token: digits with optional thousands separators, then an optional
# unit suffix (percent / magnitude / common business units).
_NUMERIC_TOKEN = r"\d[\d,\.]*\s*(?:%|％|k|K|M|m|万|亿|分|元|人|天|小时)?"
# Label: the 2-12 CJK/latin/digit run immediately before the number.  Two
# boundary rules, both needed:
#
# 1. the last character must be a letter or CJK -- a label may never end on a
#    digit.  Without it the greedy quantifier backtracked only to the last
#    digit, so "本周DAU110k" labelled as "本周DAU11" and the number became
#    "0k" -> 0.0 instead of 110000.0;
# 2. a digit inside the label may not be followed by a unit char.  Without it
#    "本周DAU110k留存率45%" still labelled as "本周DAU110k留存率" (a valid
#    12-char run ending on 率), swallowing the 110k entirely instead of
#    yielding 本周DAU=110000 plus 留存率=45.  "Top3销量 800" keeps its label
#    because the inner "3" is followed by a letter, not a unit.
_METRIC_LABEL_BODY = (
    r"(?:(?![0-9](?:%|％|k|K|M|m|万|亿|分|元|人|天|小时))[0-9A-Za-z\u4e00-\u9fff]){1,11}"
    r"[A-Za-z\u4e00-\u9fff]"
)
_METRIC_LABEL_RE = re.compile(
    rf"({_METRIC_LABEL_BODY})\s*[:：]?\s*(?=[0-9]|(?:%|％|k|K|M|m|万|亿))"
)
_COMBINED_RE = re.compile(
    rf"(?:({_METRIC_LABEL_BODY})\s*[:：]?\s*)?({_NUMERIC_TOKEN})"
)

_MIN_LABEL_COVERAGE = 0.3
#: 物化失败样本的出样上限（整张表）。物化是"尽力而为"的（parse_rate ≥ 0.8 才整列
#: 物化），失败单元格会静默变 None —— 这里留下少量可审计的证据，而不是吞掉。
UNPARSED_SAMPLE_LIMIT = 5


def materialize_string_columns(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Materialise v2 numeric/datetime/boolean STRING columns into parsed values.

    "¥12,000" -> 12000.0, "2026年3月30日" -> datetime, "是" -> True.  A column
    is materialised only when at least 80% of its non-empty cells parse, so a
    single dirty cell silently becomes ``None`` — ``unparsed_samples`` records
    those refusals (``{"column", "row", "value"}``, at most
    ``UNPARSED_SAMPLE_LIMIT`` entries table-wide) instead of losing them.

    Returns ``(materialised_frame, unparsed_samples)``; the input frame is
    never touched.  ``extract_text_metrics`` delegates here, so the behaviour
    is identical to the inline block it replaced.
    """
    working = frame.copy()
    unparsed: list[dict[str, Any]] = []
    parsers = {"numeric": parse_numeric, "datetime": parse_datetime_value, "boolean": parse_boolean}
    for column in frame.columns:
        series = working[column]
        if not ((series.dtype == object) or (str(series.dtype) in {"string", "str"})):
            continue
        info = infer_column_type_v2(series)
        parser = parsers.get(info["semantic_type"])
        if parser is None or info["parse_rate"] < 0.8:
            continue
        parsed = series.map(parser)
        working[column] = parsed
        for index, original in series.items():
            if len(unparsed) >= UNPARSED_SAMPLE_LIMIT:
                break
            try:
                if pd.isna(original):
                    continue  # 空单元格是缺失，不是未解析
            except (TypeError, ValueError):
                pass
            value = parsed.at[index]
            if value is None or (not isinstance(value, str) and pd.isna(value)):
                row = int(index) if isinstance(index, (int,)) and not isinstance(index, bool) else str(index)
                unparsed.append({"column": str(column), "row": row, "value": str(original)})
    return working, unparsed


def _normalise_label(label: str) -> str:
    return re.sub(r"\s+", "", label).lower()


def _unit_note(token: str) -> str:
    match = re.search(r"(%|％|k|K|M|m|万|亿|分|元|人|天|小时)\s*$", token)
    return match.group(1) if match else ""


def _is_textlike(series: pd.Series) -> bool:
    if not ((series.dtype == object) or (str(series.dtype) in {"string", "str"})):
        return False
    # Only genuine prose/category columns are scanned: dates, identifiers,
    # constants and near-numeric string columns would only yield junk tokens.
    info = infer_column_type_v2(series)
    if info["constant"]:
        return False
    return info["semantic_type"] in {"text", "category"}


def extract_text_metrics(
    frame: pd.DataFrame,
    *,
    text_columns: list[str] | None = None,
    min_label_coverage: float = _MIN_LABEL_COVERAGE,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Extract per-row ``(metric, number)`` pairs from text columns.

    Returns ``(derived_frame, extraction_report)``.  The derived frame is a
    copy of the input plus one float column per promoted metric, named
    ``{source_column}__{label}`` and row-aligned to the input (rows without a
    match stay ``NaN`` -- the original frame is never touched).  The report
    lists every extracted metric with its coverage, unit note and parsed-row
    count; labels below ``min_label_coverage`` are reported nowhere and never
    become columns.
    """

    if text_columns is None:
        text_columns = [str(name) for name in frame.columns if _is_textlike(frame[name])]
    report: list[dict[str, Any]] = []

    # Batch 14: v2 numeric/datetime/boolean STRING columns are materialised
    # into parsed values on the extended copy ("¥12,000" -> 12000.0,
    # "2026年3月30日" -> datetime, "是" -> True).  Without this the schema
    # says float while the cells are still text and every downstream numeric
    # computation crashes.  The original frame is never touched.  Batch 32:
    # the block lives in ``materialize_string_columns`` so the parse job can
    # also collect the refusals; behaviour is unchanged.
    working, _unparsed_samples = materialize_string_columns(frame)

    for column in text_columns or []:
        if column not in working.columns or not _is_textlike(working[column]):
            continue
        series = working[column]
        non_empty = series.dropna()
        total_rows = len(non_empty)
        if total_rows < 2:
            continue
        # norm -> {"display", "rows": {index: value}, "count", "units"}
        metrics: dict[str, dict[str, Any]] = {}
        for index, raw in non_empty.items():
            text = str(raw)
            seen_in_row: set[str] = set()
            for match in _COMBINED_RE.finditer(text):
                label, token = match.group(1), match.group(2)
                if label is None:
                    continue  # a bare number without a label is never claimed
                number = parse_numeric(token)
                if number is None:
                    continue
                norm = _normalise_label(label)
                if norm not in metrics:
                    metrics[norm] = {"display": label, "rows": {}, "count": 0, "units": {}}
                entry = metrics[norm]
                unit = _unit_note(token) or "none"
                entry["units"][unit] = entry["units"].get(unit, 0) + 1
                if index in entry["rows"]:
                    # Same metric twice in one cell: keep the first, stay deterministic.
                    continue
                if norm in seen_in_row:
                    continue
                seen_in_row.add(norm)
                entry["rows"][index] = number
                entry["count"] += 1

        for norm, entry in metrics.items():
            coverage = entry["count"] / total_rows
            if coverage < min_label_coverage:
                continue
            base_name = f"{column}__{entry['display']}"
            column_name = base_name
            suffix = 1
            # 不得静默覆盖既有列：抽取列与原列（或另一个抽取列）同名时追加确定性
            # 后缀，并把它记进 report 供人工发现。
            while column_name in working.columns:
                column_name = f"{base_name}__dup{suffix}"
                suffix += 1
            working[column_name] = pd.Series(entry["rows"], dtype="float64")
            unit_note = max(entry["units"].items(), key=lambda item: item[1])[0]
            report_entry: dict[str, Any] = {
                "source_column": str(column),
                "metric": entry["display"],
                "normalized": norm,
                "derived_column": column_name,
                "coverage": round(float(coverage), 4),
                "unit_note": unit_note,
                "parsed_rows": int(entry["count"]),
            }
            if column_name != base_name:
                report_entry["renamed_from"] = base_name
            report.append(report_entry)

    # 同一 normalized 指标在不同源列里可能写出不同的显示名（DAU / dau / Dau），
    # 于是派生出多个"其实是同一个指标"的列。把显示变体记进 report 便于人工发现
    # 指标碎片化；刻意不合并 —— 合并会改变既有派生列名契约。
    variants: dict[str, set[str]] = {}
    for entry in report:
        variants.setdefault(entry["normalized"], set()).add(entry["metric"])
    for entry in report:
        distinct = sorted(variants.get(entry["normalized"], ()))
        if len(distinct) > 1:
            entry["display_variants"] = distinct

    return working, report
