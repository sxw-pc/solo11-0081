"""Immutable-ish domain model shared by validation, generation and verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SUPPORTED_TYPES = (
    "int",
    "float",
    "decimal",
    "string",
    "text",
    "date",
    "datetime",
    "bool",
    "enum",
    "uuid",
    "sequence",
    "foreign_key",
)


@dataclass(frozen=True, slots=True)
class ReferenceSpec:
    table: str
    column: str

    @property
    def display(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """A simple, local table-level check: ``column op literal``."""

    column: str
    operator: str
    value: Any

    def describe(self) -> str:
        return f"{self.column} {self.operator} {self.value!r}"


@dataclass(slots=True)
class ColumnSpec:
    name: str
    type: str
    nullable: bool = True
    null_rate: float = 0.0
    unique: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    references: ReferenceSpec | None = None
    boundary: tuple[str, ...] = ()

    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def is_unique_member(self) -> bool:
        return self.unique


@dataclass(slots=True)
class TableSpec:
    name: str
    rows: int
    primary_key: str
    columns: tuple[ColumnSpec, ...]
    unique_constraints: tuple[tuple[str, ...], ...] = ()
    checks: tuple[CheckSpec, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def column_map(self) -> dict[str, ColumnSpec]:
        return {column.name: column for column in self.columns}

    def column(self, name: str) -> ColumnSpec | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    @property
    def unique_groups(self) -> tuple[tuple[str, ...], ...]:
        groups: list[tuple[str, ...]] = []
        for column in self.columns:
            if column.unique:
                groups.append((column.name,))
        groups.append((self.primary_key,))
        groups.extend(self.unique_constraints)
        return tuple(dict.fromkeys(groups))

    @property
    def unique_columns(self) -> frozenset[str]:
        return frozenset(name for group in self.unique_groups for name in group)


@dataclass(slots=True)
class DatasetSpec:
    name: str
    seed: int
    version: str
    tables: tuple[TableSpec, ...]
    require_boundary_coverage: bool
    max_rows: int
    source_path: str
    blueprint_sha256: str
    blueprint_bytes: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.name for table in self.tables)

    def table(self, name: str) -> TableSpec | None:
        for table in self.tables:
            if table.name == name:
                return table
        return None

    def table_map(self) -> dict[str, TableSpec]:
        return {table.name: table for table in self.tables}


@dataclass(slots=True)
class TableData:
    name: str
    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]
    spec: TableSpec

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def column_index(self, column: str) -> int:
        return self.columns.index(column)

    def values(self, column: str) -> list[Any]:
        index = self.column_index(column)
        return [row[index] for row in self.rows]


@dataclass(frozen=True, slots=True)
class BoundaryResult:
    requested: tuple[str, ...]
    hits: dict[str, bool]
    coverage: float
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": list(self.requested),
            "hits": dict(self.hits),
            "coverage": self.coverage,
            "missing": list(self.missing),
        }


@dataclass(slots=True)
class DatasetData:
    dataset: DatasetSpec
    tables: dict[str, TableData]
    table_order: tuple[str, ...]
    boundary: dict[str, dict[str, BoundaryResult]] = field(default_factory=dict)
    column_stats: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def table(self, name: str) -> TableData:
        return self.tables[name]

    def summary(self) -> dict[str, Any]:
        return {
            "table_order": list(self.table_order),
            "tables": {
                name: {
                    "rows": self.tables[name].row_count,
                    "columns": self.column_stats.get(name, {}),
                }
                for name in self.table_order
            },
        }
