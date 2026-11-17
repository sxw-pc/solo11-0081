"""Pure helpers shared by generators and boundary accounting."""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

CHARSETS: dict[str, str] = {
    "alpha": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "lower": "abcdefghijklmnopqrstuvwxyz",
    "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "digits": "0123456789",
    "alnum": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "hex": "0123456789abcdef",
}

DEFAULT_WORDS = (
    "amber", "birch", "cobalt", "delta", "ember", "fern", "granite", "harbor",
    "indigo", "juniper", "kestrel", "linen", "meadow", "north", "opal", "pine",
    "quartz", "river", "slate", "timber", "umber", "violet", "willow", "zephyr",
)


def derive_seed(seed: int, *parts: Any) -> int:
    material = "|".join(["synthlab", str(seed)] + [str(part) for part in parts])
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


def make_rng(seed: int, *parts: Any) -> random.Random:
    return random.Random(derive_seed(seed, *parts))


def normalize_enum(values: Any) -> tuple[tuple[Any, float], ...]:
    if isinstance(values, dict):
        return tuple((value, float(weight)) for value, weight in values.items())
    entries: list[tuple[Any, float]] = []
    for entry in values:
        if isinstance(entry, dict):
            entries.append((entry["value"], float(entry.get("weight", 1))))
        else:
            entries.append((entry, 1.0))
    return tuple(entries)


def weighted_choice(rng: random.Random, entries: tuple[tuple[Any, float], ...]) -> Any:
    total = sum(weight for _, weight in entries)
    cursor = rng.random() * total
    for value, weight in entries:
        cursor -= weight
        if cursor < 0:
            return value
    return entries[-1][0]


def parse_date(value: Any) -> date:
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def parse_datetime(value: Any) -> datetime:
    text = str(value)
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decimal_quantize(value: Any, precision: int) -> Decimal:
    quantizer = Decimal(1).scaleb(-precision)
    return Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP)


def fixed_decimal(value: Any, precision: int) -> str:
    return f"{decimal_quantize(value, precision):.{precision}f}"


def as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def format_template(template: str, row_index: int, start: int = 1) -> str:
    sequence = start + row_index
    return template.format(seq=sequence, row=row_index + 1, value=sequence)


def enum_values(values: Any) -> tuple[Any, ...]:
    return tuple(value for value, _ in normalize_enum(values))


def string_chars(column_type: str, params: dict[str, Any]) -> str:
    charset = str(params.get("charset", "alnum"))
    return CHARSETS.get(charset, CHARSETS["alnum"])


def word_list(params: dict[str, Any]) -> tuple[str, ...]:
    values: Iterable[Any] = params.get("words") or DEFAULT_WORDS
    return tuple(str(item) for item in values)
