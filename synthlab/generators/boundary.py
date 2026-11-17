"""Boundary candidates and semantic hit checks, independent of generation state."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..spec.model import ColumnSpec
from .common import (
    enum_values,
    fixed_decimal,
    format_template,
    parse_date,
    parse_datetime,
    string_chars,
    utc_text,
    word_list,
)

MISSING = object()
MIN_MARK, MAX_MARK = "min", "max"


def _numeric(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("NaN")


def _is_nullable(column: ColumnSpec) -> bool:
    return column.nullable


def supports_marker(column: ColumnSpec, marker: str) -> bool:
    if marker == "null":
        return _is_nullable(column)
    if marker == "empty":
        return column.type in ("string", "text")
    if marker in (MIN_MARK, MAX_MARK):
        return column.type != "uuid"
    return False


def boundary_candidate(
    column: ColumnSpec,
    marker: str,
    *,
    row_count: int = 1,
    parent_values: list[Any] | tuple[Any, ...] | None = None,
) -> Any:
    """Return a value that represents ``marker`` or MISSING when inapplicable."""
    if marker == "null":
        return None if supports_marker(column, marker) else MISSING
    if marker == "empty":
        return "" if supports_marker(column, marker) else MISSING
    if marker not in (MIN_MARK, MAX_MARK) or not supports_marker(column, marker):
        return MISSING
    if column.type == "foreign_key":
        values = list(parent_values or [])
        if not values:
            return MISSING
        return min(values) if marker == MIN_MARK else max(values)
    if column.type == "uuid":
        return MISSING
    if column.type == "int":
        return int(column.param("min" if marker == MIN_MARK else "max",
                                0 if marker == MIN_MARK else 1000))
    if column.type == "float":
        precision = int(column.param("precision", 2))
        return round(float(column.param("min" if marker == MIN_MARK else "max",
                                        0.0 if marker == MIN_MARK else 100.0)), precision)
    if column.type == "decimal":
        value = column.param("min" if marker == MIN_MARK else "max",
                             0 if marker == MIN_MARK else 100)
        return fixed_decimal(value, int(column.param("precision", 2)))
    if column.type == "string":
        if column.param("pattern"):
            return MISSING
        low = int(column.param("min_length", 4))
        high = int(column.param("max_length", 12))
        chars = string_chars(column.type, column.params)
        desired = low if marker == MIN_MARK else high
        prefix = str(column.param("prefix", ""))
        body = (chars[0] if marker == MIN_MARK else chars[-1]) * max(0, desired - len(prefix))
        return prefix + body
    if column.type == "text":
        words = word_list(column.params)
        count = int(column.param("min_words" if marker == MIN_MARK else "max_words",
                                 2 if marker == MIN_MARK else 6))
        chosen = words[0] if marker == MIN_MARK else words[-1]
        return " ".join([chosen] * count)
    if column.type == "date":
        value = column.param("start" if marker == MIN_MARK else "end",
                             "2020-01-01" if marker == MIN_MARK else "2024-12-31")
        return parse_date(value).isoformat()
    if column.type == "datetime":
        value = column.param("start" if marker == MIN_MARK else "end",
                             "2020-01-01T00:00:00Z" if marker == MIN_MARK else "2024-12-31T23:59:59Z")
        return utc_text(parse_datetime(value))
    if column.type == "bool":
        return marker == MAX_MARK
    if column.type == "enum":
        values = enum_values(column.param("values", column.param("choices")))
        return values[0] if marker == MIN_MARK else values[-1]
    if column.type == "sequence":
        return sequence_boundary_max(column, row_count) if marker == MAX_MARK else _sequence_value(column, 0)
    return MISSING


def matches_boundary(
    column: ColumnSpec,
    marker: str,
    value: Any,
    *,
    row_count: int = 1,
    parent_values: list[Any] | tuple[Any, ...] | None = None,
) -> bool:
    if marker == "null":
        return value is None
    if value is None:
        return False
    if marker == "empty":
        return value == ""
    if marker not in (MIN_MARK, MAX_MARK):
        return False
    if column.type == "string":
        if column.param("pattern"):
            candidate = boundary_candidate(column, marker, row_count=row_count)
            return candidate is not MISSING and value == candidate
        expected = int(column.param("min_length" if marker == MIN_MARK else "max_length",
                                    4 if marker == MIN_MARK else 12))
        return len(str(value)) == expected
    if column.type == "text":
        expected = int(column.param("min_words" if marker == MIN_MARK else "max_words",
                                    2 if marker == MIN_MARK else 6))
        return len(str(value).split()) == expected
    candidate = boundary_candidate(
        column, marker, row_count=row_count, parent_values=parent_values
    )
    if candidate is MISSING:
        return False
    if column.type in ("date", "datetime"):
        return str(value) == str(candidate)
    if column.type in ("int", "float", "decimal"):
        return _numeric(value) == _numeric(candidate)
    if column.type in ("sequence", "foreign_key"):
        return value == candidate
    return value == candidate


def boundary_hits(
    column: ColumnSpec,
    values: list[Any],
    *,
    row_count: int,
    parent_values: list[Any] | tuple[Any, ...] | None = None,
) -> dict[str, bool]:
    return {
        marker: any(
            matches_boundary(column, marker, value, row_count=row_count, parent_values=parent_values)
            for value in values
        )
        for marker in column.boundary
    }


def _sequence_value(column: ColumnSpec, row_index: int) -> Any:
    start = int(column.param("start", 1))
    step = int(column.param("step", 1))
    value = start + step * row_index
    template = str(column.param("format", "{value}"))
    return template.format(value=value, seq=value, row=row_index + 1)


def sequence_boundary_max(column: ColumnSpec, row_count: int) -> Any:
    return _sequence_value(column, max(row_count - 1, 0))
