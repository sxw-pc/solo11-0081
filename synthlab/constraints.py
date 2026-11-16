"""Post-generation constraint checks and boundary accounting.

This module only consumes the dataset spec and materialized table data; it never
asks the generation engine what it intended to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .errors import Issue
from .distribution import resolve_counts
from .generators.boundary import boundary_hits
from .generators.common import as_text, parse_date, parse_datetime
from .spec.model import BoundaryResult, ColumnSpec, DatasetSpec, TableData, TableSpec


@dataclass(frozen=True, slots=True)
class ConstraintReport:
    issues: tuple[Issue, ...]
    boundary: dict[str, dict[str, BoundaryResult]]

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [issue.as_dict() for issue in self.issues],
            "boundary": {
                table: {column: result.as_dict() for column, result in columns.items()}
                for table, columns in self.boundary.items()
            },
        }


def effective_nullable(table: TableSpec, column: ColumnSpec) -> bool:
    return column.nullable and column.name != table.primary_key


def _parent_pool(dataset: DatasetSpec, tables: dict[str, TableData], column: ColumnSpec) -> list[Any] | None:
    if column.references is None:
        return None
    parent = tables.get(column.references.table)
    if parent is None or column.references.column not in parent.columns:
        return None
    return parent.values(column.references.column)


def build_boundary_report(
    dataset: DatasetSpec, tables: dict[str, TableData]
) -> dict[str, dict[str, BoundaryResult]]:
    report: dict[str, dict[str, BoundaryResult]] = {}
    for table in dataset.tables:
        data = tables.get(table.name)
        if data is None:
            continue
        columns: dict[str, BoundaryResult] = {}
        for column in table.columns:
            requested = tuple(column.boundary)
            if not requested:
                continue
            values = data.values(column.name)
            hits = boundary_hits(
                column,
                values,
                row_count=data.row_count,
                parent_values=_parent_pool(dataset, tables, column),
            )
            missing = tuple(marker for marker in requested if not hits.get(marker, False))
            coverage = (len(requested) - len(missing)) / len(requested) if requested else 1.0
            columns[column.name] = BoundaryResult(requested, hits, coverage, missing)
        if columns:
            report[table.name] = columns
    return report


def _numeric(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _check_primary_key(table: TableSpec, data: TableData, issues: list[Issue]) -> None:
    values = data.values(table.primary_key)
    if any(value is None for value in values):
        issues.append(Issue("CST001", "primary key contains NULL", table=table.name, column=table.primary_key))
    seen: set[Any] = set()
    duplicates = {value for value in values if value is not None and (value in seen or seen.add(value))}
    if duplicates:
        issues.append(Issue("CST001", f"primary key has {len(duplicates)} duplicate value(s)",
                            table=table.name, column=table.primary_key))


def _check_nullability(table: TableSpec, data: TableData, issues: list[Issue]) -> None:
    for column in table.columns:
        if effective_nullable(table, column):
            continue
        nulls = sum(1 for value in data.values(column.name) if value is None)
        if nulls:
            issues.append(Issue("CST003", f"non-nullable column has {nulls} NULL value(s)",
                                table=table.name, column=column.name))


def _check_enums(table: TableSpec, data: TableData, issues: list[Issue]) -> None:
    for column in table.columns:
        if column.type != "enum":
            continue
        raw = column.param("values", column.param("choices"))
        if isinstance(raw, dict):
            allowed = set(raw)
        else:
            allowed = {entry["value"] if isinstance(entry, dict) else entry for entry in raw}
        invalid = {value for value in data.values(column.name) if value is not None and value not in allowed}
        if invalid:
            issues.append(Issue("CST004", f"enum contains disallowed value(s): {sorted(map(str, invalid))[:5]}",
                                table=table.name, column=column.name))


def _check_ranges(table: TableSpec, data: TableData, issues: list[Issue]) -> None:
    for column in table.columns:
        values = [value for value in data.values(column.name) if value is not None]
        if column.type in ("int", "float", "decimal"):
            low = _numeric(column.param("min", 0))
            high = _numeric(column.param("max", 1000 if column.type == "int" else 100))
            bad = [value for value in values if _numeric(value) is None or _numeric(value) < low or _numeric(value) > high]
            if bad:
                issues.append(Issue("CST005", f"{len(bad)} value(s) outside numeric range",
                                    table=table.name, column=column.name))
        elif column.type in ("date", "datetime"):
            parser = parse_date if column.type == "date" else parse_datetime
            low = parser(column.param("start"))
            high = parser(column.param("end"))
            bad = []
            for value in values:
                try:
                    if not low <= parser(value) <= high:
                        bad.append(value)
                except (TypeError, ValueError):
                    bad.append(value)
            if bad:
                issues.append(Issue("CST005", f"{len(bad)} value(s) outside date range",
                                    table=table.name, column=column.name))
        elif column.type == "string" and not column.param("pattern"):
            low = int(column.param("min_length", 4))
            high = int(column.param("max_length", 12))
            bad = [value for value in values if value != "" and not low <= len(str(value)) <= high]
            if bad:
                issues.append(Issue("CST006", f"{len(bad)} value(s) outside length range",
                                    table=table.name, column=column.name))
        elif column.type == "text":
            low = int(column.param("min_words", 2))
            high = int(column.param("max_words", 6))
            bad = [value for value in values if value != "" and not low <= len(str(value).split()) <= high]
            if bad:
                issues.append(Issue("CST006", f"{len(bad)} value(s) outside word-count range",
                                    table=table.name, column=column.name))


def _check_unique(table: TableSpec, data: TableData, issues: list[Issue]) -> None:
    for group in table.unique_groups:
        if table.primary_key in group:
            continue
        seen: set[tuple[Any, ...]] = set()
        duplicates: set[tuple[Any, ...]] = set()
        indexes = [data.column_index(name) for name in group]
        for row in data.rows:
            key = tuple(row[index] for index in indexes)
            if any(value is None for value in key):
                continue
            if key in seen:
                duplicates.add(key)
            seen.add(key)
        if duplicates:
            issues.append(Issue("CST007", f"unique constraint {list(group)} has {len(duplicates)} duplicate(s)",
                                table=table.name, column=",".join(group)))


def _check_foreign_keys(dataset: DatasetSpec, tables: dict[str, TableData], table: TableSpec,
                        data: TableData, issues: list[Issue]) -> None:
    for column in table.columns:
        if column.references is None:
            continue
        parent = tables.get(column.references.table)
        if parent is None or column.references.column not in parent.columns:
            issues.append(Issue("CST002", f"foreign key target {column.references.display} is unavailable",
                                table=table.name, column=column.name))
            continue
        allowed = {value for value in parent.values(column.references.column) if value is not None}
        missing = {value for value in data.values(column.name) if value is not None and value not in allowed}
        if missing:
            issues.append(Issue("CST002", f"{len(missing)} foreign key value(s) missing from parent",
                                table=table.name, column=column.name))


def _check_distributions(dataset: DatasetSpec, tables: dict[str, TableData], table: TableSpec,
                         data: TableData, issues: list[Issue]) -> None:
    """Recompute per-class child counts and foreign key ownership from data."""
    missing = object()
    for column in table.columns:
        dist = column.distribute
        if dist is None or column.references is None:
            continue
        parent = tables.get(column.references.table)
        if parent is None or column.references.column not in parent.columns:
            continue  # dangling parent is reported by _check_foreign_keys
        if dist.attribute not in parent.columns:
            issues.append(Issue(
                "CST010",
                f"distribute.by attribute {column.references.table}.{dist.attribute} is unavailable",
                table=table.name, column=column.name,
            ))
            continue
        key_class: dict[Any, Any] = {}
        for key, attribute in zip(parent.values(column.references.column),
                                  parent.values(dist.attribute)):
            if key is not None:
                key_class[key] = attribute
        values = [value for value in data.values(column.name) if value is not None]
        counts, _remainder = resolve_counts(dist, len(values))
        declared = {as_text(value) for value, _ in counts}
        actual: dict[str, int] = {}
        unlisted = 0
        for value in values:
            cls = key_class.get(value, missing)
            if cls is missing:
                continue  # dangling key is reported by _check_foreign_keys
            label = as_text(cls)
            if label in declared:
                actual[label] = actual.get(label, 0) + 1
            else:
                unlisted += 1
        for value, expected in counts:
            label = as_text(value)
            found = actual.get(label, 0)
            if found != expected:
                issues.append(Issue(
                    "CST010",
                    f"distribution quota for parent attribute value {label!r}: "
                    f"expected {expected} row(s), found {found}",
                    table=table.name, column=column.name,
                ))
        if dist.unlisted == "forbid" and unlisted:
            issues.append(Issue(
                "CST010",
                f"{unlisted} row(s) reference unlisted parent attribute values",
                table=table.name, column=column.name,
            ))


def _compare(value: Any, operator: str, literal: Any) -> bool:
    left, right = value, literal
    if isinstance(literal, (int, float)) and not isinstance(literal, bool):
        left = _numeric(value)
        right = _numeric(literal)
        if left is None or right is None:
            return False
    operations: dict[str, Callable[[Any, Any], bool]] = {
        ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, "<": lambda a, b: a < b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
    }
    try:
        return operations[operator](left, right)
    except TypeError:
        return False


def _check_table_checks(table: TableSpec, data: TableData, issues: list[Issue]) -> None:
    for check in table.checks:
        failures = [
            value for value in data.values(check.column)
            if value is not None and not _compare(value, check.operator, check.value)
        ]
        if failures:
            issues.append(Issue("CST008", f"table check failed: {check.describe()} ({len(failures)} row(s))",
                                table=table.name, column=check.column))


def check_dataset(
    dataset: DatasetSpec,
    tables: dict[str, TableData],
    *,
    enforce_boundary: bool | None = None,
) -> ConstraintReport:
    issues: list[Issue] = []
    for table in dataset.tables:
        data = tables.get(table.name)
        if data is None:
            issues.append(Issue("CST002", "table data is missing", table=table.name))
            continue
        data.spec = table
        _check_primary_key(table, data, issues)
        _check_nullability(table, data, issues)
        _check_enums(table, data, issues)
        _check_ranges(table, data, issues)
        _check_unique(table, data, issues)
        _check_foreign_keys(dataset, tables, table, data, issues)
        _check_distributions(dataset, tables, table, data, issues)
        _check_table_checks(table, data, issues)
    boundary = build_boundary_report(dataset, tables)
    require = dataset.require_boundary_coverage if enforce_boundary is None else enforce_boundary
    if require:
        for table, columns in boundary.items():
            for column, result in columns.items():
                if result.missing:
                    issues.append(Issue("CST009", f"required boundary markers not covered: {list(result.missing)}",
                                        table=table, column=column))
    return ConstraintReport(tuple(issues), boundary)
