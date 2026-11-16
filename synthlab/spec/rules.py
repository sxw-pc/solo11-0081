"""Parameter-level blueprint rules kept separate from structural validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..errors import IssueBag

BOUNDARY_MARKERS = {"min", "max", "null", "empty"}
CHECK_OPERATORS = {">=", "<=", "!=", "==", ">", "<"}
STRING_CHARSETS = {"alpha", "lower", "upper", "digits", "alnum", "hex"}


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_moment(value: Any, kind: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError("not a string")
    if kind == "date":
        return datetime.strptime(value, "%Y-%m-%d")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


def validate_enum(values: Any, table: str, column: str, bag: IssueBag) -> None:
    if values is None:
        bag.add("SPEC007", "enum requires values", table=table, column=column)
        return
    if isinstance(values, dict):
        entries = list(values.items())
        if not entries:
            bag.add("SPEC007", "enum requires at least one value", table=table, column=column)
        seen: set[Any] = set()
        for value, weight in entries:
            if value in seen or not is_number(weight) or weight <= 0:
                bag.add("SPEC007", "enum values must be unique and weights must be positive",
                        table=table, column=column)
                return
            seen.add(value)
        return
    if not isinstance(values, list) or not values:
        bag.add("SPEC007", "enum values must be a non-empty list or mapping", table=table, column=column)
        return
    seen = set()
    for entry in values:
        if isinstance(entry, dict):
            if "value" not in entry or not is_number(entry.get("weight", 1)) or entry.get("weight", 1) <= 0:
                bag.add("SPEC007", "enum entries need value and positive weight", table=table, column=column)
                return
            value = entry["value"]
        else:
            value = entry
        if value in seen:
            bag.add("SPEC007", "enum values must be unique", table=table, column=column)
            return
        seen.add(value)


def validate_params(column_type: str, params: dict[str, Any], table: str, column: str, bag: IssueBag) -> None:
    def bad(message: str) -> None:
        bag.add("SPEC009", message, table=table, column=column)

    if column_type == "int":
        low, high, step = params.get("min", 0), params.get("max", 1000), params.get("step", 1)
        if not all(is_int(v) for v in (low, high, step)) or low > high or step <= 0:
            bad("int parameters require integer min <= max and positive step")
    elif column_type in ("float", "decimal"):
        low, high = params.get("min", 0), params.get("max", 100)
        precision = params.get("precision", 2)
        if not is_number(low) or not is_number(high) or low > high:
            bad(f"{column_type} parameters require numeric min <= max")
        if not is_int(precision) or precision < 0 or precision > 12:
            bad("precision must be an integer between 0 and 12")
    elif column_type == "string":
        low, high = params.get("min_length", 4), params.get("max_length", 12)
        if not is_int(low) or not is_int(high) or low < 0 or low > high:
            bad("string parameters require 0 <= min_length <= max_length")
        if params.get("charset", "alnum") not in STRING_CHARSETS:
            bad(f"charset must be one of {sorted(STRING_CHARSETS)}")
        if "pattern" in params and not isinstance(params["pattern"], str):
            bad("pattern must be a string")
        prefix = params.get("prefix", "")
        if not isinstance(prefix, str):
            bad("prefix must be a string")
        elif is_int(high) and len(prefix) > high:
            bad("prefix must not be longer than max_length")
    elif column_type == "text":
        low, high = params.get("min_words", 2), params.get("max_words", 6)
        if not is_int(low) or not is_int(high) or low < 1 or low > high:
            bad("text parameters require 1 <= min_words <= max_words")
        words = params.get("words")
        if words is not None and (not isinstance(words, list) or not words or not all(isinstance(w, str) for w in words)):
            bad("text words must be a non-empty list of strings")
    elif column_type in ("date", "datetime"):
        try:
            start = parse_moment(params.get("start"), column_type)
            end = parse_moment(params.get("end"), column_type)
        except (TypeError, ValueError):
            bad(f"{column_type} start and end must be ISO values")
            return
        if start > end:
            bad(f"{column_type} start must not be after end")
        step_key = "step_days" if column_type == "date" else "step_seconds"
        if not is_int(params.get(step_key, 1)) or params.get(step_key, 1) <= 0:
            bad(f"{step_key} must be a positive integer")
    elif column_type == "bool":
        weight = params.get("true_weight", 0.5)
        if not is_number(weight) or not 0 <= weight <= 1:
            bad("bool true_weight must be between 0 and 1")
    elif column_type == "enum":
        validate_enum(params.get("values", params.get("choices")), table, column, bag)
    elif column_type == "sequence":
        if not is_int(params.get("start", 1)) or not is_int(params.get("step", 1)) or params.get("step", 1) <= 0:
            bad("sequence start must be integer and step must be positive")
        if "format" in params and not isinstance(params["format"], str):
            bad("sequence format must be a string")
