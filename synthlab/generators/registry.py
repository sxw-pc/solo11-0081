"""Column-type to generator registration."""

from __future__ import annotations

from ..errors import GenerationError
from ..spec.model import ColumnSpec
from .primitives import BaseGenerator, GENERATOR_CLASSES


def available_types() -> tuple[str, ...]:
    return tuple(sorted(GENERATOR_CLASSES))


def create_generator(column: ColumnSpec, table: str) -> BaseGenerator:
    generator_class = GENERATOR_CLASSES.get(column.type)
    if generator_class is None:
        raise GenerationError(
            "GEN002",
            f"no generator registered for column type {column.type!r}",
            table=table,
            column=column.name,
        )
    return generator_class(column, table)


def build_generators(columns: tuple[ColumnSpec, ...], table: str) -> dict[str, BaseGenerator]:
    return {column.name: create_generator(column, table) for column in columns}
