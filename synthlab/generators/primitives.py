"""Deterministic column generators. Every generator is self-contained and testable."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from ..errors import GenerationError
from ..spec.model import ColumnSpec
from .common import (
    enum_values,
    format_template,
    normalize_enum,
    parse_date,
    parse_datetime,
    string_chars,
    utc_text,
    weighted_choice,
    word_list,
)


@dataclass(slots=True)
class GenContext:
    row_index: int
    row_count: int = 1
    attempt: int = 0
    parent_values: list[Any] | tuple[Any, ...] = ()
    self_reference: bool = False


class BaseGenerator:
    def __init__(self, column: ColumnSpec, table: str) -> None:
        self.column = column
        self.table = table
        self.name = column.name

    def error(self, message: str, code: str = "GEN002") -> GenerationError:
        return GenerationError(code, message, table=self.table, column=self.name)

    def generate(self, rng: random.Random, context: GenContext) -> Any:  # pragma: no cover - interface
        raise NotImplementedError


class IntGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> int:
        low = int(self.column.param("min", 0))
        high = int(self.column.param("max", 1000))
        step = int(self.column.param("step", 1))
        return low + rng.randrange((high - low) // step + 1) * step


class FloatGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> float:
        low = float(self.column.param("min", 0.0))
        high = float(self.column.param("max", 100.0))
        precision = int(self.column.param("precision", 2))
        return round(rng.uniform(low, high), precision)


class DecimalGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> str:
        precision = int(self.column.param("precision", 2))
        scale = 10 ** precision
        low = Decimal(str(self.column.param("min", 0))) * scale
        high = Decimal(str(self.column.param("max", 100))) * scale
        low_units = int(low.to_integral_value(rounding=ROUND_CEILING))
        high_units = int(high.to_integral_value(rounding=ROUND_FLOOR))
        if low_units > high_units:
            raise self.error("decimal range contains no value at the requested precision")
        units = rng.randint(low_units, high_units)
        return f"{Decimal(units) / scale:.{precision}f}"


class StringGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> str:
        pattern = self.column.param("pattern")
        if pattern:
            try:
                return format_template(str(pattern), context.row_index, int(self.column.param("pattern_start", 1)))
            except (KeyError, IndexError, ValueError) as exc:
                raise self.error(f"invalid string pattern: {exc}") from exc
        low = int(self.column.param("min_length", 4))
        high = int(self.column.param("max_length", 12))
        prefix = str(self.column.param("prefix", ""))
        chars = string_chars(self.column.type, self.column.params)
        low_body, high_body = max(0, low - len(prefix)), high - len(prefix)
        if high_body < 0:
            raise self.error("string prefix is longer than max_length")
        body = "".join(rng.choice(chars) for _ in range(rng.randint(low_body, high_body)))
        return prefix + body


class TextGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> str:
        low = int(self.column.param("min_words", 2))
        high = int(self.column.param("max_words", 6))
        words = word_list(self.column.params)
        count = rng.randint(low, high)
        chosen = [rng.choice(words) for _ in range(count)]
        sentence = " ".join(chosen)
        return sentence[:1].upper() + sentence[1:] + "."


class DateGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> str:
        start = parse_date(self.column.param("start", "2020-01-01"))
        end = parse_date(self.column.param("end", "2024-12-31"))
        step = int(self.column.param("step_days", 1))
        span = (end - start).days // step
        return (start + timedelta(days=rng.randrange(span + 1) * step)).isoformat()


class DatetimeGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> str:
        start = parse_datetime(self.column.param("start", "2020-01-01T00:00:00Z"))
        end = parse_datetime(self.column.param("end", "2024-12-31T23:59:59Z"))
        step = int(self.column.param("step_seconds", 1))
        span = int((end - start).total_seconds()) // step
        return utc_text(start + timedelta(seconds=rng.randrange(span + 1) * step))


class BoolGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> bool:
        return rng.random() < float(self.column.param("true_weight", 0.5))


class EnumGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> Any:
        entries = normalize_enum(self.column.param("values", self.column.param("choices")))
        return weighted_choice(rng, entries)


class UuidGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> str:
        raw = bytearray(rng.randbytes(16))
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        text = raw.hex()
        return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


class SequenceGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> Any:
        start = int(self.column.param("start", 1))
        step = int(self.column.param("step", 1))
        value = start + context.row_index * step
        template = str(self.column.param("format", "{value}"))
        try:
            return template.format(value=value, seq=value, row=context.row_index + 1)
        except (KeyError, IndexError, ValueError) as exc:
            raise self.error(f"invalid sequence format: {exc}") from exc


class ForeignKeyGenerator(BaseGenerator):
    def generate(self, rng: random.Random, context: GenContext) -> Any:
        values = list(context.parent_values)
        if not values:
            if self.column.nullable:
                return None
            raise self.error("foreign key has no parent values to sample", code="GEN003")
        return values[rng.randrange(len(values))]


GENERATOR_CLASSES: dict[str, type[BaseGenerator]] = {
    "int": IntGenerator,
    "float": FloatGenerator,
    "decimal": DecimalGenerator,
    "string": StringGenerator,
    "text": TextGenerator,
    "date": DateGenerator,
    "datetime": DatetimeGenerator,
    "bool": BoolGenerator,
    "enum": EnumGenerator,
    "uuid": UuidGenerator,
    "sequence": SequenceGenerator,
    "foreign_key": ForeignKeyGenerator,
}
