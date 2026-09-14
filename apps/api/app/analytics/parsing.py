"""Robust value parsing for real-world spreadsheet columns (batch 14).

Real business tables store numbers as ``1,234`` / ``¥12,000`` / ``45%`` /
``110k`` and dates as ``2026年3月30日``.  These parsers turn such cells into
typed values so type inference, aggregation and trend analysis see the data
the way an analyst would.  Everything is deterministic: fixed regexes, fixed
suffix multipliers, no semantic guessing -- unparseable input yields ``None``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd

_TRUE_TOKENS = {"是", "true", "y", "yes", "1"}
_FALSE_TOKENS = {"否", "false", "n", "no", "0"}
_PERCENT_SUFFIX_RE = re.compile(r"(?:%|％)$")
_NUMERIC_SUFFIX_RE = re.compile(r"(k|K|M|m|B|b|万|亿)$")
_CURRENCY_PREFIX_RE = re.compile(r"^[¥￥$]")
_PARENS_RE = re.compile(r"^\((.*)\)$|^（(.*)）$")
_THOUSANDS_RE = re.compile(r"[,\s]")
_NUMBER_BODY_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_SUFFIX_MULTIPLIERS = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9, "万": 1e4, "亿": 1e8}

_ISO_DATE_RE = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)
_CN_DATE_RE = re.compile(
    r"^(\d{4})年(?:\s*(\d{1,2})月)?(?:\s*(\d{1,2})[日号])?(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)
SAMPLE_LIMIT = 2000

#: 「标识符」形状的三条边界。近唯一只是必要条件：无空格的中文长句同样"近唯一"，
#: 但它是自由文本，必须落到 ``text`` 才能被 ``text_metrics.extract_text_metrics``
#: 扫描。``id-0`` / ``KA001`` 这类短标识仍在边界内，判据不变。
IDENTIFIER_MAX_LENGTH = 40
IDENTIFIER_SENTENCE_PUNCTUATION = "。！？，、；："


def _looks_like_identifier(values: list[Any]) -> bool:
    """近唯一的字符串列是否具备「标识符」形状。

    三条**同时**满足才算标识符：样本不含任何空白字符、长度不超过
    :data:`IDENTIFIER_MAX_LENGTH`、且不含句读标点
    (:data:`IDENTIFIER_SENTENCE_PUNCTUATION`)。无空格的中文长句会被后两条挡下，
    因而归为 ``text``；短标识（``id-0``、``KA001``）不受影响。
    """

    sample = values[:20]
    if not sample:
        return False
    for item in sample:
        text = str(item)
        if any(character.isspace() for character in text):
            return False
        if len(text) > IDENTIFIER_MAX_LENGTH:
            return False
        if any(character in IDENTIFIER_SENTENCE_PUNCTUATION for character in text):
            return False
    return True


def parse_numeric(value: Any) -> float | None:
    """Parse a spreadsheet-style numeric cell into a float.

    Handles thousands separators, currency prefixes (¥￥$), percent suffixes
    (the number itself is kept: ``45%`` -> ``45.0``), magnitude suffixes
    (``110k`` -> ``110000.0``, ``1.5万`` -> ``15000.0``), accounting-style
    negative parentheses and plain numbers.  Returns ``None`` for anything
    else -- including booleans, which belong to :func:`parse_boolean`.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number != number else number  # NaN guard
    text = str(value).strip()
    if not text:
        return None
    negative = False
    parens = _PARENS_RE.match(text)
    if parens:
        negative = True
        text = "".join(group for group in parens.groups() if group)
    multiplier = 1.0
    if _PERCENT_SUFFIX_RE.search(text):
        # A percent is kept as its own number; the unit lives in metadata.
        text = _PERCENT_SUFFIX_RE.sub("", text).strip()
    else:
        suffix = _NUMERIC_SUFFIX_RE.search(text)
        if suffix:
            multiplier = _SUFFIX_MULTIPLIERS[suffix.group(1)]
            text = text[: suffix.start()].strip()
    text = _CURRENCY_PREFIX_RE.sub("", text).strip()
    text = _THOUSANDS_RE.sub("", text)
    if not _NUMBER_BODY_RE.fullmatch(text):
        return None
    number = float(text) * multiplier
    return -number if negative else number


