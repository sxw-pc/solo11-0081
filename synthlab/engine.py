"""Deterministic multi-table generation engine."""

from __future__ import annotations

import random
from typing import Any

from .constraints import build_boundary_report, effective_nullable
from .errors import GenerationError
from .generators.boundary import MISSING, boundary_candidate, supports_marker
from .generators.common import make_rng
from .generators.primitives import GenContext
from .generators.registry import build_generators
from .planner import ExecutionPlan, plan_dataset
from .spec.model import ColumnSpec, DatasetData, DatasetSpec, TableData, TableSpec

MAX_UNIQUE_ATTEMPTS = 50


def _parent_values(
    column: ColumnSpec, table: TableSpec, column_pools: dict[str, dict[str, list[Any]]]
) -> list[Any]:
    if column.references is None:
        return []
    if column.references.table == table.name:
        return list(column_pools.get(table.name, {}).get(column.references.column, []))
    return column_pools.get(column.references.table, {}).get(column.references.column, [])


def _plan_boundaries(
    dataset: DatasetSpec,
    table: TableSpec,
    column_pools: dict[str, dict[str, list[Any]]],
) -> dict[tuple[int, str], Any]:
    forced: dict[tuple[int, str], Any] = {}
    unique_columns = table.unique_columns
    for column in table.columns:
        if not column.boundary:
            continue
        used_rows: set[int] = set()
        is_self = column.references is not None and column.references.table == table.name
        parent_values = _parent_values(column, table, column_pools) if column.references else None
        for marker in column.boundary:
            if column.name in unique_columns and marker != "null":
                continue
            if is_self and marker not in ("null", "empty"):
                continue
            if not supports_marker(column, marker):
                continue
            for row_index in range(table.rows):
                if row_index in used_rows:
                    continue
                candidate = boundary_candidate(
                    column,
                    marker,
                    row_count=table.rows,
                    parent_values=parent_values,
                )
                if candidate is MISSING:
                    break
                forced[(row_index, column.name)] = candidate
                used_rows.add(row_index)
                break
    return forced


def _row_conflict(
    table: TableSpec,
    row: tuple[Any, ...],
    seen: dict[tuple[str, ...], set[tuple[Any, ...]]],
    data_columns: tuple[str, ...],
) -> tuple[str, ...] | None:
    indexes = {name: data_columns.index(name) for name in table.column_names}
    for group in table.unique_groups:
        key = tuple(row[indexes[name]] for name in group)
        if any(value is None for value in key):
            continue
        if key in seen[group]:
            return group
    return None


def _record_unique(
    table: TableSpec,
    row: tuple[Any, ...],
    seen: dict[tuple[str, ...], set[tuple[Any, ...]]],
    data_columns: tuple[str, ...],
) -> None:
    indexes = {name: data_columns.index(name) for name in table.column_names}
    for group in table.unique_groups:
        key = tuple(row[indexes[name]] for name in group)
        if not any(value is None for value in key):
            seen[group].add(key)


def _cell_value(
    dataset: DatasetSpec,
    table: TableSpec,
    column: ColumnSpec,
    generator: Any,
    row_index: int,
    attempt: int,
    forced: dict[tuple[int, str], Any],
    column_pools: dict[str, dict[str, list[Any]]],
) -> Any:
    forced_key = (row_index, column.name)
    if forced_key in forced:
        return forced[forced_key]
    self_reference = column.references is not None and column.references.table == table.name
    if self_reference and row_index == 0:
        if not column.nullable:
            raise GenerationError("GEN003", "self-referencing foreign key must be nullable",
                                  table=table.name, column=column.name)
        return None
    rng = make_rng(dataset.seed, dataset.name, table.name, column.name, row_index, attempt)
    if effective_nullable(table, column) and column.null_rate > 0 and rng.random() < column.null_rate:
        return None
    context = GenContext(
        row_index=row_index,
        row_count=table.rows,
        attempt=attempt,
        parent_values=_parent_values(column, table, column_pools),
        self_reference=self_reference,
    )
    return generator.generate(rng, context)


def _generate_rows(
    dataset: DatasetSpec,
    table: TableSpec,
    forced: dict[tuple[int, str], Any],
    column_pools: dict[str, dict[str, list[Any]]],
) -> list[tuple[Any, ...]]:
    generators = build_generators(table.columns, table.name)
    columns = table.column_names
    seen = {group: set() for group in table.unique_groups}
    rows: list[tuple[Any, ...]] = []
    for row_index in range(table.rows):
        previous: tuple[Any, ...] | None = None
        for attempt in range(MAX_UNIQUE_ATTEMPTS):
            row = tuple(
                _cell_value(dataset, table, column, generators[column.name], row_index, attempt,
                            forced, column_pools)
                for column in table.columns
            )
            conflict = _row_conflict(table, row, seen, columns)
            if conflict is None:
                rows.append(row)
                _record_unique(table, row, seen, columns)
                for index, column in enumerate(table.columns):
                    column_pools.setdefault(table.name, {}).setdefault(column.name, []).append(row[index])
                break
            if previous == row:
                raise GenerationError(
                    "GEN001",
                    f"unique constraint {list(conflict)} cannot be satisfied; deterministic values repeat",
                    table=table.name,
                    column=",".join(conflict),
                )
            previous = row
        else:
            raise GenerationError(
                "GEN001",
                f"unique retry limit ({MAX_UNIQUE_ATTEMPTS}) exhausted",
                table=table.name,
                column=",".join(table.unique_groups[0]) if table.unique_groups else table.primary_key,
            )
    return rows


def _column_stats(
    table: TableSpec,
    data: TableData,
    boundary: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for column in table.columns:
        values = data.values(column.name)
        hits = boundary.get(table.name, {}).get(column.name)
        result[column.name] = {
            "name": column.name,
            "type": column.type,
            "null_count": sum(1 for value in values if value is None),
            "distinct_count": len({value for value in values if value is not None}),
            "boundary_hits": dict(hits.hits) if hits else {},
        }
    return result


def generate_dataset(dataset: DatasetSpec, plan: ExecutionPlan | None = None) -> DatasetData:
    execution = plan or plan_dataset(dataset)
    tables: dict[str, TableData] = {}
    column_pools: dict[str, dict[str, list[Any]]] = {}
    for table_name in execution.order:
        table = dataset.table(table_name)
        if table is None:
            raise GenerationError("GEN002", f"planned table {table_name!r} is missing")
        forced = _plan_boundaries(dataset, table, column_pools)
        rows = _generate_rows(dataset, table, forced, column_pools)
        tables[table_name] = TableData(table_name, table.column_names, rows, table)
    boundary = build_boundary_report(dataset, tables)
    stats = {
        table_name: _column_stats(dataset.table(table_name), tables[table_name], boundary)
        for table_name in execution.order
        if dataset.table(table_name) is not None
    }
    return DatasetData(dataset, tables, execution.order, boundary, stats)
