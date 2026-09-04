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
# Label: the 2-12 CJK/latin/digit run immediately before the number ("7日留存"
# keeps its leading digit; "周 DAU" yields "DAU" because the single "周" is
# split off by whitespace and too short).  The lookahead inside the class
# rejects all-digit runs ("202" out of a date is not a metric name).
_METRIC_LABEL_RE = re.compile(
    r"((?=[0-9A-Za-z\u4e00-\u9fff]*[A-Za-z\u4e00-\u9fff])[0-9A-Za-z\u4e00-\u9fff]{2,12})\s*[:：]?\s*(?=[0-9]|(?:%|％|k|K|M|m|万|亿))"
)
_COMBINED_RE = re.compile(
    rf"(?:((?=[0-9A-Za-z\u4e00-\u9fff]*[A-Za-z\u4e00-\u9fff])[0-9A-Za-z\u4e00-\u9fff]{{2,12}})\s*[:：]?\s*)?({_NUMERIC_TOKEN})"
)

_MIN_LABEL_COVERAGE = 0.3


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

    working = frame.copy()
    if text_columns is None:
        text_columns = [str(name) for name in frame.columns if _is_textlike(frame[name])]
    report: list[dict[str, Any]] = []

    # Batch 14: v2 numeric/datetime/boolean STRING columns are materialised
    # into parsed values on the extended copy ("¥12,000" -> 12000.0,
    # "2026年3月30日" -> datetime, "是" -> True).  Without this the schema
    # says float while the cells are still text and every downstream numeric
    # computation crashes.  The original frame is never touched.
    parsers = {"numeric": parse_numeric, "datetime": parse_datetime_value, "boolean": parse_boolean}
    for column in frame.columns:
        series = working[column]
        if not ((series.dtype == object) or (str(series.dtype) in {"string", "str"})):
            continue
        info = infer_column_type_v2(series)
        parser = parsers.get(info["semantic_type"])
        if parser is not None and info["parse_rate"] >= 0.8:
            working[column] = series.map(parser)

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
            column_name = f"{column}__{entry['display']}"
            working[column_name] = pd.Series(entry["rows"], dtype="float64")
            unit_note = max(entry["units"].items(), key=lambda item: item[1])[0]
            report.append(
                {
                    "source_column": str(column),
                    "metric": entry["display"],
                    "normalized": norm,
                    "derived_column": column_name,
                    "coverage": round(float(coverage), 4),
                    "unit_note": unit_note,
                    "parsed_rows": int(entry["count"]),
                }
            )

    return working, report