def parse_datetime_value(value: Any) -> datetime | None:
    """Parse ISO, slash and Chinese date cells into a ``datetime``.

    Accepted shapes: ``2026-03-30``, ``2026/3/30``, ``2026-03-30 10:00``,
    ``2026-03-30T10:00:30``, ``2026年3月30日``, ``2026年3月`` and the same with
    a time part.  Returns ``None`` when nothing matches.
    """

    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):  # pandas Timestamp
        return value.to_pydatetime()
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _ISO_DATE_RE.match(text)
    if match:
        year, month, day, hour, minute, second = match.groups()
        return datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0)
        )
    match = _CN_DATE_RE.match(text)
    if match:
        year, month, day, hour, minute, second = match.groups()
        if month is None and day is None:
            return None
        return datetime(
            int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0)
        )
    return None


def parse_boolean(value: Any) -> bool | None:
    """Parse common boolean spellings: 是/否, true/false, 0/1, Y/N."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    text = str(value).strip().lower()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return None


def infer_column_type_v2(series: pd.Series) -> dict[str, Any]:
    """Classify a column into the product's semantic types.

    Returns ``{"semantic_type", "parse_rate", "unique_ratio", "constant",
    "identifier"}``.  ``semantic_type`` is one of ``numeric | datetime |
    boolean | category | text | identifier``; string columns are sampled
    through the parsers and a type wins when ≥ 80% of non-empty cells parse.
    ``identifier`` marks near-unique string columns **that also look like an
    identifier** (compact, whitespace-free, no sentence punctuation -- see
    :func:`_looks_like_identifier`), so prose without spaces falls through to
    ``text``.  ``constant`` marks single-value columns (orthogonal to the main
    type).
    """

    non_empty = series.dropna()
    values = [item for item in non_empty.tolist()][:SAMPLE_LIMIT]
    unique_ratio = (len(set(map(str, values))) / len(values)) if values else 0.0
    is_constant = bool(values) and len(set(map(str, values))) == 1
    if not values:
        return {"semantic_type": "text", "parse_rate": 0.0, "unique_ratio": 0.0, "constant": False, "identifier": False}

    if pd.api.types.is_bool_dtype(series):
        return {"semantic_type": "boolean", "parse_rate": 1.0, "unique_ratio": round(float(unique_ratio), 4), "constant": is_constant, "identifier": False}
    if pd.api.types.is_datetime64_any_dtype(series):
        return {"semantic_type": "datetime", "parse_rate": 1.0, "unique_ratio": round(float(unique_ratio), 4), "constant": is_constant, "identifier": False}
    if pd.api.types.is_numeric_dtype(series):
        # identifier is a string-column concept (batch 14 spec): a numeric
        # metric column stays numeric no matter how unique its values are.
        return {"semantic_type": "numeric", "parse_rate": 1.0, "unique_ratio": round(float(unique_ratio), 4), "constant": is_constant, "identifier": False}

    # String (object) columns: sample every parser; the first one whose parse
    # rate clears the threshold wins (numeric before datetime before boolean,
    # so 0/1 columns stay numeric and 是/否 columns become boolean).
    numeric_rate = sum(parse_numeric(item) is not None for item in values) / len(values)
    datetime_rate = sum(parse_datetime_value(item) is not None for item in values) / len(values)
    boolean_rate = sum(parse_boolean(item) is not None for item in values) / len(values)
    if numeric_rate >= 0.8:
        semantic, parse_rate = "numeric", numeric_rate
    elif datetime_rate >= 0.8:
        semantic, parse_rate = "datetime", datetime_rate
    elif boolean_rate >= 0.8:
        semantic, parse_rate = "boolean", boolean_rate
    elif unique_ratio >= 0.9 and _looks_like_identifier(values):
        # 近唯一 **且** 具备标识符形状（紧凑、无空白、无句读）：长句与带标点的
        # 自由文本即使无空格也不会被误判成 identifier。
        semantic, parse_rate = "identifier", 0.0
    elif unique_ratio <= 0.4:
        semantic, parse_rate = "category", 0.0
    else:
        semantic, parse_rate = "text", 0.0
    return {
        "semantic_type": semantic,
        "parse_rate": round(float(parse_rate), 4),
        "unique_ratio": round(float(unique_ratio), 4),
        "constant": is_constant,
        "identifier": semantic == "identifier",
    }
